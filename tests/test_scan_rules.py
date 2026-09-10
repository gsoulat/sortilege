"""Ce que le parcours ecarte : trois decisions rendues a l'utilisateur.

Elles etaient des constantes, donc des choix pris a sa place, et deux d'entre
eux sans la moindre trace : un court-metrage ou un episode en 480p passait sous
le plancher de cinquante mega-octets et disparaissait de la liste, un dossier
personnel restait dans le perimetre sans aucun moyen de l'en sortir.

Le point qui decide si le reglage tient : les valeurs livrees restent le socle.
Un utilisateur qui ajoute son dossier de photos ne doit pas, ce faisant, faire
reparcourir « @eaDir » et « #recycle ».
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api.deps import get_store, scan_rules
from sortilege.core.scanner import MIN_SIZE_BYTES, ScanRules, collect, scan
from sortilege.main import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


def video(path: Path, taille: int = 60 * 1024 * 1024) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        handle.seek(taille)
        handle.write(b"\0")
    return path


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "dl"
    src.mkdir()
    return src


# --- Les exclusions supplementaires ------------------------------------------


def test_un_dossier_ajoute_est_reellement_ecarte(source: Path) -> None:
    video(source / "Films" / "Dune.2024.mkv")
    video(source / "Photos perso" / "vacances.2019.mkv")

    regles = ScanRules.extended(extra_dirs=["Photos perso"])
    trouves, _, _ = collect([source], rules=regles)

    noms = {p.name for _, p in trouves}
    assert noms == {"Dune.2024.mkv"}


def test_un_motif_de_nom_ajoute_est_reellement_ecarte(source: Path) -> None:
    video(source / "Dune.2024.mkv")
    video(source / "Dune.2024.making-of.mkv")

    regles = ScanRules.extended(extra_hints=["making-of"])
    resultat = scan([source], deep=False, rules=regles)

    assert [f.path.name for f in resultat.files] == ["Dune.2024.mkv"]
    assert resultat.skipped == 1


def test_le_socle_livre_reste_applique(source: Path) -> None:
    """Ajouter une exclusion ne doit pas faire reparcourir les residus de NAS."""
    video(source / "@eaDir" / "vignette.mkv")
    video(source / "Perso" / "x.mkv")
    video(source / "Dune.2024.mkv")

    regles = ScanRules.extended(extra_dirs=["Perso"])
    trouves, _, _ = collect([source], rules=regles)

    assert {p.name for _, p in trouves} == {"Dune.2024.mkv"}


def test_la_casse_et_les_espaces_d_une_saisie_sont_absorbes(source: Path) -> None:
    """La liste vient d'un champ de saisie : «  MAKING-OF  » y est courant."""
    video(source / "Dune.MAKING-OF.mkv")

    regles = ScanRules.extended(extra_hints=["  Making-Of  "])
    resultat = scan([source], deep=False, rules=regles)

    assert resultat.files == []


def test_sans_regles_le_comportement_est_celui_d_avant(source: Path) -> None:
    video(source / "Perso" / "x.mkv")

    trouves, _, _ = collect([source])

    assert {p.name for _, p in trouves} == {"x.mkv"}


# --- Le plancher de taille ---------------------------------------------------


def test_un_court_metrage_peut_etre_retenu(source: Path) -> None:
    """Le cas que le plancher fixe ecartait sans trace."""
    video(source / "Court.Metrage.2021.mkv", taille=8 * 1024 * 1024)

    resultat = scan([source], deep=False, rules=ScanRules.extended(min_size_bytes=5 * 1024 * 1024))

    assert resultat.files[0].skipped_reason is None


def test_le_plancher_par_defaut_est_inchange(source: Path) -> None:
    video(source / "Court.Metrage.2021.mkv", taille=8 * 1024 * 1024)

    resultat = scan([source], deep=False)

    assert resultat.files[0].skipped_reason is not None
    assert resultat.files[0].min_size_bytes == MIN_SIZE_BYTES


def test_le_plancher_reste_celui_du_scan(source: Path) -> None:
    """Porte par le fichier : sans cela, changer le reglage reclasserait apres
    coup des fichiers deja affiches comme retenus."""
    video(source / "Court.2021.mkv", taille=8 * 1024 * 1024)

    resultat = scan([source], deep=False, rules=ScanRules.extended(min_size_bytes=1024))

    assert resultat.files[0].min_size_bytes == 1024


# --- Le chemin complet, depuis les preferences -------------------------------


def test_les_preferences_alimentent_les_regles(client: TestClient) -> None:
    avant = get_store().load().scan
    try:
        client.put(
            "/api/settings/preferences",
            json={
                "scan": {
                    "min_size_mb": 5,
                    "extra_skip_dirs": ["Photos perso", "  "],
                    "extra_skip_hints": ["making-of"],
                }
            },
        )

        regles = scan_rules()

        assert regles.min_size_bytes == 5 * 1024 * 1024
        assert "Photos perso" in regles.skip_dirs
        # Les lignes vides d'un champ multiligne sont un accident de saisie :
        # gardees, elles excluraient tout, puisque « » figure dans tout nom.
        assert "" not in regles.skip_dirs
        assert "@eaDir" in regles.skip_dirs
        assert "making-of" in regles.skip_hints and "sample" in regles.skip_hints
    finally:
        store = get_store()
        prefs = store.load()
        prefs.scan = avant
        store.save(prefs)


def test_un_chemin_au_lieu_d_un_nom_de_dossier_est_refuse(client: TestClient) -> None:
    """L'exclusion porte sur un NOM, ou qu'il apparaisse : accepter un chemin
    donnerait une regle qui ne s'applique jamais, sans rien dire."""
    r = client.put(
        "/api/settings/preferences",
        json={"scan": {"extra_skip_dirs": ["/volume1/Photos"]}},
    )

    assert r.status_code == 400
    assert "NOM" in r.json()["detail"]
