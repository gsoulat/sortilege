"""Mode root : ce qui est range adopte l'identite de son dossier d'accueil.

Le cas reel : les dossiers du client de telechargement appartiennent a 999, la
bibliotheque a 1000, et aucune identite unique n'ecrit des deux cotes — sauf
root. Mais un processus root depose des fichiers root:root, que l'utilisateur ne
peut plus ni renommer ni supprimer depuis son partage reseau. On remplacerait un
blocage par un autre.

Ces tests tournent en NON-root, en CI comme sur un poste : ils ne peuvent donc
pas vraiment changer de proprietaire. Deux detournements le simulent —
``os.geteuid`` pour faire croire qu'on est root, ``os.chown`` pour capturer ce
qui aurait ete fait. Ce qu'on verifie n'est pas le resultat du chown, que le
noyau garantit, mais la DECISION : qui sert de modele, sur quels chemins, et ce
qu'il advient du deplacement quand l'appel echoue.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

import pytest

from sortilege import identite
from sortilege.config import Settings

# Importe depuis « journal » a dessein : le helper vit desormais dans
# « core/proprietaire », et l'alias prive doit continuer de l'y trouver.
from sortilege.core.journal import Journal, _adopte_identite_du_dossier, apply_plan
from sortilege.core.planner import Plan
from sortilege.core.scoring import Decision
from sortilege.main import _diagnostic_des_droits

Appel = tuple[Path, int, int]


@pytest.fixture
def env(tmp_path: Path):
    src = tmp_path / "dl"
    dst = tmp_path / "media"
    src.mkdir()
    dst.mkdir()
    return src, dst, Journal(tmp_path / "journal.jsonl")


def make_plan(source: Path, destination: Path) -> Plan:
    return Plan(
        id="abc123",
        source=source,
        destination=destination,
        kind="movie",
        score=0.95,
        decision=Decision.AUTO,
    )


def capture_chown(monkeypatch: pytest.MonkeyPatch) -> list[Appel]:
    """Remplace ``os.chown`` par un carnet. Rend la liste des appels."""
    appels: list[Appel] = []

    def faux_chown(chemin, uid, gid, *, follow_symlinks=True) -> None:
        appels.append((Path(chemin), uid, gid))

    monkeypatch.setattr(os, "chown", faux_chown)
    return appels


def fait_croire_a_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)


def test_hors_root_aucun_chown(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le chemin courant reste gratuit : pas d'appel, donc pas de cout ni de
    message sur les installations ou l'identite suffit deja."""
    src, dst, journal = env
    appels = capture_chown(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    f = src / "film.mkv"
    f.write_text("contenu")

    resultat = apply_plan(
        make_plan(f, dst / "Dune (2024)" / "Dune (2024).mkv"), journal, dry_run=False
    )

    assert resultat.ok is True
    assert appels == []


def test_en_root_le_fichier_adopte_le_dossier_d_accueil(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'identite vient du dossier qui accueille, pas de root ni de la source.

    En CI, l'accueil appartient a l'utilisateur qui lance les tests : c'est
    justement le cas reel, ou il appartient a celui qui possede la
    bibliotheque.
    """
    src, dst, journal = env
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    f = src / "film.mkv"
    f.write_text("contenu")
    cible = dst / "Dune (2024).mkv"
    accueil = dst.stat()

    resultat = apply_plan(make_plan(f, cible), journal, dry_run=False)

    assert resultat.ok is True
    assert cible.read_text() == "contenu"
    assert appels == [(cible, accueil.st_uid, accueil.st_gid)]


def test_les_dossiers_crees_adoptent_aussi_l_identite(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """« Serie/Season 01 » cree par root laisserait l'utilisateur sans prise sur
    ce qu'il contient : c'est le dossier contenant qui donne le droit de
    supprimer."""
    src, dst, journal = env
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    f = src / "ep.mkv"
    f.write_text("contenu")
    serie = dst / "Severance (2022)"
    saison = serie / "Season 01"
    cible = saison / "Severance (2022) - S01E01.mkv"
    modele = dst.stat()

    resultat = apply_plan(make_plan(f, cible), journal, dry_run=False)

    assert resultat.ok is True
    # Les deux niveaux crees, du plus profond au plus proche du modele, puis le
    # fichier lui-meme.
    assert appels == [
        (saison, modele.st_uid, modele.st_gid),
        (serie, modele.st_uid, modele.st_gid),
        (cible, modele.st_uid, modele.st_gid),
    ]


def test_un_dossier_deja_la_n_est_pas_retouche(env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Un dossier existant appartient a qui l'a cree — pas a nous de le changer
    au passage, et surtout pas de le changer a chaque fichier range."""
    src, dst, journal = env
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    accueil = dst / "Films"
    accueil.mkdir()
    f = src / "film.mkv"
    f.write_text("contenu")
    cible = accueil / "Dune (2024).mkv"

    apply_plan(make_plan(f, cible), journal, dry_run=False)

    assert [chemin for chemin, _, _ in appels] == [cible]


def test_un_chown_refuse_laisse_le_fichier_range(
    env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Le deplacement a REUSSI. Le declarer en echec pour une etiquette qu'un
    chown repare perdrait le travail utile."""
    src, dst, journal = env
    fait_croire_a_root(monkeypatch)

    def chown_refuse(chemin, uid, gid, *, follow_symlinks=True) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "chown", chown_refuse)
    f = src / "film.mkv"
    f.write_text("contenu")
    cible = dst / "Films" / "Dune (2024).mkv"

    with caplog.at_level(logging.WARNING, logger="sortilege.core.proprietaire"):
        resultat = apply_plan(make_plan(f, cible), journal, dry_run=False)

    assert resultat.ok is True
    assert cible.read_text() == "contenu"
    assert not f.exists()
    assert len(journal.read_all()) == 1
    assert "operation not permitted" in caplog.text


def test_un_dossier_modele_illisible_ne_fait_rien_deviner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Sans modele lisible, aucune identite n'est inventee : on le dit, et on
    laisse le fichier ou il est."""
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    fichier = tmp_path / "film.mkv"
    fichier.write_text("contenu")

    with caplog.at_level(logging.WARNING, logger="sortilege.core.proprietaire"):
        _adopte_identite_du_dossier(fichier, tmp_path / "jamais-monte")

    assert appels == []
    assert "illisible" in caplog.text


@pytest.fixture
def sans_source_ajoutee(monkeypatch: pytest.MonkeyPatch):
    """Isole le diagnostic des preferences reelles du poste.

    ``chemins_declares`` lit ``data/preferences.json``, relatif au repertoire
    courant : sans cela, un test dependrait des sources que le developpeur a
    ajoutees dans sa propre instance.
    """
    monkeypatch.setattr(identite, "sources_ajoutees", list)


def test_le_diagnostic_de_demarrage_ne_leve_pas_quand_une_racine_manque(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, sans_source_ajoutee: None
) -> None:
    """Un volume pas encore monte est normal au demarrage d'un NAS : il se
    signale, il ne fait pas tomber l'application qui permettrait de le
    corriger."""
    presente = tmp_path / "dl"
    presente.mkdir()
    conf = Settings(
        library_root=tmp_path / "jamais-monte",
        source_roots=[presente, tmp_path / "absente"],
    )

    with caplog.at_level(logging.INFO, logger="sortilege.main"):
        _diagnostic_des_droits(conf)

    assert "jamais-monte" in caplog.text
    assert "absente" in caplog.text
    assert str(presente) in caplog.text
    assert identite.ECRITURE_OK in caplog.text


def test_le_diagnostic_signale_le_mode_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    sans_source_ajoutee: None,
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    conf = Settings(library_root=tmp_path, source_roots=[tmp_path])

    with caplog.at_level(logging.INFO, logger="sortilege.main"):
        _diagnostic_des_droits(conf)

    assert "root" in caplog.text


def test_le_diagnostic_de_demarrage_parle_le_meme_lexique_que_l_entrypoint(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, sans_source_ajoutee: None
) -> None:
    """Meme journal, meme vocabulaire.

    L'entrypoint disait « ÉCRITURE IMPOSSIBLE », l'application « ECRITURE
    REFUSEE » pour le meme fait : deux lexiques donnent l'impression de deux
    verdicts. Les deux verdicts sont maintenant les memes constantes.
    """
    verrouille = tmp_path / "verrouille"
    verrouille.mkdir(mode=0o555)
    conf = Settings(library_root=verrouille, source_roots=[])
    try:
        with caplog.at_level(logging.INFO, logger="sortilege.main"):
            _diagnostic_des_droits(conf)
    finally:
        verrouille.chmod(0o755)

    assert identite.ECRITURE_KO in caplog.text
    assert "ECRITURE REFUSEE" not in caplog.text


def test_le_diagnostic_de_demarrage_ecarte_ce_qui_n_est_pas_un_dossier(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, sans_source_ajoutee: None
) -> None:
    """Le second diagnostic contredisait le premier.

    Une racine qui est un FICHIER n'a pas de contenu a ecrire : l'entrypoint le
    disait (« ce n'est pas un dossier, ignoré »), l'application annoncait
    « accessible en écriture » parce qu'``os.access`` repond pour le fichier.
    """
    pas_un_dossier = tmp_path / "media"
    pas_un_dossier.write_text("ceci est un fichier", encoding="utf-8")
    conf = Settings(library_root=pas_un_dossier, source_roots=[])

    with caplog.at_level(logging.INFO, logger="sortilege.main"):
        _diagnostic_des_droits(conf)

    assert "pas un dossier" in caplog.text
    assert identite.ECRITURE_OK not in caplog.text


def test_le_diagnostic_de_demarrage_voit_les_sources_ajoutees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """C'est l'entree qui peut faire basculer le conteneur en root : la taire
    dans le diagnostic de l'application rendrait les deux inconciliables."""
    ajoutee = tmp_path / "ajoutee-depuis-l-interface"
    ajoutee.mkdir()
    monkeypatch.setattr(identite, "sources_ajoutees", lambda *args: [ajoutee])
    conf = Settings(library_root=tmp_path / "media", source_roots=[])

    with caplog.at_level(logging.INFO, logger="sortilege.main"):
        _diagnostic_des_droits(conf)

    assert str(ajoutee) in caplog.text


def test_le_diagnostic_de_demarrage_ne_repete_pas_une_racine(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, sans_source_ajoutee: None
) -> None:
    """La bibliotheque figure souvent aussi dans les sources : une ligne, pas deux."""
    conf = Settings(library_root=tmp_path, source_roots=[tmp_path])

    with caplog.at_level(logging.INFO, logger="sortilege.main"):
        _diagnostic_des_droits(conf)

    assert caplog.text.count(f"{tmp_path} : ") == 1


def test_le_diagnostic_de_demarrage_ne_reste_pas_sur_un_montage_fige(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, sans_source_ajoutee: None
) -> None:
    """Le delai de garde protege aussi ce diagnostic-ci.

    Il tourne dans le ``lifespan`` : un ``stat`` qui n'en revient pas empecherait
    l'application de finir de demarrer, donc de servir l'interface qui permet de
    corriger le montage.
    """
    muet = Path("/storage/nfs-tombe")
    barriere = threading.Event()
    vrai_stat = os.stat

    def stat_qui_ne_repond_pas(chemin, *args, **kwargs):
        # Seul le chemin vise est muet : os.stat sert partout, jusque dans
        # pytest, et le rendre muet pour tous ferait attendre le test lui-meme.
        if str(chemin) != str(muet):
            return vrai_stat(chemin, *args, **kwargs)
        barriere.wait(30)
        return vrai_stat(__file__)

    monkeypatch.setattr(os, "stat", stat_qui_ne_repond_pas)
    monkeypatch.setattr(identite, "DELAI_STAT", 0.05)
    conf = Settings(library_root=muet, source_roots=[])

    try:
        with caplog.at_level(logging.INFO, logger="sortilege.main"):
            _diagnostic_des_droits(conf)
    finally:
        barriere.set()

    assert "montage figé" in caplog.text
