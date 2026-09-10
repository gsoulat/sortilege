"""Sauvegarde et relecture de l'etat de travail : scan et plans.

Un scan de mille fichiers coute plusieurs minutes de disque — ffprobe ouvre
chaque conteneur — et les plans qui en decoulent coutent des centaines
d'appels a TMDB. Les garder en memoire seule fait payer tout cela a chaque
redemarrage du conteneur, c'est-a-dire a chaque mise a jour d'image. Sur une
bibliotheque reelle, cela suffit a rendre l'outil penible.

Deux precautions gouvernent ce module :

**Un instantane est une VUE, pas une verite.** Entre l'enregistrement et la
relecture, un fichier a pu etre deplace, renomme ou supprime — par un autre
outil, ou par Sortilege lui-meme dans une session precedente. Rien n'est donc
tenu pour acquis : un plan relu est verifie au moment de l'appliquer, comme
n'importe quel plan frais. La reprise fait gagner du calcul, elle ne
court-circuite aucun controle.

**Un format qui ne se relit pas se jette.** Le schema porte un numero ; toute
divergence, tout champ manquant, toute valeur inattendue ramene a « pas
d'instantane » plutot qu'a une exception au demarrage. Perdre une reprise est
un desagrement, ne plus demarrer est une panne.

L'encodage est ecrit a la main plutot que derive des dataclasses : c'est
volontairement le seul endroit qui connait la forme sur disque, et une
evolution des structures internes s'y voit — au lieu de casser silencieusement
la relecture d'un ancien instantane.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..providers.base import Candidate
from .parser import MediaKind, ParsedName
from .planner import Plan
from .probe import FileProbe
from .scanner import MIN_SIZE_BYTES, ScannedFile, ScanResult
from .scoring import Decision

logger = logging.getLogger(__name__)

VERSION = 1
"""Numero de schema. A incrementer des qu'un champ change de sens.

Le lever plutot que tenter une migration : un instantane est reconstructible,
il ne merite pas le code qu'une migration couterait."""

SCAN_KEY = "scan"
PLANS_KEY = "plans"


# --- Encodage ---------------------------------------------------------------


def _parsed_out(p: ParsedName) -> dict:
    return {
        "raw": p.raw,
        "title": p.title,
        "kind": str(p.kind),
        "year": p.year,
        "season": p.season,
        "episode": p.episode,
        "episode_end": p.episode_end,
        "absolute_episode": p.absolute_episode,
        "resolution": p.resolution,
        "source": p.source,
        "codec": p.codec,
        "language": p.language,
        "release_group": p.release_group,
        "fansub_group": p.fansub_group,
        "quality": p.quality,
        "signals": list(p.signals),
    }


def _probe_out(p: FileProbe) -> dict:
    return {
        "duration_seconds": p.duration_seconds,
        "width": p.width,
        "height": p.height,
        "video_codec": p.video_codec,
        "audio_languages": p.audio_languages,
        "container_title": p.container_title,
        "container_show": p.container_show,
        "container_season": p.container_season,
        "container_episode": p.container_episode,
        "tmdb_id": p.tmdb_id,
        "imdb_id": p.imdb_id,
        "tvdb_id": p.tvdb_id,
        "nfo_title": p.nfo_title,
        "nfo_year": p.nfo_year,
        "probed": p.probed,
    }


def _candidate_out(c: Candidate) -> dict:
    return {
        "provider": c.provider,
        "external_id": c.external_id,
        "title": c.title,
        "original_title": c.original_title,
        "year": c.year,
        "popularity": c.popularity,
        "kind": c.kind,
        "poster_url": c.poster_url,
        "overview": c.overview,
    }


def scan_out(result: ScanResult, *, deep: bool) -> dict:
    return {
        "version": VERSION,
        "deep": deep,
        "skipped": result.skipped,
        "errors": list(result.errors),
        "files": [
            {
                "path": str(f.path),
                "size_bytes": f.size_bytes,
                "relative_path": f.relative_path,
                "in_library": f.in_library,
                "min_size_bytes": f.min_size_bytes,
                "parsed": _parsed_out(f.parsed),
                "probe": _probe_out(f.probe),
            }
            for f in result.files
        ],
    }


def plans_out(plans: list[Plan]) -> dict:
    return {
        "version": VERSION,
        "plans": [
            {
                "id": p.id,
                "source": str(p.source),
                "destination": str(p.destination) if p.destination else None,
                "kind": p.kind,
                "score": p.score,
                "decision": str(p.decision),
                "reasons": list(p.reasons),
                "title": p.title,
                "year": p.year,
                "provider": p.provider,
                "external_id": p.external_id,
                "poster_url": p.poster_url,
                "error": p.error,
                "companions": [[str(a), str(b)] for a, b in p.companions],
                "leftovers": [str(x) for x in p.leftovers],
                "alternatives": [_candidate_out(c) for c in p.alternatives],
                "manual": p.manual,
            }
            for p in plans
        ],
    }


# --- Decodage ---------------------------------------------------------------


class SnapshotError(ValueError):
    """Instantane illisible. Toujours rattrape : on repart de zero."""


def _require_version(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise SnapshotError("instantane malforme")
    if raw.get("version") != VERSION:
        raise SnapshotError(f"schema {raw.get('version')} ignore (attendu {VERSION})")
    return raw


def _parsed_in(d: dict) -> ParsedName:
    kind = d.get("kind")
    try:
        media_kind = MediaKind(kind)
    except ValueError as exc:
        raise SnapshotError(f"type de media inconnu : {kind}") from exc
    return ParsedName(
        raw=d["raw"],
        title=d["title"],
        kind=media_kind,
        year=d.get("year"),
        season=d.get("season"),
        episode=d.get("episode"),
        episode_end=d.get("episode_end"),
        absolute_episode=d.get("absolute_episode"),
        resolution=d.get("resolution"),
        source=d.get("source"),
        codec=d.get("codec"),
        language=d.get("language"),
        release_group=d.get("release_group"),
        fansub_group=d.get("fansub_group"),
        quality=d.get("quality", 0.0),
        signals=list(d.get("signals") or []),
    )


def scan_in(raw: object) -> tuple[ScanResult, bool]:
    """Reconstruit un scan. Leve ``SnapshotError`` si le format ne colle pas."""
    data = _require_version(raw)
    try:
        files = [
            ScannedFile(
                path=Path(f["path"]),
                size_bytes=f["size_bytes"],
                parsed=_parsed_in(f["parsed"]),
                probe=FileProbe(**f["probe"]),
                relative_path=f.get("relative_path", ""),
                in_library=f.get("in_library", False),
                # Champ ajoute apres coup : un instantane ecrit avant lui reste
                # lisible et retombe sur le plancher livre, ce qui vaut mieux
                # que jeter un scan de plusieurs minutes pour un entier.
                min_size_bytes=f.get("min_size_bytes", MIN_SIZE_BYTES),
            )
            for f in data["files"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError(f"scan illisible : {exc}") from exc

    result = ScanResult(
        files=files,
        skipped=data.get("skipped", 0),
        errors=list(data.get("errors") or []),
    )
    return result, bool(data.get("deep", True))


def plans_in(raw: object) -> list[Plan]:
    data = _require_version(raw)
    try:
        return [
            Plan(
                id=p["id"],
                source=Path(p["source"]),
                destination=Path(p["destination"]) if p.get("destination") else None,
                kind=p["kind"],
                score=p["score"],
                decision=Decision(p["decision"]),
                reasons=list(p.get("reasons") or []),
                title=p.get("title", ""),
                year=p.get("year"),
                provider=p.get("provider", ""),
                external_id=p.get("external_id", ""),
                poster_url=p.get("poster_url", ""),
                error=p.get("error"),
                companions=[(Path(a), Path(b)) for a, b in (p.get("companions") or [])],
                leftovers=[Path(x) for x in (p.get("leftovers") or [])],
                alternatives=[Candidate(**c) for c in (p.get("alternatives") or [])],
                manual=p.get("manual", False),
            )
            for p in data["plans"]
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotError(f"plans illisibles : {exc}") from exc
