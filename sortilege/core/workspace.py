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
from .scanner import ScannedFile
from .scoring import Decision
from .store import title_key


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
        entry = touch([entry_key(title=scanned.parsed.title)], scanned.parsed.title)
        entry.pending.unplanned.append(scanned)
        entry.kind = entry.kind or str(scanned.parsed.kind)

    return sorted(entries.values(), key=lambda e: e.sort_rank())


def summarize(entries: list[WorkspaceEntry]) -> dict[str, int]:
    """Les compteurs de la barre d'action, calcules une fois pour toutes."""
    return {
        "works": len(entries),
        "owned_files": sum(e.owned.file_count for e in entries if e.owned),
        "ready": sum(len(e.pending.ready) for e in entries),
        "review": sum(len(e.pending.review) for e in entries),
        "rejected": sum(len(e.pending.rejected) for e in entries),
        "unplanned": sum(len(e.pending.unplanned) for e in entries),
        "missing": sum(e.owned.missing_count for e in entries if e.owned),
        "duplicates": sum(len(e.owned.duplicates) for e in entries if e.owned),
    }
