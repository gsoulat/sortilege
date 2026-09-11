"""Tests de la sauvegarde et de la restauration.

Deux enjeux, et un seul compte vraiment. Le premier est qu'une archive se
relise : une sauvegarde qu'on ne peut pas restaurer est pire que pas de
sauvegarde, parce qu'on cesse d'en chercher une autre. Le second est qu'elle ne
transporte AUCUN secret — c'est une decision de conception, et une decision de
conception qui n'est pas tenue par un test redevient une intention.
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import deps
from sortilege.core import backup
from sortilege.core.preferences import PreferenceStore
from sortilege.main import app

PASSWORD = "mot-de-passe-de-test"

CLE_TMDB = "cle-tmdb-tout-a-fait-reconnaissable"
WEBHOOK = "https://discord.com/api/webhooks/1234/jeton-tout-a-fait-reconnaissable"
CLE_API = "srtl_cle-api-tout-a-fait-reconnaissable"
CLE_OS = "cle-opensubtitles-tout-a-fait-reconnaissable"
JETON_OS = "jeton-vip-opensubtitles-tout-a-fait-reconnaissable"
GABARIT = "{title}{? year: ($)}/{title}{? year: ($)}"

SECRETS = (
    CLE_TMDB,
    WEBHOOK,
    CLE_API,
    CLE_OS,
    JETON_OS,
    "cle-ia-reconnaissable",
    "cle-jellyfin-reconnaissable",
)


def peuple(racine: Path) -> None:
    """Un volume de donnees comme en produit une installation qui a servi."""
    racine.mkdir(parents=True, exist_ok=True)
    (racine / backup.PREFERENCES_NAME).write_text(
        json.dumps(
            {
                "templates": {"movie": GABARIT},
                "destinations": {"movie": "Films"},
                "metadata": {"tmdb_api_key": CLE_TMDB, "language": "en-US"},
                "ai": {"enabled": True, "api_key": "cle-ia-reconnaissable"},
                "notifications": {"enabled": True, "webhook_url": WEBHOOK},
                "media_server": {"api_key": "cle-jellyfin-reconnaissable"},
                "integration": {"api_key": CLE_API},
                "subtitles": {
                    "enabled": True,
                    "opensubtitles_api_key": CLE_OS,
                    "opensubtitles_token": JETON_OS,
                    "languages": ["fr", "en"],
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (racine / backup.JOURNAL_NAME).write_text(
        '{"id": "abc", "detail": "deplace"}\n', encoding="utf-8"
    )
    # Table a nous : le volume peut deja porter une base creee au demarrage
    # de l'application, avec le vrai schema.
    conn = sqlite3.connect(racine / backup.DB_NAME)
    conn.execute("CREATE TABLE IF NOT EXISTS essai (titre TEXT)")
    conn.execute("DELETE FROM essai")
    conn.execute("INSERT INTO essai VALUES ('Dune')")
    conn.commit()
    conn.close()


def entrees(blob: bytes) -> set[str]:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return set(archive.namelist())


def lit(blob: bytes, nom: str) -> bytes:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return archive.read(nom)


def membres(blob: bytes) -> dict[str, bytes]:
    """Contenu DECOMPRESSE de chaque membre de l'archive."""
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        return {info.filename: archive.read(info) for info in archive.infolist()}


def remplace_preferences(blob: bytes, **blocs: object) -> bytes:
    """Fabrique une archive dont seuls certains blocs de reglages changent.

    Sert a simuler une archive qui PORTE des secrets — produite par une version
    qui les laissait fuir, ou retouchee a la main.
    """
    nom = f"{backup.PAYLOAD_DIR}/{backup.PREFERENCES_NAME}"
    sortie = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(blob)) as source,
        zipfile.ZipFile(sortie, "w", zipfile.ZIP_DEFLATED) as destination,
    ):
        for info in source.infolist():
            donnees = source.read(info)
            if info.filename == nom:
                prefs = json.loads(donnees)
                prefs.update(blocs)
                donnees = json.dumps(prefs, ensure_ascii=False).encode("utf-8")
            destination.writestr(info.filename, donnees)
    return sortie.getvalue()


def reecrit_manifeste(blob: bytes, **maj: object) -> bytes:
    """Fabrique une archive dont seul le manifeste change."""
    sortie = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(blob)) as source,
        zipfile.ZipFile(sortie, "w") as destination,
    ):
        for info in source.infolist():
            donnees = source.read(info)
            if info.filename == backup.MANIFEST_NAME:
                manifeste = json.loads(donnees)
                manifeste.update(maj)
                donnees = json.dumps(manifeste).encode("utf-8")
            destination.writestr(info.filename, donnees)
    return sortie.getvalue()


# --- Aller-retour -----------------------------------------------------------


def test_archive_produite_puis_relue(tmp_path: Path) -> None:
    """Le vrai test : ce qui sort de la restauration vaut ce qui est entre."""
    source = tmp_path / "avant"
    peuple(source)
    blob = backup.build(source, app_version="9.9.9")

    cible = tmp_path / "apres"
    rapport = backup.restore(blob, cible)

    assert {r["name"] for r in rapport["restored"]} == {
        backup.PREFERENCES_NAME,
        backup.DB_NAME,
        backup.JOURNAL_NAME,
    }
    prefs = json.loads((cible / backup.PREFERENCES_NAME).read_text(encoding="utf-8"))
    assert prefs["templates"]["movie"] == GABARIT
    assert prefs["metadata"]["language"] == "en-US"
    assert (cible / backup.JOURNAL_NAME).read_text(encoding="utf-8").startswith('{"id": "abc"')

    conn = sqlite3.connect(cible / backup.DB_NAME)
    assert conn.execute("SELECT titre FROM essai").fetchone()[0] == "Dune"
    conn.close()


def test_le_manifeste_nomme_le_format_et_sa_version(tmp_path: Path) -> None:
    peuple(tmp_path)
    manifeste = json.loads(lit(backup.build(tmp_path), backup.MANIFEST_NAME))
    assert manifeste["format"] == backup.FORMAT
    assert manifeste["version"] == backup.VERSION


def test_une_installation_vierge_produit_une_archive_lisible(tmp_path: Path) -> None:
    """Aucun etat sur disque n'est pas une erreur : c'est un premier demarrage."""
    blob = backup.build(tmp_path)
    assert backup.MANIFEST_NAME in entrees(blob)
    assert backup.inspect(blob)["contents"] == []


# --- Secrets ----------------------------------------------------------------


def test_aucun_secret_dans_l_archive(tmp_path: Path) -> None:
    """Le controle porte sur le contenu DECOMPRESSE de chaque membre.

    Chercher la chaine dans les octets du zip ne prouvait rien : les membres sont
    compresses, un secret ecrit en clair dans le JSON n'y apparait pas tel quel,
    et le test passait quoi que contienne l'archive. Verifier seulement le champ
    relu laisserait passer une fuite par un autre chemin — une copie du fichier,
    un secret repete dans le manifeste ou le LISEZMOI. Chaque membre est donc
    relu en entier.
    """
    peuple(tmp_path)
    contenu = membres(backup.build(tmp_path))
    # Temoin positif : une lecture qui rendrait des octets vides ou encore
    # compresses ferait passer le test sans rien prouver.
    assert b"en-US" in contenu[f"{backup.PAYLOAD_DIR}/{backup.PREFERENCES_NAME}"]
    for secret in SECRETS:
        for nom, octets in contenu.items():
            assert secret.encode("utf-8") not in octets, f"« {secret} » a fui dans {nom}"


def test_le_controle_des_secrets_sait_voir_une_fuite(tmp_path: Path) -> None:
    """Le test precedent doit pouvoir echouer : la meme lecture repere une cle
    presente dans une archive."""
    peuple(tmp_path)
    fuite = remplace_preferences(
        backup.build(tmp_path), subtitles={"opensubtitles_api_key": CLE_OS}
    )
    assert any(CLE_OS.encode("utf-8") in octets for octets in membres(fuite).values())


def test_les_champs_secrets_sont_vides_et_non_absents(tmp_path: Path) -> None:
    """Vides plutot que retires : un bloc ampute se relit mal ailleurs."""
    peuple(tmp_path)
    prefs = json.loads(lit(backup.build(tmp_path), f"{backup.PAYLOAD_DIR}/preferences.json"))
    assert prefs["metadata"]["tmdb_api_key"] == ""
    assert prefs["notifications"]["webhook_url"] == ""
    assert prefs["integration"]["api_key"] == ""
    assert prefs["subtitles"]["opensubtitles_api_key"] == ""
    assert prefs["subtitles"]["opensubtitles_token"] == ""


def test_le_lisezmoi_annonce_ce_qui_a_ete_retire(tmp_path: Path) -> None:
    """La decision doit etre lisible par celui qui ouvre le zip, pas seulement
    par celui qui lit le code."""
    peuple(tmp_path)
    texte = lit(backup.build(tmp_path), backup.README_NAME).decode("utf-8")
    assert "Clé TheMovieDB" in texte
    assert "Webhook Discord" in texte
    assert "Clé d'API OpenSubtitles" in texte
    assert "Jeton VIP OpenSubtitles" in texte
    assert "NE CONTIENT PAS" in texte


def test_les_secrets_en_place_survivent_a_la_restauration(tmp_path: Path) -> None:
    """Recuperer ses gabarits ne doit pas couper l'identification."""
    source = tmp_path / "avant"
    peuple(source)
    blob = backup.build(source)

    cible = tmp_path / "apres"
    peuple(cible)
    rapport = backup.restore(blob, cible)

    prefs = json.loads((cible / backup.PREFERENCES_NAME).read_text(encoding="utf-8"))
    assert prefs["metadata"]["tmdb_api_key"] == CLE_TMDB
    assert prefs["integration"]["api_key"] == CLE_API
    assert prefs["subtitles"]["opensubtitles_api_key"] == CLE_OS
    assert prefs["subtitles"]["opensubtitles_token"] == JETON_OS
    assert "Clé TheMovieDB" in rapport["secrets_kept"]
    assert rapport["secrets_missing"] == []


def test_une_instance_neuve_dit_quelles_cles_ressaisir(tmp_path: Path) -> None:
    source = tmp_path / "avant"
    peuple(source)
    rapport = backup.restore(backup.build(source), tmp_path / "apres")
    assert "Clé TheMovieDB" in rapport["secrets_missing"]
    assert rapport["secrets_kept"] == []


def test_une_archive_portant_d_autres_cles_opensubtitles_ne_remplace_pas_celles_en_place(
    tmp_path: Path,
) -> None:
    """Une archive qui porte des cles — produite avant que ces champs ne soient
    exclus — ne doit pas ecraser celles de l'instance."""
    source = tmp_path / "avant"
    peuple(source)
    blob = remplace_preferences(
        backup.build(source),
        subtitles={
            "enabled": True,
            "opensubtitles_api_key": "cle-opensubtitles-d-une-autre-instance",
            "opensubtitles_token": "jeton-vip-d-une-autre-instance",
            "languages": ["fr"],
        },
    )

    cible = tmp_path / "apres"
    peuple(cible)
    rapport = backup.restore(blob, cible)

    prefs = json.loads((cible / backup.PREFERENCES_NAME).read_text(encoding="utf-8"))
    assert prefs["subtitles"]["opensubtitles_api_key"] == CLE_OS
    assert prefs["subtitles"]["opensubtitles_token"] == JETON_OS
    # Le reste du bloc, lui, vient bien de l'archive.
    assert prefs["subtitles"]["languages"] == ["fr"]
    assert "Clé d'API OpenSubtitles" in rapport["secrets_kept"]
    assert "Jeton VIP OpenSubtitles" in rapport["secrets_kept"]


def test_une_instance_neuve_peut_encore_enregistrer_ses_reglages(tmp_path: Path) -> None:
    """Restaurer sur une instance neuve laissait notifications et sous-titres
    ACTIFS sans leur secret : la validation refusait ensuite tout enregistrement
    de reglages, quel qu'il soit. Ils sont coupes, et le compte rendu le dit."""
    source = tmp_path / "avant"
    peuple(source)
    cible = tmp_path / "apres"
    rapport = backup.restore(backup.build(source), cible)

    prefs = json.loads((cible / backup.PREFERENCES_NAME).read_text(encoding="utf-8"))
    assert prefs["notifications"]["enabled"] is False
    assert prefs["subtitles"]["enabled"] is False
    manquants = " | ".join(rapport["secrets_missing"])
    assert "notifications désactivées" in manquants
    assert "recherche de sous-titres désactivée" in manquants

    # La preuve qui compte : un enregistrement passe de nouveau la validation.
    bibliotheque = tmp_path / "media"
    bibliotheque.mkdir()
    magasin = PreferenceStore(
        path=cible / backup.PREFERENCES_NAME, library_root=bibliotheque, source_roots=[]
    )
    magasin.save(magasin.load())


# --- Refus ------------------------------------------------------------------


def test_un_fichier_qui_n_est_pas_un_zip_est_refuse() -> None:
    with pytest.raises(backup.BackupError, match="pas une archive ZIP"):
        backup.read_archive(b"ceci n'est pas un zip")


def test_archive_etrangere_refusee() -> None:
    """Un zip quelconque glisse dans le formulaire ne doit rien ecraser."""
    sortie = io.BytesIO()
    with zipfile.ZipFile(sortie, "w") as archive:
        archive.writestr("photos/vacances.jpg", b"\xff\xd8\xff")
    with pytest.raises(backup.BackupError, match="ne vient pas de Sortilège"):
        backup.read_archive(sortie.getvalue())


def test_manifeste_d_un_autre_produit_refuse() -> None:
    sortie = io.BytesIO()
    with zipfile.ZipFile(sortie, "w") as archive:
        archive.writestr(backup.MANIFEST_NAME, json.dumps({"format": "radarr", "version": 1}))
    with pytest.raises(backup.BackupError, match="Archive étrangère"):
        backup.read_archive(sortie.getvalue())


def test_version_plus_recente_refusee_avec_la_marche_a_suivre(tmp_path: Path) -> None:
    peuple(tmp_path)
    blob = reecrit_manifeste(backup.build(tmp_path), version=backup.VERSION + 1)
    with pytest.raises(backup.BackupError, match="mets l'application à jour"):
        backup.read_archive(blob)


def test_version_plus_ancienne_refusee(tmp_path: Path) -> None:
    peuple(tmp_path)
    blob = reecrit_manifeste(backup.build(tmp_path), version=backup.VERSION - 1)
    with pytest.raises(backup.BackupError, match="Le format a changé"):
        backup.read_archive(blob)


def test_une_archive_refusee_ne_touche_a_rien(tmp_path: Path) -> None:
    peuple(tmp_path)
    avant = (tmp_path / backup.JOURNAL_NAME).read_bytes()
    with pytest.raises(backup.BackupError):
        backup.restore(b"pas un zip", tmp_path)
    assert (tmp_path / backup.JOURNAL_NAME).read_bytes() == avant


def test_une_entree_hors_perimetre_est_ignoree(tmp_path: Path) -> None:
    """Seuls les noms attendus sont extraits : une remontee est inoperante."""
    source = tmp_path / "avant"
    peuple(source)
    blob = backup.build(source)

    sortie = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(blob)) as origine,
        zipfile.ZipFile(sortie, "w") as trafiquee,
    ):
        for info in origine.infolist():
            trafiquee.writestr(info.filename, origine.read(info))
        trafiquee.writestr("../../evasion.txt", b"je ne dois pas etre ecrit")

    cible = tmp_path / "apres"
    rapport = backup.restore(sortie.getvalue(), cible)
    assert "../../evasion.txt" in rapport["ignored"]
    assert not (tmp_path.parent / "evasion.txt").exists()
    assert not (cible / "evasion.txt").exists()


def test_la_restauration_laisse_un_filet(tmp_path: Path) -> None:
    """Le seul geste du produit qui detruit sans corbeille en garde une copie."""
    source = tmp_path / "avant"
    peuple(source)
    cible = tmp_path / "apres"
    peuple(cible)
    (cible / backup.JOURNAL_NAME).write_text('{"id": "a-recuperer"}\n', encoding="utf-8")

    rapport = backup.restore(backup.build(source), cible)
    assert rapport["previous_saved_as"] == backup.PREVIOUS_NAME

    filet = (cible / backup.PREVIOUS_NAME).read_bytes()
    ancien = lit(filet, f"{backup.PAYLOAD_DIR}/{backup.JOURNAL_NAME}").decode("utf-8")
    assert "a-recuperer" in ancien


def test_sans_filet_la_restauration_est_refusee_et_ne_touche_a_rien(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Continuer sans filet promettait un retour arriere qui n'existait pas."""
    source = tmp_path / "avant"
    peuple(source)
    blob = backup.build(source)
    cible = tmp_path / "apres"
    peuple(cible)
    (cible / backup.JOURNAL_NAME).write_text('{"id": "en-place"}\n', encoding="utf-8")
    avant = {p.name: p.read_bytes() for p in cible.iterdir()}

    def _volume_plein(*args: object, **kwargs: object) -> bytes:
        raise OSError("plus de place sur le volume")

    monkeypatch.setattr(backup, "build", _volume_plein)
    with pytest.raises(backup.BackupError, match="Restauration refusée"):
        backup.restore(blob, cible)

    assert {p.name: p.read_bytes() for p in cible.iterdir()} == avant


def test_une_ecriture_ratee_ne_remplace_aucun_fichier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tous les temporaires d'abord, les remplacements ensuite : un disque plein
    au deuxieme fichier ne laisse pas des reglages neufs a cote d'un journal
    ancien."""
    source = tmp_path / "avant"
    peuple(source)
    blob = backup.build(source)
    cible = tmp_path / "apres"
    peuple(cible)
    (cible / backup.JOURNAL_NAME).write_text('{"id": "en-place"}\n', encoding="utf-8")
    reglages_avant = (cible / backup.PREFERENCES_NAME).read_bytes()

    ecrire = Path.write_bytes
    provisoires = 0

    def _disque_plein(self: Path, donnees: bytes) -> int:
        nonlocal provisoires
        if self.name.endswith(".restauration"):
            provisoires += 1
            if provisoires == 2:
                raise OSError("disque plein")
        return ecrire(self, donnees)

    monkeypatch.setattr(Path, "write_bytes", _disque_plein)
    with pytest.raises(OSError, match="disque plein"):
        backup.restore(blob, cible)

    assert (cible / backup.PREFERENCES_NAME).read_bytes() == reglages_avant
    assert "en-place" in (cible / backup.JOURNAL_NAME).read_text(encoding="utf-8")
    assert not list(cible.glob("*.restauration"))


# --- Routes -----------------------------------------------------------------


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Detourne le volume de donnees vers un dossier propre a ce test.

    Sans cela, ces tests ecriraient dans le dossier partage par toute la suite
    et une restauration en emporterait l'etat au passage.
    """
    monkeypatch.setattr(deps, "DATA_DIR", tmp_path)
    for accesseur in (deps.get_store, deps.get_memory, deps.get_journal):
        accesseur.cache_clear()
    yield tmp_path
    for accesseur in (deps.get_store, deps.get_memory, deps.get_journal):
        accesseur.cache_clear()


@pytest.fixture
def logged(volume: Path) -> TestClient:
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
        yield client


def test_telechargement_de_l_archive(logged: TestClient, volume: Path) -> None:
    reponse = logged.get("/api/backup/archive")
    assert reponse.status_code == 200
    assert reponse.headers["content-type"] == "application/zip"
    assert "attachment; filename=" in reponse.headers["content-disposition"]
    # Dit sur la reponse elle-meme ce que l'archive ne porte pas : un client qui
    # automatise la sauvegarde ne lira jamais le LISEZMOI range dedans.
    assert reponse.headers["x-sortilege-secrets"] == "exclus"
    assert backup.MANIFEST_NAME in entrees(reponse.content)


def test_inspection_sans_rien_ecraser(logged: TestClient, volume: Path) -> None:
    peuple(volume)
    blob = backup.build(volume)
    avant = (volume / backup.JOURNAL_NAME).read_bytes()

    reponse = logged.post("/api/backup/inspect", content=blob)
    assert reponse.status_code == 200
    assert reponse.json()["secrets_included"] is False
    assert (volume / backup.JOURNAL_NAME).read_bytes() == avant


def test_la_restauration_exige_une_confirmation(logged: TestClient, volume: Path) -> None:
    peuple(volume)
    blob = backup.build(volume)
    (volume / backup.JOURNAL_NAME).write_text('{"id": "en-place"}\n', encoding="utf-8")

    reponse = logged.post("/api/backup/restore", content=blob)
    assert reponse.status_code == 400
    assert "confirm=true" in reponse.json()["detail"]
    assert "en-place" in (volume / backup.JOURNAL_NAME).read_text(encoding="utf-8")


def test_restauration_confirmee(logged: TestClient, volume: Path) -> None:
    peuple(volume)
    blob = backup.build(volume)
    (volume / backup.JOURNAL_NAME).write_text('{"id": "a-ecraser"}\n', encoding="utf-8")

    reponse = logged.post("/api/backup/restore?confirm=true", content=blob)
    assert reponse.status_code == 200
    assert "abc" in (volume / backup.JOURNAL_NAME).read_text(encoding="utf-8")


def test_archive_etrangere_refusee_par_la_route(logged: TestClient, volume: Path) -> None:
    sortie = io.BytesIO()
    with zipfile.ZipFile(sortie, "w") as archive:
        archive.writestr("notes.txt", b"rien a voir")

    reponse = logged.post("/api/backup/restore?confirm=true", content=sortie.getvalue())
    assert reponse.status_code == 400
    assert "Sortilège" in reponse.json()["detail"]


def test_corps_vide_refuse_avec_la_marche_a_suivre(logged: TestClient, volume: Path) -> None:
    reponse = logged.post("/api/backup/restore?confirm=true", content=b"")
    assert reponse.status_code == 400
    assert "--data-binary" in reponse.json()["detail"]


def test_la_sauvegarde_est_fermee_sans_session(volume: Path) -> None:
    with TestClient(app) as client:
        assert client.get("/api/backup/archive").status_code == 401
        assert client.post("/api/backup/restore?confirm=true", content=b"x").status_code == 401


def test_description_avant_telechargement(logged: TestClient, volume: Path) -> None:
    """Savoir ce qu'on emporte AVANT de cliquer, et pas a la restauration."""
    corps = logged.get("/api/backup").json()
    assert corps["format"] == backup.FORMAT
    assert corps["secrets_included"] is False
    assert {p["name"] for p in corps["pieces"]} == {p.name for p in backup.PIECES}
    # La cle d'API est generee au demarrage : elle figure donc parmi les
    # secrets que l'archive laissera derriere elle.
    assert "Clé d'API de Sortilège" in corps["secrets_excluded"]


def test_un_secret_porte_en_clair_par_une_archive_ancienne_est_signale() -> None:
    """Une archive d'avant le classement du champ le porte en clair sans le declarer.

    Il est efface a la restauration — c'est voulu — mais doit etre signale.
    """
    import json

    from sortilege.core.backup import _reinjecte_secrets

    archive = json.dumps(
        {"subtitles": {"opensubtitles_api_key": "cle-en-clair"}, "vpn": {"reference_ip": ""}}
    ).encode("utf-8")

    donnees, _, manquants = _reinjecte_secrets(archive, {}, {"Clé TheMovieDB"})

    assert "Clé d'API OpenSubtitles" in manquants
    assert "Adresse publique de référence du VPN" not in manquants, "vide et non declare"
    assert json.loads(donnees)["subtitles"]["opensubtitles_api_key"] == ""
