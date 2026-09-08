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
from dataclasses import replace
from pathlib import Path

from ..providers.anilist import AniListProvider
from ..providers.base import Candidate
from ..providers.tmdb import TMDBProvider
from .ai_resolver import AIProposal, AmbiguousItem
from .matching import MatchResult, best_match, build_signals
from .parser import MediaKind, ParsedName
from .planner import Plan, build_plan
from .scanner import ScannedFile
from .scoring import Decision, Policy

logger = logging.getLogger(__name__)

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
        policy: Policy,
        ai=None,
        ai_batch_size: int = 12,
    ) -> None:
        self._tmdb = tmdb
        self._anilist = anilist
        self._library_root = library_root
        self._templates = templates
        self._policy = policy
        self._ai = ai
        self._ai_batch_size = max(1, ai_batch_size)
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

    async def plan_one(self, scanned: ScannedFile) -> Plan:
        async with self._semaphore:
            try:
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

                template = self._templates.get(_kind_key(scanned.parsed.kind), "")
                return build_plan(
                    scanned,
                    match,
                    template=template,
                    library_root=self._library_root,
                    policy=self._policy,
                    all_candidates=candidates,
                )
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

        plan = build_plan(
            scanned,
            match,
            template=self._templates.get(_kind_key(scanned.parsed.kind), ""),
            library_root=self._library_root,
            policy=self._policy,
        )
        plan.alternatives = others[:8]
        plan.manual = True

        if plan.destination is not None:
            plan.decision = Decision.AUTO
            plan.score = 1.0
            plan.reasons = [
                f"choisi manuellement : {chosen.title}"
                + (f" ({chosen.year})" if chosen.year else ""),
                "le score automatique ne s'applique pas a un choix humain",
            ]
        return plan

    async def plan_all(self, files: list[ScannedFile]) -> list[Plan]:
        """Planifie un lot. Les fichiers deja ranges sont ignores.

        Les rescanner produirait des plans « deplacer X vers X » : du bruit
        dans la file de revue, et des entrees vides au journal d'annulation.
        """
        todo = [f for f in files if not f.in_library and f.skipped_reason is None]
        plans = list(await asyncio.gather(*(self.plan_one(f) for f in todo)))

        if self._ai is not None:
            plans = await self._second_pass(todo, plans)

        return plans

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
            if p.decision is not Decision.AUTO
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

        return build_plan(
            rescanned,
            match,
            template=self._templates.get(_kind_key(corrected.kind), ""),
            library_root=self._library_root,
            policy=self._policy,
        )


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
    }.get(kind, "movie")
