"""Entretien de la bibliotheque : serveur multimedia, corbeille, renommage.

Trois fonctions sans rapport entre elles, sauf qu'elles ferment chacune une
boucle laissee ouverte :

- **Le serveur multimedia** n'apprenait jamais qu'un fichier venait d'arriver.
- **La corbeille** ne se vidait pas : Sortilege ne supprimant jamais, elle
  grossissait indefiniment et l'espace n'etait jamais recupere.
- **Ce qui etait deja range** ne suivait pas les changements de gabarit : le
  pipeline ignore deliberement les fichiers en bibliotheque.

Le vidage de la corbeille est le SEUL endroit de l'application qui supprime
reellement. Ses tests portent donc moins sur ce qu'il supprime que sur ce qu'il
refuse de toucher.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from sortilege.core.mediaserver import (
    MediaServerError,
    refresh_library,
    validate_server_url,
)
from sortilege.core.parser import parse
from sortilege.core.probe import FileProbe
from sortilege.core.renaming import rename_plans
from sortilege.core.scanner import ScannedFile
from sortilege.core.trash import MIN_AGE_DAYS, inventory, purge, total_bytes

# --- Serveur multimedia : ou l'on accepte d'emettre -------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://192.168.10.10:8096",
        "http://10.0.0.5:8096",
        "https://jellyfin.local",
        "http://localhost:8096",
        "http://172.16.4.2:8096",
    ],
)
def test_une_adresse_locale_est_acceptee(url) -> None:
    assert validate_server_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    ("url", "raison"),
    [
        ("http://example.com:8096", "hote public"),
        ("https://8.8.8.8", "adresse publique"),
        ("ftp://192.168.1.1", "schema non http"),
        ("http://169.254.169.254", "metadonnees d'instance cloud"),
    ],
)
def test_une_adresse_non_locale_est_refusee(url, raison) -> None:
    """Le champ est saisi depuis le navigateur et appele par le SERVEUR : sans
    restriction, il ferait du conteneur un relais de requetes."""
    with pytest.raises(MediaServerError):
        validate_server_url(url)


def test_le_refus_explique_ce_qui_est_attendu() -> None:
    with pytest.raises(MediaServerError, match=re.escape("192.168")):
        validate_server_url("http://example.com")


def test_la_barre_finale_est_normalisee() -> None:
    assert validate_server_url("http://192.168.1.5:8096/") == "http://192.168.1.5:8096"


async def test_un_rafraichissement_accepte_renvoie_vrai() -> None:
    vus: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        vus.append(request)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert await refresh_library("http://192.168.1.5:8096", "cle", client=client) is True
    finally:
        await client.aclose()

    assert vus[0].url.path == "/Library/Refresh"
    assert "cle" in vus[0].headers["authorization"]


async def test_un_serveur_injoignable_ne_leve_pas() -> None:
    """Le fichier est deja range : ne pas avoir prevenu est un desagrement,
    pas une perte."""

    async def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("injoignable")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert await refresh_library("http://192.168.1.5:8096", "cle", client=client) is False
    finally:
        await client.aclose()


async def test_une_adresse_publique_n_est_jamais_appelee() -> None:
    """La verification a lieu AVANT la requete."""
    appels = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal appels
        appels += 1
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    try:
        assert await refresh_library("http://example.com", "cle", client=client) is False
    finally:
        await client.aclose()
    assert appels == 0


# --- Corbeille : ce qu'on refuse de supprimer -------------------------------


def batch(root: Path, days_ago: int, files: int = 2) -> Path:
    day = (datetime.now(UTC).date() - timedelta(days=days_ago)).isoformat()
    directory = root / day
    directory.mkdir(parents=True)
    for i in range(files):
        (directory / f"reste{i}.mkv").write_bytes(b"x" * 1024)
    return directory


@pytest.fixture
def trash(tmp_path: Path) -> Path:
    return tmp_path / "corbeille"


def test_l_inventaire_compte_les_lots(trash: Path) -> None:
    batch(trash, 40, files=3)
    batch(trash, 5, files=1)

    lots = inventory(trash)

    assert [b.files for b in lots] == [3, 1]
    assert total_bytes(lots) == 4 * 1024


def test_le_plus_ancien_vient_en_tete(trash: Path) -> None:
    """C'est celui qu'on vide en premier."""
    batch(trash, 2)
    batch(trash, 60)
    assert inventory(trash)[0].age_days == 60


def test_une_corbeille_absente_n_est_pas_une_erreur(trash: Path) -> None:
    assert inventory(trash) == []


def test_le_vidage_respecte_le_delai(trash: Path) -> None:
    vieux = batch(trash, 40)
    recent = batch(trash, 3)

    result = purge(trash, older_than_days=30)

    assert result.removed_batches == 1
    assert not vieux.exists()
    assert recent.is_file() or recent.is_dir(), "le lot recent est intact"


def test_un_delai_nul_vide_vraiment_tout(trash: Path) -> None:
    """« Tout vider » doit tout vider, y compris ce qui vient d'etre evacue.

    Un plancher silencieux laisserait en place ce qu'on vient de demander de
    supprimer, sans le dire — le pire des comportements. La protection est
    ailleurs : la confirmation exigee par l'endpoint."""
    aujourdhui = batch(trash, 0)

    result = purge(trash, older_than_days=0)

    assert not aujourdhui.exists()
    assert result.removed_batches == 1
    assert MIN_AGE_DAYS == 0


def test_un_dossier_inattendu_n_est_jamais_supprime(trash: Path) -> None:
    """S'il est la, ce n'est pas nous qui l'avons mis. Le supprimer serait la
    pire chose que puisse faire le seul code de l'application qui supprime."""
    trash.mkdir(parents=True)
    intrus = trash / "photos-de-famille"
    intrus.mkdir()
    (intrus / "photo.jpg").write_bytes(b"x")

    purge(trash, older_than_days=1)

    assert (intrus / "photo.jpg").is_file()


def test_le_vidage_rapporte_l_espace_libere(trash: Path) -> None:
    batch(trash, 90, files=5)
    result = purge(trash, older_than_days=30)
    assert result.removed_files == 5
    assert result.freed_bytes == 5 * 1024


# --- Renommage de l'existant ------------------------------------------------


def in_library(root: Path, relative: str) -> ScannedFile:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video")
    return ScannedFile(
        path=path,
        size_bytes=2 * 1024**3,
        parsed=parse(path, list(Path(relative).parts[:-1])),
        probe=FileProbe(),
        relative_path=relative,
        in_library=True,
    )


def test_un_fichier_deja_conforme_n_est_pas_propose(tmp_path: Path) -> None:
    """Afficher « rien a faire » sur des centaines de lignes noierait les
    quelques-unes qui comptent."""
    template = "{title}/Season {season:02}/{title} - S{season:02}E{episode:02}"
    scanned = in_library(tmp_path, "Severance/Season 01/Severance - S01E01.mkv")

    plans = rename_plans([scanned], template=template, destination_root=tmp_path)

    assert plans == []


def test_un_fichier_non_conforme_est_propose(tmp_path: Path) -> None:
    template = "{title}/Season {season:02}/{title} - S{season:02}E{episode:02}"
    scanned = in_library(tmp_path, "Severance/severance.s01e01.1080p.mkv")

    plans = rename_plans([scanned], template=template, destination_root=tmp_path)

    assert len(plans) == 1
    # La casse n'est PAS corrigee : aucune identification n'est refaite, et
    # seul le fournisseur connait le titre officiel. Ce mode corrige la
    # structure, pas l'identite.
    assert plans[0].destination.name == "severance - S01E01.mkv"


def test_un_renommage_demande_toujours_une_validation(tmp_path: Path) -> None:
    """Aucune identification n'a ete refaite : marquer ces plans « automatiques »
    mentirait sur leur nature, et un renommage de masse partirait tout seul."""
    from sortilege.core.scoring import Decision

    scanned = in_library(tmp_path, "Severance/severance.s01e01.mkv")
    plans = rename_plans(
        [scanned], template="{title}/S{season:02}E{episode:02}", destination_root=tmp_path
    )

    assert plans[0].decision is Decision.REVIEW
    assert plans[0].manual is True


def test_un_gabarit_invalide_ne_propose_rien(tmp_path: Path) -> None:
    """Plutot que de proposer des chemins absurdes sur toute la bibliotheque."""
    scanned = in_library(tmp_path, "Severance/severance.s01e01.mkv")
    assert rename_plans([scanned], template="{inconnu}", destination_root=tmp_path) == []


def test_l_extension_est_conservee(tmp_path: Path) -> None:
    scanned = in_library(tmp_path, "Film/un.film.2024.avi")
    plans = rename_plans([scanned], template="{title}/{title}", destination_root=tmp_path)
    if plans:
        assert plans[0].destination.suffix == ".avi"
