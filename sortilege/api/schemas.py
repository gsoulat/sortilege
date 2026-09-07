"""Formes de reponse exposees a l'interface.

Separees des dataclasses du domaine a dessein : l'UI ne doit pas dependre de la
structure interne, et on ne veut pas exposer de chemin absolu de conteneur, qui
ne signifie rien pour l'utilisateur et renseigne inutilement sur l'hote.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..core.scanner import ScannedFile, ScanResult


class ProbeOut(BaseModel):
    duration_seconds: float | None = None
    resolution: str | None = None
    video_codec: str | None = None
    audio_languages: list[str] | None = None
    container_title: str | None = None
    declared_id: str | None = None
    probed: bool = False


class ParsedOut(BaseModel):
    title: str
    kind: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    absolute_episode: int | None = None
    resolution: str | None = None
    source: str | None = None
    codec: str | None = None
    language: str | None = None
    fansub_group: str | None = None
    quality: float
    signals: list[str]


class ScannedFileOut(BaseModel):
    relative_path: str
    filename: str
    size_bytes: int
    parsed: ParsedOut
    probe: ProbeOut
    skipped_reason: str | None = None
    in_library: bool = False


class ScanOut(BaseModel):
    total: int
    skipped: int
    errors: list[str]
    files: list[ScannedFileOut]
    deep: bool


def _declared_id(file: ScannedFile) -> str | None:
    p = file.probe
    if p.tmdb_id:
        return f"tmdb:{p.tmdb_id}"
    if p.imdb_id:
        return f"imdb:{p.imdb_id}"
    if p.tvdb_id:
        return f"tvdb:{p.tvdb_id}"
    return None


def to_out(file: ScannedFile) -> ScannedFileOut:
    p = file.parsed
    probe = file.probe
    return ScannedFileOut(
        relative_path=file.relative_path,
        filename=file.path.name,
        size_bytes=file.size_bytes,
        skipped_reason=file.skipped_reason,
        in_library=file.in_library,
        parsed=ParsedOut(
            title=p.title,
            kind=str(p.kind),
            year=p.year,
            season=p.season,
            episode=p.episode,
            absolute_episode=p.absolute_episode,
            # La resolution mesuree prime sur celle annoncee dans le nom.
            resolution=probe.resolution_label or p.resolution,
            source=p.source,
            codec=probe.video_codec or p.codec,
            language=p.language,
            fansub_group=p.fansub_group,
            quality=p.quality,
            signals=p.signals,
        ),
        probe=ProbeOut(
            duration_seconds=probe.duration_seconds,
            resolution=probe.resolution_label,
            video_codec=probe.video_codec,
            audio_languages=probe.audio_languages,
            container_title=probe.container_title,
            declared_id=_declared_id(file),
            probed=probe.probed,
        ),
    )


def scan_to_out(result: ScanResult, deep: bool) -> ScanOut:
    return ScanOut(
        total=result.total,
        skipped=result.skipped,
        errors=result.errors,
        files=[to_out(f) for f in result.files],
        deep=deep,
    )
