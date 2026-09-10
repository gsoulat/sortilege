"""Orchestration : du fichier trouve au plan de rangement.

Enchaine les etapes qui existaient deja isolement. Aucune logique metier ici —
c'est un chef d'orchestre, et le fait qu'il soit court est le signe que la
separation tient.

Deux contraintes gouvernent la forme du code :

- **La concurrence est bornee.** Une bibliotheque de 2 000 fichiers lancerait
  2 000 requetes simultanees, ce qui epuiserait le quota TMDB et ferait bannir
  l'adresse. Un semaphore limite les appels en vol.
- **Un fichier qui echoue n'arrete pas le lot.** Chaque fichier produit un plan,
  fut-il un rejet motive. Une exception qui remonterait ferait perdre le
  travail deja fait sur les precedents.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from ..providers.anilist import AniListProvider
from ..providers.base import Candidate
from ..providers.tmdb import TMDBProvider
from .ai import AIProposal, AmbiguousItem
from .franchise import franchise_for
from .matching import MatchResult, best_match, build_signals
from .parser import MediaKind, ParsedName
from .planner import Plan, build_book_plan, build_plan
from .scanner import ScannedFile
from .scoring import Decision, Policy
from .template import PRESETS

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
"""Fichiers planifies par lot. Aucun effet sur la vitesse — le semaphore borne
deja la concurrence — mais evite de tenir mille taches en memoire pour n'en
executer que six."""

MAX_CONCURRENCY = 6
"""Requetes simultanees vers les fournisseurs. TMDB tolere environ 50 requetes
par seconde ; on reste tres en dessous, la lenteur d'un scan n'etant jamais un
probleme alors qu'un bannissement en est un."""


class Pipeline:
    """Assemble les etapes. Instancie pour la duree d'un scan, puis ferme."""

    def __init__(
        self,
        *,
        tmdb: TMDBProvider | None,
        anilist: AniListProvider | None,
        library_root: Path,
        templates: dict[str, str],
        destination_for=None,
        policy: Policy,
        ai=None,
        ai_batch_size: int = 12,
        ai_threshold: float = 0.80,
        memory=None,
        known_titles: set[str] | None = None,
    ) -> None:
        self._tmdb = tmdb
        self._anilist = anilist
        self._library_root = library_root
        self._templates = templates
        # Fonction (kind, taille) -> dossier de destination. Injectee plutot que
        # codee ici : le pipeline n'a pas a connaitre les preferences, et cela
        # rend le routage par taille testable sans configuration.
        self._destination_for = destination_for or (lambda kind, size: library_root)
        self._policy = policy
        self._ai = ai
        self._ai_batch_size = max(1, ai_batch_size)
        self._ai_threshold = ai_threshold
        self._memory = memory
        # Titres deja presents en bibliotheque. Ils comptent comme voisins pour
        # la deduction de franchise : sans eux, importer « Star Trek: Picard »
        # seul ne le rangerait pas avec les autres Star Trek deja la.
        self._known_titles = known_titles or set()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def aclose(self) -> None:
        for provider in (self._tmdb, self._anilist):
            if provider is not None:
                await provider.aclose()

    async def _candidates(self, scanned: ScannedFile) -> list[Candidate]:
        """Interroge les fournisseurs pertinents pour ce type de media.

        Les deux sont interroges en parallele pour les animes : c'est
        justement leur ACCORD qui constitue le signal fort, il faut donc les
        deux reponses, pas la premiere qui arrive.
        """
        parsed = scanned.parsed
        title = parsed.title or scanned.probe.nfo_title or ""
        if not title:
            return []

        tasks = []
        if parsed.kind is MediaKind.MOVIE:
            if self._tmdb:
                tasks.append(self._tmdb.search_movie(title, parsed.year))
            if self._anilist:
                tasks.append(self._anilist.search_movie(title, parsed.year))
        elif parsed.kind is MediaKind.ANIME:
            if self._anilist:
                tasks.append(self._anilist.search_anime(title, parsed.year))
            if self._tmdb:
                tasks.append(self._tmdb.search_series(title, parsed.year))
        else:
            if self._tmdb:
                tasks.append(self._tmdb.search_series(title, parsed.year))
            if self._anilist:
                # Beaucoup d'animes sont nommes comme des series classiques :
                # interroger AniList aussi evite de les manquer.
                tasks.append(self._anilist.search_anime(title, parsed.year))

        if not tasks:
            return []

        results = await asyncio.gather(*tasks, return_exceptions=True)
        candidates: list[Candidate] = []
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("fournisseur en erreur : %s", type(result).__name__)
                continue
            candidates.extend(result)
        return candidates

    async def _enrich(self, scanned: ScannedFile, candidate: Candidate) -> bool | None:
        """Complete le candidat retenu et renvoie le signal ``episode_match``.

        Deux appels supplementaires, faits uniquement sur le candidat GAGNANT :
        les lancer sur tous les candidats multiplierait le cout par dix pour
        une information dont on ne se sert que sur un seul.
        """
        if self._tmdb is None or candidate.provider != "tmdb":
            return None

        parsed = scanned.parsed

        if parsed.kind is MediaKind.MOVIE:
            collection = await self._tmdb.get_collection(candidate.external_id)
            if collection:
                candidate.extra["collection"] = collection
            return None

        season = parsed.season if parsed.season is not None else scanned.probe.container_season
        episode = parsed.episode if parsed.episode is not None else scanned.probe.container_episode

        # Numerotation absolue : « One Piece 1088 » doit devenir S21E13, sinon
        # le fichier est range sous un numero que Jellyfin ne sait relier a
        # aucune saison. La conversion cumule les episodes saison par saison.
        if season is None and episode is None and parsed.absolute_episode is not None:
            resolved = await self._tmdb.resolve_absolute(
                candidate.external_id, parsed.absolute_episode
            )
            if resolved is not None:
                season, episode = resolved
                candidate.extra["resolved_season"] = season
                candidate.extra["resolved_episode"] = episode

        if season is None or episode is None:
            return None

        title = await self._tmdb.get_episode_title(candidate.external_id, season, episode)
        if title:
            candidate.extra["episode_title"] = title
            return True
        # Absence de reponse = la saison ou l'episode n'existe pas chez ce
        # candidat, donc l'identification est douteuse. C'est un signal, pas
        # une panne.
        return False

    def _remembered_candidate(self, scanned: ScannedFile):
        """Decision humaine deja prise pour ce titre, transformee en candidat."""
        if self._memory is None:
            return None
        title = scanned.parsed.title or scanned.probe.nfo_title or ""
        if not title:
            return None
        return self._memory.recall(_kind_key(scanned.parsed.kind), title)

    async def _recall(self, scanned: ScannedFile) -> Plan | None:
        decision = self._remembered_candidate(scanned)
        if decision is None:
            return None

        candidate = Candidate(
            provider=decision.provider,
            external_id=decision.external_id,
            title=decision.title,
            year=decision.year,
            poster_url=decision.poster_url,
            popularity=1.0,
        )
        episode_match = await self._enrich(scanned, candidate)
        signals = build_signals(
            scanned.parsed, scanned.probe, candidate, [candidate], episode_match=episode_match
        )
        match = MatchResult(candidate=candidate, signals=signals, similarity=1.0)

        kind = _kind_key(scanned.parsed.kind)
        plan = build_plan(
            scanned,
            match,
            template=self._templates.get(kind, ""),
            destination_root=self._destination_for(kind, scanned.size_bytes),
            policy=self._policy,
        )
        if plan.destination is not None:
            plan.decision = Decision.AUTO
            plan.score = 1.0
            plan.manual = True
            plan.identified_by = "memoire"
            plan.reasons = [
                f"identification memorisee : {decision.title}"
                + (f" ({decision.year})" if decision.year else ""),
                "tu as deja tranche pour ce titre, la question n'est pas reposee",
            ]
            self._memory.note_hit(kind, decision.title_key)
        return plan

    async def _plan_with_match(self, scanned: ScannedFile):
        """Plan ET correspondance retenue.

        La correspondance sert au second rendu : la franchise d'une serie ne se
        connait qu'une fois TOUS les titres du lot resolus, et il faut alors
        pouvoir refabriquer la destination sans relancer les appels reseau.
        """
        async with self._semaphore:
            try:
                # Un livre ne passe par AUCUN fournisseur. Le fichier fait
                # autorite — un EPUB porte le manifeste renseigne par son
                # editeur — et interroger une base pour confirmer ce qu'on a
                # deja sous la main serait payer un appel reseau pour rien.
                if scanned.parsed.kind is MediaKind.BOOK:
                    return self._render_book(scanned), None

                # Une decision deja prise court-circuite tout : ni recherche,
                # ni score, ni arbitrage. C'est ce qui fait qu'une file de
                # revue converge vers le vide au lieu de reposer eternellement
                # les memes questions.
                if (remembered := await self._recall(scanned)) is not None:
                    return remembered, None

                candidates = await self._candidates(scanned)
                match = best_match(scanned.parsed, scanned.probe, candidates)

                episode_match: bool | None = None
                if match is not None:
                    episode_match = await self._enrich(scanned, match.candidate)
                    # Le match est recalcule pour integrer episode_match et les
                    # champs ajoutes par l'enrichissement.
                    match = best_match(
                        scanned.parsed,
                        scanned.probe,
                        candidates,
                        episode_match=episode_match,
                    )

                return self._render(scanned, match, candidates), match
            except Exception as exc:
                # Filet de securite : un fichier pathologique ne doit pas faire
                # perdre le travail deja fait sur les precedents.
                logger.exception("echec du plan pour %s", scanned.path.name)
                return Plan(
                    id="error",
                    source=scanned.path,
                    destination=None,
                    kind=_kind_key(scanned.parsed.kind),
                    score=0.0,
                    decision=Decision.REJECT,
                    reasons=[f"erreur interne : {type(exc).__name__}"],
                    error=str(exc),
                ), None

    def _render_book(self, scanned: ScannedFile) -> Plan:
        return build_book_plan(
            scanned,
            template=self._templates.get("book", "") or PRESETS["jellyfin"]["book"],
            destination_root=self._destination_for("book", scanned.size_bytes),
        )

    def _render(self, scanned: ScannedFile, match, candidates) -> Plan:
        """Fabrique le plan a partir d'une correspondance deja etablie.

        Isole pour pouvoir etre rejoue : le second rendu de franchise ne
        change qu'une valeur du gabarit, il ne doit rien redemander au reseau.
        """
        kind = _kind_key(scanned.parsed.kind)
        return build_plan(
            scanned,
            match,
            template=self._templates.get(kind, ""),
            destination_root=self._destination_for(kind, scanned.size_bytes),
            policy=self._policy,
            all_candidates=candidates or [],
        )

    async def replan_with(self, scanned: ScannedFile, chosen: Candidate, previous: Plan) -> Plan:
        """Reconstruit un plan autour d'un candidat impose par l'utilisateur.

        Le score n'est pas recalcule : un choix humain explicite n'est pas une
        hypothese a evaluer. Le repasser au calcul reviendrait a douter de la
        personne qui vient de trancher — et sur un cas d'homonymie, le calcul
        avait deja montre qu'il ne savait pas.

        L'ancien candidat rejoint les alternatives : changer d'avis doit rester
        possible sans relancer un scan.
        """
        episode_match = await self._enrich(scanned, chosen)
        signals = build_signals(
            scanned.parsed, scanned.probe, chosen, [chosen], episode_match=episode_match
        )
        match = MatchResult(candidate=chosen, signals=signals, similarity=1.0)

        others = [
            c
            for c in previous.alternatives
            if (c.provider, c.external_id) != (chosen.provider, chosen.external_id)
        ]
        if previous.provider and previous.external_id:
            evince = next(
                (
                    c
                    for c in previous.alternatives
                    if (c.provider, c.external_id) == (previous.provider, previous.external_id)
                ),
                None,
            )
            if evince is None:
                # L'ancien gagnant n'etait pas dans les alternatives (il en
                # etait exclu) : on l'y remet pour pouvoir revenir en arriere.
                others = [
                    Candidate(
                        provider=previous.provider,
                        external_id=previous.external_id,
                        title=previous.title,
                        year=previous.year,
                    ),
                    *others,
                ]

        # Le type du CANDIDAT CHOISI, pas celui que le parseur avait lu. C'est
        # tout l'objet du geste : « The Vampire Diaries » pris pour un film se
        # corrige en choisissant la serie, et le ranger malgre tout avec le
        # gabarit des films annulerait la correction qu'on vient de faire.
        kind = (
            chosen.kind
            if chosen.kind in ("movie", "episode", "anime")
            else _kind_key(scanned.parsed.kind)
        )
        plan = build_plan(
            scanned,
            match,
            template=self._templates.get(kind, ""),
            destination_root=self._destination_for(kind, scanned.size_bytes),
            policy=self._policy,
        )
        plan.kind = kind
        plan.alternatives = others[:8]
        plan.manual = True
        plan.identified_by = "manuel"

        # Une serie sans numero d'episode ne se range pas : le gabarit produirait
        # « Saison  / Titre - SE », un chemin que personne ne veut. On le DIT au
        # lieu de le fabriquer, et le fichier reste a arbitrer.
        if kind in ("episode", "anime") and scanned.parsed.episode is None:
            if scanned.parsed.absolute_episode is None:
                plan.decision = Decision.REVIEW
                plan.reasons = [
                    *plan.reasons,
                    "aucun numero d'episode dans le nom du fichier : renomme-le en "
                    "« ... S01E02 ... » ou range-le a la main",
                ]

        if plan.destination is not None:
            plan.decision = Decision.AUTO
            plan.score = 1.0
            plan.reasons = [
                f"choisi manuellement : {chosen.title}"
                + (f" ({chosen.year})" if chosen.year else ""),
                "le score automatique ne s'applique pas a un choix humain",
            ]
        return plan

    async def plan_all(
        self,
        files: list[ScannedFile],
        on_progress: Callable[[int, int, str], None] | None = None,
        on_plan: Callable[[Plan], None] | None = None,
    ) -> list[Plan]:
        """Planifie un lot. Les fichiers deja ranges sont ignores.

        Les rescanner produirait des plans « deplacer X vers X » : du bruit
        dans la file de revue, et des entrees vides au journal d'annulation.

        ``on_plan`` recoit chaque plan des qu'il est pret, sans attendre la fin
        du lot. Sur un millier de fichiers, attendre la fin signifiait plusieurs
        minutes d'ecran vide alors que les premiers resultats etaient
        exploitables depuis longtemps. Le plan est publie deux fois quand la
        seconde passe l'ameliore : l'identifiant etant derive de la source, le
        second remplace le premier au lieu de s'y ajouter.
        """
        todo = [f for f in files if not f.in_library and f.skipped_reason is None]
        total = len(todo)
        done = 0

        matched: dict[str, object] = {}

        async def one(scanned: ScannedFile) -> Plan:
            nonlocal done
            plan, match = await self._plan_with_match(scanned)
            if match is not None:
                matched[plan.id] = (scanned, match)
            done += 1
            if on_plan:
                on_plan(plan)
            if on_progress:
                # Le nom du fichier TERMINE, pas celui en cours : avec six
                # taches en parallele, « en cours » n'aurait pas de sens unique.
                on_progress(done, total, scanned.path.name)
            return plan

        if on_progress:
            on_progress(0, total, "")

        # Traitement par lots plutot qu'un gather geant : sur une bibliotheque
        # de plus de mille fichiers, creer autant de taches d'un coup immobilise
        # de la memoire pour rien — le semaphore n'en laisse tourner que six a
        # la fois de toute facon. Le lot borne la pression sans rien ralentir.
        plans: list[Plan] = []
        for start in range(0, total, BATCH_SIZE):
            chunk = todo[start : start + BATCH_SIZE]
            plans.extend(await asyncio.gather(*(one(f) for f in chunk)))

        # --- Regroupement par franchise ------------------------------------
        #
        # Une franchise ne se voit qu'une fois TOUS les titres du lot resolus :
        # « Star Trek » tout court n'a rien d'une franchise tant qu'on ignore
        # que « Star Trek: Discovery » existe a cote. D'ou ce second rendu, qui
        # ne redemande rien au reseau et ne change qu'une valeur du gabarit.
        plans = self._group_franchises(plans, matched)

        if self._ai is not None:
            # La seconde passe n'a pas d'avancement fin : c'est un ou deux
            # appels groupes. On annonce la phase plutot que de figer la barre.
            if on_progress:
                on_progress(done, total, "seconde passe IA…")
            improved = await self._second_pass(todo, plans)
            if on_plan:
                for before, after in zip(plans, improved, strict=True):
                    if after is not before:
                        on_plan(after)
            plans = improved

        return plans

    def _group_franchises(self, plans: list[Plan], matched: dict) -> list[Plan]:
        """Range les series d'une meme franchise sous un dossier commun.

        TMDB ne declare aucun lien entre « Star Trek », « Star Trek: Discovery »
        et « Star Trek: Picard » : ce sont trois series sans rapport pour lui.
        La franchise se deduit du sous-titre, mais la deduction n'est retenue
        que si une AUTRE serie la partage — un dossier de franchise a un seul
        element n'est pas un regroupement, c'est un niveau de plus a traverser.

        Les titres deja presents en bibliotheque comptent parmi les voisins :
        sans eux, importer « Star Trek: Picard » seul ne le rangerait pas avec
        les autres Star Trek deja la.
        """
        titles = {p.title for p in plans if p.title} | self._known_titles
        if not titles:
            return plans

        updated = list(plans)
        for index, plan in enumerate(plans):
            if plan.kind not in ("episode", "anime") or not plan.title:
                continue
            entry = matched.get(plan.id)
            if entry is None:
                continue

            franchise = franchise_for(plan.title, sorted(titles))
            scanned, match = entry
            if not franchise or match.candidate.extra.get("collection") == franchise:
                continue

            match.candidate.extra["collection"] = franchise
            rebuilt = self._render(scanned, match, plan.alternatives)
            # Le second rendu ne doit rien degrader : il ne touche qu'au
            # chemin. Si le gabarit n'utilise pas {collection}, la destination
            # est identique et l'echange est sans effet.
            if rebuilt.destination is not None:
                updated[index] = rebuilt

        return updated

    # --- Seconde passe assistee par IA --------------------------------------

    async def _second_pass(self, files: list[ScannedFile], plans: list[Plan]) -> list[Plan]:
        """Reprend les fichiers que le deterministe n'a pas tranches.

        JAMAIS le tout-venant : le parseur et les fournisseurs traitent la
        grande majorite des fichiers a cout nul. On ne paie un appel que pour
        ce qui allait de toute facon demander une intervention humaine.
        """
        pending = [
            (index, f)
            for index, (f, p) in enumerate(zip(files, plans, strict=True))
            # Seuil dedie plutot que « tout ce qui n'est pas automatique » :
            # entre le seuil IA et le seuil d'application, le score est deja
            # bon et une revue humaine suffit. Payer un appel la n'apporterait
            # rien.
            if p.decision is not Decision.AUTO and p.score < self._ai_threshold
        ]
        if not pending:
            return plans

        logger.info("resolveur IA : %s fichier(s) ambigus", len(pending))
        updated = list(plans)

        for start in range(0, len(pending), self._ai_batch_size):
            chunk = pending[start : start + self._ai_batch_size]
            items = [
                AmbiguousItem(
                    index=index,
                    filename=f.path.name,
                    parent_folder=f.path.parent.name,
                    parsed=f.parsed,
                    candidates=[],
                )
                for index, f in chunk
            ]

            try:
                # Le SDK Anthropic est synchrone : l'appeler directement
                # bloquerait la boucle d'evenements et figerait tout le scan.
                proposals = await asyncio.to_thread(self._ai.resolve, items)
            except Exception:
                logger.exception("resolveur IA indisponible ; lot laisse en l'etat")
                continue

            for index, scanned in chunk:
                proposal = proposals.get(index)
                if proposal is None or not proposal.title:
                    continue
                better = await self._replan_with(scanned, proposal)
                # On ne remplace que si la seconde passe fait MIEUX : une
                # proposition moins bonne que la lecture deterministe ne doit
                # pas degrader un resultat deja acquis.
                if better is not None and better.score > updated[index].score:
                    updated[index] = better

        return updated

    async def _replan_with(self, scanned: ScannedFile, proposal: AIProposal) -> Plan | None:
        """Rejoue l'identification avec le titre propose par le modele.

        Le modele ne fournit pas la reponse : il fournit une meilleure REQUETE.
        Les candidats viennent toujours des fournisseurs, et sa confiance entre
        dans le scoring comme un plafond — voir ``compute_score``.
        """
        corrected = ParsedName(
            raw=scanned.parsed.raw,
            title=proposal.title,
            kind=_kind_from(proposal.kind, scanned.parsed.kind),
            year=proposal.year or scanned.parsed.year,
            season=proposal.season if proposal.season is not None else scanned.parsed.season,
            episode=proposal.episode if proposal.episode is not None else scanned.parsed.episode,
            absolute_episode=(
                proposal.absolute_episode
                if proposal.absolute_episode is not None
                else scanned.parsed.absolute_episode
            ),
            resolution=scanned.parsed.resolution,
            source=scanned.parsed.source,
            codec=scanned.parsed.codec,
            language=scanned.parsed.language,
            fansub_group=scanned.parsed.fansub_group,
            quality=scanned.parsed.quality,
            signals=[*scanned.parsed.signals, "titre propose par le resolveur IA"],
        )

        rescanned = replace(scanned, parsed=corrected)

        async with self._semaphore:
            candidates = await self._candidates(rescanned)

        match = best_match(corrected, rescanned.probe, candidates)
        if match is None:
            return None

        match.signals.ai_confidence = proposal.confidence

        plan = build_plan(
            rescanned,
            match,
            template=self._templates.get(_kind_key(corrected.kind), ""),
            destination_root=self._destination_for(_kind_key(corrected.kind), rescanned.size_bytes),
            policy=self._policy,
        )
        # La fiche vient bien du fournisseur, mais c'est le modele qui a trouve
        # QUOI lui demander. Afficher « TMDB » masquerait le maillon dont on
        # veut justement se mefier — et deux plans a 82 % ne se valent pas selon
        # qu'ils viennent d'une lecture de nom ou d'une supposition de modele.
        plan.identified_by = "ia"
        return plan


def _kind_from(value: str, fallback: MediaKind) -> MediaKind:
    try:
        return MediaKind(value)
    except ValueError:
        return fallback


def _kind_key(kind: MediaKind) -> str:
    return {
        MediaKind.MOVIE: "movie",
        MediaKind.EPISODE: "episode",
        MediaKind.ANIME: "anime",
        MediaKind.BOOK: "book",
    }.get(kind, "movie")
