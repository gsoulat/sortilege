"""Vue unique : ce qu'on possede et ce qui attend d'etre range, ensemble.

Trois ecrans montraient trois moities du meme objet — les fichiers a ranger,
la collection rangee, la file d'arbitrage — et obligeaient a faire des
allers-retours pour repondre a une question simple : « ou en est cette
serie ? ». La reponse tient dans une seule ligne des lors qu'on accepte de
melanger les deux etats d'une meme oeuvre.

Le point delicat est le RAPPROCHEMENT. Un fichier deja range porte le titre
officiel, ecrit par nos soins (« Avatar Le dernier maitre de l'air ») ; un
fichier en attente porte celui de la release (« Avatar The Last Airbender »),
puis, une fois identifie, celui du fournisseur avec sa ponctuation d'origine
(« Avatar : Le dernier maitre de l'air »). Trois ecritures d'une meme oeuvre,
qui doivent tomber dans la meme ligne — sans quoi la vue unique afficherait
trois entrees et serait pire que les trois ecrans qu'elle remplace.

La cle de rapprochement est donc normalisee, et l'identifiant du fournisseur
prime des qu'il est connu : deux oeuvres homonymes restent distinctes, et deux
ecritures de la meme oeuvre se rejoignent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .collection import Work
from .planner import Plan
from .quality import QualitySettings
from .reencode import Candidate, audit
from .scanner import ScannedFile
from .scoring import Decision
from .store import title_key

HEAVY_RATIO = 2.0
"""A partir de combien de fois la mediane une oeuvre merite d'etre signalee.

Deux fois le poids habituel pour une qualite identique, c'est ce qui distingue
un remux d'un encodage courant. En dessous, l'ecart s'explique par la duree ou
la scene et ne justifie pas de re-telecharger."""


@dataclass
class Pending:
    """Ce qui attend d'etre range pour une oeuvre donnee."""

    ready: list[Plan] = field(default_factory=list)
    """Assez sur pour partir sans qu'on regarde."""

    review: list[Plan] = field(default_factory=list)
    """Demande un arbitrage : l'identification n'est pas tranchee."""

    rejected: list[Plan] = field(default_factory=list)
    """Trop douteux pour etre propose."""

    unplanned: list[ScannedFile] = field(default_factory=list)
    """Scanne, pas encore passe par le calcul de plan."""

    @property
    def total(self) -> int:
        return len(self.ready) + len(self.review) + len(self.rejected) + len(self.unplanned)


@dataclass(slots=True)
class HeavyFile:
    """Un fichier nettement plus lourd que ses semblables."""

    relative_path: str
    size_bytes: int
    ratio: float
    """Rapport a la mediane des fichiers du meme type. 2.5 = deux fois et demie
    le poids habituel."""


@dataclass
class WorkspaceEntry:
    """Une oeuvre, dans ses deux etats a la fois."""

    key: str
    title: str
    kind: str = ""
    year: int | None = None
    poster_url: str = ""
    owned: Work | None = None
    pending: Pending = field(default_factory=Pending)

    heavy_files: list[HeavyFile] = field(default_factory=list)
    """Les fichiers precis qui font le surpoids.

    « 2,5 fois le poids habituel » sur une serie de neuf episodes ne dit pas QUEL
    episode est en cause. Sans le nom du fichier, l'avertissement se regarde
    sans rien pouvoir en faire — et c'est justement au fichier qu'on agit.
    """

    off_strategy: list[Candidate] = field(default_factory=list)
    """Les fichiers qui ne respectent pas la strategie choisie pour leur type.

    Distinct du surpoids : un episode peut peser le double des autres tout en
    respectant la strategie (une scene chargee, un episode double), et un
    fichier parfaitement dans la moyenne peut etre en 2160p alors que la
    strategie dit 1080p.
    """

    heaviness: float = 0.0
    """Taille par fichier RAPPORTEE a la mediane des oeuvres du meme type.

    1.0 = dans la norme, 2.5 = deux fois et demie plus lourd que l'habitude.
    La taille brute ne dirait rien : une serie de trente episodes pese
    forcement plus qu'un film. Ce qui se compare, c'est le poids d'UN fichier.

    La mediane et non la moyenne : la moyenne serait tiree vers le haut par les
    quelques remux enormes qu'on cherche justement a reperer, et le seuil se
    deplacerait avec eux.
    """

    @property
    def bytes_per_file(self) -> int:
        if self.owned is None or not self.owned.file_count:
            return 0
        return self.owned.total_bytes // self.owned.file_count

    @property
    def is_new(self) -> bool:
        """Rien en bibliotheque : c'est une arrivee, pas un complement."""
        return self.owned is None

    def sort_rank(self) -> tuple:
        """Ce qui demande une action passe devant ce qui est au repos.

        L'ordre alphabetique serait le plus previsible, mais il enterre les
        quelques lignes actionnables sous des centaines de lignes inertes —
        exactement ce que la vue unique cherche a eviter.
        """
        actionable = bool(self.pending.ready or self.pending.review)
        gaps = self.owned.has_gaps if self.owned else False
        dupes = bool(self.owned and self.owned.duplicates)
        return (
            not actionable,
            not self.pending.unplanned,
            not (gaps or dupes),
            self.title.casefold(),
        )


def entry_key(*, provider: str = "", external_id: str = "", title: str = "") -> str:
    """Cle de rapprochement d'une oeuvre.

    L'identifiant du fournisseur prime : c'est la seule donnee qui distingue
    sans ambiguite « Dark Matter » 2015 de « Dark Matter » 2024. A defaut, le
    titre normalise — insensible a la casse, aux accents et a la ponctuation —
    rapproche les differentes ecritures d'un meme titre.
    """
    if provider and external_id:
        return f"{provider}:{external_id}"
    normalized = title_key(title)
    stripped = "".join(c if c.isalnum() else " " for c in normalized)
    # Les espaces sont ecrases APRES le retrait de la ponctuation : sinon
    # « Avatar : Le dernier maitre » laisse une double espace la ou etaient les
    # deux-points, et ne rejoint pas « Avatar Le dernier maitre ». C'est
    # exactement le rapprochement que cette cle existe pour faire.
    return " ".join(stripped.split()) or "?"


def _keys_for(provider: str, external_id: str, title: str) -> list[str]:
    """Toutes les cles sous lesquelles une meme oeuvre peut se presenter.

    Un plan connait l'identifiant du fournisseur ; une oeuvre lue sur le disque
    ne connait que son titre, puisqu'elle n'est qu'un ensemble de fichiers
    ranges. Les rapprocher demande donc de chercher sous les DEUX cles, la plus
    precise d'abord — sans quoi la meme serie apparaitrait deux fois : une
    ligne « ce que tu possedes » et une ligne « ce qui arrive », ce que la vue
    unique existe precisement pour eviter.
    """
    keys = []
    if provider and external_id:
        keys.append(f"{provider}:{external_id}")
    if title:
        keys.append(entry_key(title=title))
    return keys or ["?"]


def build(
    works: list[Work],
    plans: list[Plan],
    pending: list[ScannedFile],
    quality_settings: QualitySettings | None = None,
) -> list[WorkspaceEntry]:
    """Assemble la vue. Ne fait aucun appel reseau ni disque.

    Les trois entrees correspondent aux trois ecrans remplaces :

    - ``works`` : l'index de bibliotheque, deja enrichi de ses affiches et de
      ses episodes manquants. On le consomme, on ne le recalcule pas.
    - ``plans`` : ce qui a deja ete identifie, y compris pendant que le calcul
      tourne encore.
    - ``pending`` : les fichiers scannes qui n'ont pas encore de plan. C'est
      cette troisieme liste qui permet d'afficher au fur et a mesure : une
      ligne existe des le scan, et se precise quand son plan arrive.
    """
    entries: dict[str, WorkspaceEntry] = {}
    # Toute cle connue -> cle canonique de l'entree. Une oeuvre gagne des alias
    # au fur et a mesure qu'on en apprend : le titre d'abord, l'identifiant du
    # fournisseur des que le calcul l'a trouve.
    aliases: dict[str, str] = {}

    def touch(keys: list[str], title: str) -> WorkspaceEntry:
        canonical = next((aliases[k] for k in keys if k in aliases), keys[0])
        if canonical not in entries:
            entries[canonical] = WorkspaceEntry(key=canonical, title=title)
        for key in keys:
            aliases[key] = canonical
        return entries[canonical]

    # --- Ce qui est deja range ---------------------------------------------
    for work in works:
        entry = touch(_keys_for(work.provider, work.external_id, work.title), work.title)
        entry.owned = work
        entry.kind = entry.kind or work.kind
        entry.year = entry.year or work.year
        entry.poster_url = entry.poster_url or work.poster_url
        # Le titre de la bibliotheque est celui qu'on a ecrit nous-memes :
        # il fait foi sur celui d'une release.
        entry.title = work.title

    # --- Ce qui a un plan ---------------------------------------------------
    by_decision = {
        Decision.AUTO: "ready",
        Decision.REVIEW: "review",
        Decision.REJECT: "rejected",
    }
    for plan in plans:
        entry = touch(
            _keys_for(plan.provider, plan.external_id, plan.title),
            plan.title or "(non identifié)",
        )
        getattr(entry.pending, by_decision[plan.decision]).append(plan)
        entry.kind = entry.kind or plan.kind
        entry.year = entry.year or plan.year
        entry.poster_url = entry.poster_url or plan.poster_url
        if entry.owned is None and plan.title:
            entry.title = plan.title

    # --- Ce qui attend encore le calcul -------------------------------------
    for scanned in pending:
        # Aucun identifiant de fournisseur a ce stade : seul le titre lu par le
        # parseur est disponible, et il peut differer de celui qui sortira du
        # calcul. La ligne se recollera d'elle-meme au plan suivant.
        titre = scanned.parsed.title
        if titre:
            keys = [entry_key(title=titre)]
        else:
            # Le parseur n'a RIEN pu lire du nom — « 02x01 - Episode.avi » sans
            # dossier de serie au-dessus. Tous ces fichiers partageaient alors
            # la meme cle vide et se fondaient dans UNE entree sans libelle :
            # une ligne blanche a la place de cinquante fichiers bien reels.
            #
            # On les separe par leur chemin, et on affiche le nom du fichier.
            # C'est moins qu'un titre, mais c'est verifiable — et surtout ca
            # existe a l'ecran.
            titre = scanned.path.stem
            keys = [f"chemin:{scanned.path}"]
        entry = touch(keys, titre)
        entry.pending.unplanned.append(scanned)
        entry.kind = entry.kind or str(scanned.parsed.kind)

    tous = list(entries.values())
    _mark_heaviness(tous)
    _mark_heavy_files(tous)
    _mark_off_strategy(tous, quality_settings)
    return sorted(tous, key=lambda e: e.sort_rank())


def _mark_heaviness(entries: list[WorkspaceEntry]) -> None:
    """Situe chaque oeuvre par rapport au poids habituel de son type.

    Comparer un film a une serie n'aurait aucun sens ; on compare donc a
    l'interieur de chaque type. En dessous de trois oeuvres, on s'abstient : une
    mediane sur deux valeurs ne dit rien, et signaler un « anormal » sur une
    base pareille serait du bruit.
    """
    by_kind: dict[str, list[WorkspaceEntry]] = {}
    for entry in entries:
        if entry.owned is not None and entry.bytes_per_file:
            by_kind.setdefault(entry.kind or "?", []).append(entry)

    for group in by_kind.values():
        if len(group) < 3:
            continue
        sizes = sorted(e.bytes_per_file for e in group)
        middle = len(sizes) // 2
        median = sizes[middle] if len(sizes) % 2 else (sizes[middle - 1] + sizes[middle]) // 2
        if median <= 0:
            continue
        for entry in group:
            entry.heaviness = entry.bytes_per_file / median


def summarize(entries: list[WorkspaceEntry]) -> dict[str, int]:
    """Les compteurs de la barre d'action, calcules une fois pour toutes."""
    return {
        "works": len(entries),
        # Les deux onglets : ce qui attend dans la source, et ce qui est range.
        # Une meme oeuvre peut compter dans les deux — un episode en attente
        # d'une serie deja presente en bibliotheque.
        "source_works": sum(1 for e in entries if e.pending.total),
        "library_works": sum(1 for e in entries if e.owned),
        "owned_files": sum(e.owned.file_count for e in entries if e.owned),
        "ready": sum(len(e.pending.ready) for e in entries),
        "review": sum(len(e.pending.review) for e in entries),
        "rejected": sum(len(e.pending.rejected) for e in entries),
        "unplanned": sum(len(e.pending.unplanned) for e in entries),
        "missing": sum(e.owned.missing_count for e in entries if e.owned),
        "duplicates": sum(len(e.owned.duplicates) for e in entries if e.owned),
        "total_bytes": sum(e.owned.total_bytes for e in entries if e.owned),
        "heavy": sum(1 for e in entries if e.heaviness >= HEAVY_RATIO),
        "off_strategy": sum(len(e.off_strategy) for e in entries),
        "recoverable_bytes": sum(c.savings_bytes for e in entries for c in e.off_strategy),
    }


def _mark_heavy_files(entries: list[WorkspaceEntry]) -> None:
    """Designe les fichiers precis qui font le surpoids d'une oeuvre.

    La mediane se calcule ici sur les FICHIERS et non sur les oeuvres : la
    question posee est « quel episode est trop lourd », et un episode ne se
    compare pas a la moyenne d'une serie entiere.
    """
    tailles: dict[str, list[int]] = {}
    for entry in entries:
        if entry.owned is None:
            continue
        for refs in entry.owned.slots.values():
            for ref in refs:
                if ref.size_bytes > 0:
                    tailles.setdefault(entry.owned.kind or "?", []).append(ref.size_bytes)

    medianes = {kind: _median(v) for kind, v in tailles.items() if len(v) >= 3}

    for entry in entries:
        if entry.owned is None:
            continue
        mediane = medianes.get(entry.owned.kind or "?", 0)
        if mediane <= 0:
            continue
        lourds = [
            HeavyFile(ref.relative_path, ref.size_bytes, ref.size_bytes / mediane)
            for refs in entry.owned.slots.values()
            for ref in refs
            if ref.size_bytes >= HEAVY_RATIO * mediane
        ]
        entry.heavy_files = sorted(lourds, key=lambda f: f.size_bytes, reverse=True)


def _mark_off_strategy(entries: list[WorkspaceEntry], settings: QualitySettings | None) -> None:
    """Designe les fichiers qui ne respectent pas la strategie de leur type.

    On delegue a ``reencode.audit``, qui tient deja la regle : un fichier viole
    sa strategie s'il existe une resolution plus basse que la sienne que cette
    strategie classe mieux. Deux endroits pour une meme regle finiraient par
    diverger.
    """
    if settings is None:
        return
    for entry in entries:
        if entry.owned is not None:
            entry.off_strategy = audit([entry.owned], settings, min_savings_bytes=0)


def _median(valeurs: list[int]) -> int:
    ordonnees = sorted(valeurs)
    milieu = len(ordonnees) // 2
    if not ordonnees:
        return 0
    if len(ordonnees) % 2:
        return ordonnees[milieu]
    return (ordonnees[milieu - 1] + ordonnees[milieu]) // 2
