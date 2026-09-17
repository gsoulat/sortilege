"""Tests du choix de l'identite du conteneur.

Ce module decide sous quel UID tourne toute l'application : se tromper, c'est
soit un « Permission denied » a chaque rangement, soit root sans raison. Les
deux erreurs doivent etre attrapees ici, pas sur le NAS de l'utilisateur.

Les identites sont SIMULEES pour la logique de decision : la CI tourne en
non-root, ``chown`` y est impossible, et un test qui ne peut pas fabriquer un
dossier appartenant a 999:999 ne testerait que le cas deja connu. Les lectures
de dossiers reels, elles, passent par ``tmp_path`` et ``chmod`` — le seul levier
dont on dispose vraiment.
"""

import os
import stat
import threading
import time
from pathlib import Path

import pytest

from sortilege import identite
from sortilege.identite import (
    DEFAUT,
    MontageFige,
    Racine,
    choisir,
    identite_demandee,
    lignes_diagnostic,
    lire_racine,
    main,
    peut_ecrire,
    sources_ajoutees,
    stat_avec_delai,
)

DOSSIER = stat.S_IFDIR


def racine(chemin: str, uid: int, gid: int, mode: int) -> Racine:
    """Une racine simulee : le seul moyen d'avoir deux proprietaires distincts."""
    return Racine(Path(chemin), uid=uid, gid=gid, mode=DOSSIER | mode)


# --- Droit d'ecriture ---------------------------------------------------------


def test_proprietaire_avec_le_bit_ecriture() -> None:
    assert peut_ecrire(racine("/dl", 999, 999, 0o755), 999, 999)


def test_proprietaire_sans_le_bit_ecriture() -> None:
    """Etre proprietaire ne suffit pas : c'est le mode qui tranche."""
    assert not peut_ecrire(racine("/dl", 999, 999, 0o555), 999, 999)


def test_proprietaire_n_herite_pas_des_droits_des_autres() -> None:
    """Unix retient la PREMIERE classe qui correspond, elles ne s'additionnent pas."""
    assert not peut_ecrire(racine("/dl", 999, 999, 0o577), 999, 999)


def test_groupe_avec_le_bit_ecriture() -> None:
    assert peut_ecrire(racine("/dl", 999, 999, 0o775), 1000, 999)


def test_groupe_sans_le_bit_ecriture() -> None:
    assert not peut_ecrire(racine("/dl", 999, 999, 0o755), 1000, 999)


def test_les_autres_en_dernier_recours() -> None:
    assert peut_ecrire(racine("/dl", 999, 999, 0o757), 1000, 1000)
    assert not peut_ecrire(racine("/dl", 999, 999, 0o755), 1000, 1000)


def test_le_bit_traversee_est_exige() -> None:
    """Sans x, on n'entre pas dans le dossier : un rename() y echoue malgre w."""
    assert not peut_ecrire(racine("/dl", 999, 999, 0o644), 999, 999)


def test_root_passe_partout() -> None:
    assert peut_ecrire(racine("/dl", 999, 999, 0o700), *identite.ROOT)


# --- Choix de l'identite ------------------------------------------------------


def test_identite_demandee_gardee_quand_elle_convient() -> None:
    racines = [
        racine("/storage/downloads", 1000, 1000, 0o755),
        racine("/storage/media", 1000, 1000, 0o755),
    ]
    choix = choisir(racines, 1000, 1000)
    assert (choix.uid, choix.gid) == (1000, 1000)
    assert "toutes les racines" in choix.raison


def test_deux_proprietaires_sans_ecriture_au_groupe_donnent_root() -> None:
    """Le cas du NAS : JDownloader ecrit en 999:999, la bibliotheque est a 1000.

    Aucune identite non-root n'ecrit dans les deux, et c'est une regle du noyau :
    il ne reste que root.
    """
    racines = [
        racine("/storage/downloads", 999, 999, 0o755),
        racine("/storage/media", 1000, 1000, 0o755),
    ]
    choix = choisir(racines, 1000, 1000)
    assert (choix.uid, choix.gid) == identite.ROOT
    assert "non-root" in choix.raison


def test_ecriture_au_groupe_evite_root() -> None:
    """Le meme cas, mais les telechargements sont en g+w : plus besoin de root."""
    racines = [
        racine("/storage/downloads", 999, 1000, 0o775),
        racine("/storage/media", 1000, 1000, 0o755),
    ]
    choix = choisir(racines, 1000, 1000)
    assert (choix.uid, choix.gid) == (1000, 1000)


def test_une_seule_racine_donne_son_proprietaire() -> None:
    racines = [racine("/storage/downloads", 999, 999, 0o755)]
    choix = choisir(racines, 1000, 1000)
    assert (choix.uid, choix.gid) == (999, 999)
    assert "999:999" in choix.raison


def test_racine_inexistante_ignoree() -> None:
    """Une source demontee ne doit pas faire basculer tout le conteneur en root."""
    racines = [
        racine("/storage/media", 1000, 1000, 0o755),
        Racine(Path("/storage/absente"), ecartee="absente ou illisible, ignorée"),
    ]
    choix = choisir(racines, 1000, 1000)
    assert (choix.uid, choix.gid) == (1000, 1000)


def test_aucune_racine_existante_garde_l_identite_demandee() -> None:
    racines = [Racine(Path("/storage/absente"), ecartee="absente, ignorée")]
    choix = choisir(racines, 1234, 5678)
    assert (choix.uid, choix.gid) == (1234, 5678)


def test_identite_composite_non_inventee() -> None:
    """1000:999 conviendrait, mais ce couple n'existe nulle part sur le NAS.

    On ne fabrique pas un proprietaire que personne ne reconnaitra : root est
    retenu, et le diagnostic dit pourquoi.
    """
    racines = [
        racine("/storage/downloads", 999, 999, 0o775),
        racine("/storage/media", 1000, 1000, 0o755),
    ]
    choix = choisir(racines, 1000, 1000)
    assert (choix.uid, choix.gid) == identite.ROOT


def test_meme_uid_partout_ne_bascule_pas_en_root() -> None:
    """Le cas du NAS ou seul le GROUPE differe d'un montage a l'autre.

    999 possede les deux dossiers et ecrit dans les deux : les deux candidates
    disent la meme chose. Basculer tout le conteneur en root pour un desaccord
    de groupe serait une punition sans rapport avec le probleme.
    """
    racines = [
        racine("/dl", 999, 999, 0o755),
        racine("/media", 999, 1000, 0o755),
    ]
    choix = choisir(racines, 1000, 1000)
    assert (choix.uid, choix.gid) == (999, 999)
    assert "root" in choix.raison


def test_le_gid_retenu_est_celui_qui_ouvre_une_porte() -> None:
    """A uid egal, le groupe retenu est celui d'une racine en g+wx.

    Il continuera de passer si le proprietaire d'une racine change ; l'autre ne
    tenait que par le proprietaire. L'ordre de declaration ne suffit donc pas :
    ici c'est la SECONDE racine qui donne le gid.
    """
    racines = [
        racine("/dl", 999, 999, 0o755),
        racine("/media", 999, 1000, 0o775),
    ]
    choix = choisir(racines, 1234, 5678)
    assert (choix.uid, choix.gid) == (999, 1000)


def test_uids_differents_restent_tranches_par_root() -> None:
    """La tolerance ne porte que sur le groupe : l'uid designe le compte qui
    possedera la bibliotheque, et on ne le tire pas au sort."""
    racines = [
        racine("/dl", 999, 100, 0o775),
        racine("/media", 1000, 100, 0o775),
    ]
    choix = choisir(racines, 1234, 5678)
    assert (choix.uid, choix.gid) == identite.ROOT


def test_ambiguite_tranchee_par_root() -> None:
    """Deux proprietaires conviennent : on ne tire pas au sort l'identite.

    Les deux dossiers partagent le groupe 100 en ecriture : chacun des deux
    proprietaires ecrit chez l'autre par le groupe. L'identite demandee, elle,
    n'est ni l'un ni l'autre.
    """
    racines = [
        racine("/storage/downloads", 999, 100, 0o775),
        racine("/storage/media", 1000, 100, 0o775),
    ]
    choix = choisir(racines, 1234, 5678)
    assert (choix.uid, choix.gid) == identite.ROOT
    assert "999:100" in choix.raison and "1000:100" in choix.raison


# --- Lecture des dossiers reels ----------------------------------------------


def test_lecture_d_un_dossier_reel(tmp_path: Path) -> None:
    dossier = tmp_path / "downloads"
    dossier.mkdir(mode=0o755)
    lue = lire_racine(dossier)
    assert lue.ecartee == ""
    assert (lue.uid, lue.gid) == (os.getuid(), os.stat(dossier).st_gid)
    assert stat.filemode(lue.mode).startswith("d")


def test_dossier_absent_ecarte(tmp_path: Path) -> None:
    lue = lire_racine(tmp_path / "jamais-monte")
    assert lue.ecartee
    assert "ignorée" in lue.ecartee


def test_fichier_au_lieu_d_un_dossier_ecarte(tmp_path: Path) -> None:
    fichier = tmp_path / "pas-un-dossier"
    fichier.touch()
    assert "dossier" in lire_racine(fichier).ecartee


MUET = Path("/storage/nfs-tombe")
"""Le chemin dont le ``stat`` ne repondra pas, dans les tests de delai."""


def fait_taire_stat(monkeypatch: pytest.MonkeyPatch) -> threading.Event:
    """Rend muet le ``stat`` de ``MUET`` — et de lui seul — jusqu'au relachement.

    C'est le montage fige : l'appel ne rend ni erreur ni resultat. Deux
    precautions :

    - seul le chemin vise est muet. ``os.stat`` est appele partout, jusque dans
      pytest : le rendre muet pour tous ferait attendre le test lui-meme ;
    - le fil qui l'execute finit sans rien lever. Un montage qui repond enfin,
      alors que plus personne n'ecoute, n'est pas une anomalie, et une exception
      dans un fil abandonne ferait echouer le test par la bande.
    """
    barriere = threading.Event()
    vrai_stat = os.stat

    def stat_qui_ne_repond_pas(chemin, *args, **kwargs):
        if str(chemin) != str(MUET):
            return vrai_stat(chemin, *args, **kwargs)
        # Relachee par le test, jamais par le delai : c'est l'appelant qui
        # abandonne, le fil reste bloque comme il le serait sur un vrai montage.
        barriere.wait(30)
        return vrai_stat(__file__)

    monkeypatch.setattr(os, "stat", stat_qui_ne_repond_pas)
    return barriere


def test_un_montage_fige_rend_la_main_au_lieu_d_attendre(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le defaut que ce delai ferme : un ``stat`` qui ne repond jamais.

    Un montage NFS ou SMB « hard » dont le serveur est tombe ne rend ni erreur
    ni resultat : l'appel attend. Ce module tourne avant l'``exec`` de
    l'entrypoint — sans delai de garde, le conteneur ne demarre pas du tout,
    alors que la docstring du module promet l'inverse.
    """
    barriere = fait_taire_stat(monkeypatch)
    debut = time.monotonic()
    try:
        with pytest.raises(MontageFige) as leve:
            stat_avec_delai(MUET, delai=0.05)
    finally:
        barriere.set()

    assert time.monotonic() - debut < 5, "l'appel a attendu la reponse malgre le delai"
    assert "montage figé" in str(leve.value.strerror)


def test_une_racine_muette_est_ecartee_et_le_dit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ecartee comme une racine absente — mais avec le bon motif : « absente »
    enverrait verifier un montage qui, lui, existe."""
    barriere = fait_taire_stat(monkeypatch)
    monkeypatch.setattr(identite, "DELAI_STAT", 0.05)
    try:
        lue = lire_racine(MUET)
    finally:
        barriere.set()

    assert "montage figé" in lue.ecartee
    assert "ignorée" in lue.ecartee
    assert (lue.uid, lue.gid) == (-1, -1)


def test_une_racine_qui_repond_lentement_est_quand_meme_lue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le delai n'est pas un couperet : une reponse en retard reste une reponse."""
    dossier = tmp_path / "lent"
    dossier.mkdir(mode=0o755)
    vrai_stat = os.stat

    def stat_lent(chemin, *args, **kwargs):
        time.sleep(0.05)
        return vrai_stat(chemin, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat_lent)
    monkeypatch.setattr(identite, "DELAI_STAT", 1.0)

    assert lire_racine(dossier).ecartee == ""


def test_dossier_en_lecture_seule_refuse(tmp_path: Path) -> None:
    """Seul levier disponible en CI : chmod sur un dossier qu'on possede."""
    dossier = tmp_path / "verrouille"
    dossier.mkdir(mode=0o555)
    try:
        lue = lire_racine(dossier)
        assert not peut_ecrire(lue, lue.uid, lue.gid)
    finally:
        dossier.chmod(0o755)


# --- Sources ajoutees depuis l'interface --------------------------------------


def test_sources_ajoutees_lues(tmp_path: Path) -> None:
    fichier = tmp_path / "preferences.json"
    fichier.write_text('{"custom_sources": ["/storage/Video", "relatif", 42]}', encoding="utf-8")
    assert sources_ajoutees(fichier) == [Path("/storage/Video")]


def test_preferences_absentes_ou_cassees_sans_bruit(tmp_path: Path) -> None:
    assert sources_ajoutees(tmp_path / "rien.json") == []
    casse = tmp_path / "casse.json"
    casse.write_text("{ceci n'est pas du JSON", encoding="utf-8")
    assert sources_ajoutees(casse) == []


# --- Lecture de PUID/PGID -----------------------------------------------------


def test_identite_demandee_lue() -> None:
    assert identite_demandee("999:1001") == (999, 1001)


def test_auto_et_saisies_fautives_retombent_sur_le_defaut() -> None:
    assert identite_demandee("auto:auto") == DEFAUT
    assert identite_demandee("") == DEFAUT
    assert identite_demandee("100O:-5") == DEFAUT


def test_seuls_des_chiffres_ascii_sont_lus() -> None:
    """``int()`` acceptait plus que ce que la fonction promettait.

    « 1_0 » valait 10, les espaces passaient, les chiffres arabes aussi. Un PUID
    decide sous quelle identite tourne tout le conteneur : mieux vaut retomber
    sur le defaut, visible dans le diagnostic, qu'un nombre que personne n'a
    voulu ecrire.
    """
    assert identite_demandee("1_0:1_0") == DEFAUT
    assert identite_demandee(" 1000 : 1000 ") == DEFAUT
    # Les chiffres arabes, ecrits en echappements pour rester lisibles ici :
    # « int() » les convertissait comme les notres.
    assert identite_demandee("\u0661\u0660\u0660\u0660:\u0661\u0660\u0660\u0660") == DEFAUT
    assert identite_demandee("+1000:1000") == DEFAUT
    assert identite_demandee("1000:1000") == (1000, 1000)
    assert identite_demandee("0:0") == (0, 0)


# --- Sortie du programme ------------------------------------------------------


def test_stdout_ne_porte_que_l_identite(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Contrat avec l'entrypoint : stdout se consomme tel quel, sans filtrage."""
    dossier = tmp_path / "media"
    dossier.mkdir(mode=0o755)
    monkeypatch.setattr(identite, "chemins_declares", lambda: [dossier])

    assert main([f"{os.getuid()}:{os.getgid()}"]) == 0
    sortie = capsys.readouterr()
    assert sortie.out == f"{os.getuid()}:{os.getgid()}\n"
    assert str(dossier) in sortie.err


def test_root_retenu_quand_plus_rien_ne_passe(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    dossier = tmp_path / "verrouille"
    dossier.mkdir(mode=0o555)
    monkeypatch.setattr(identite, "chemins_declares", lambda: [dossier])
    try:
        assert main([f"{os.getuid()}:{os.getgid()}"]) == 0
    finally:
        dossier.chmod(0o755)

    sortie = capsys.readouterr()
    assert sortie.out == "0:0\n"
    # root peut tout : sans le rappel de ce qui resistait a l'identite demandee,
    # le tableau dirait « accessible en écriture » partout et le basculement
    # ressemblerait a un caprice.
    assert "Pourquoi pas" in sortie.err
    assert str(dossier) in sortie.err


def test_impose_rend_l_identite_demandee_malgre_le_refus(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUID fixe reste la loi : on explique, on ne decide pas a sa place."""
    dossier = tmp_path / "verrouille"
    dossier.mkdir(mode=0o555)
    monkeypatch.setattr(identite, "chemins_declares", lambda: [dossier])
    try:
        assert main(["--impose", "1234:5678"]) == 0
    finally:
        dossier.chmod(0o755)

    sortie = capsys.readouterr()
    assert sortie.out == "1234:5678\n"
    assert "PUID=auto" in sortie.err


def test_identite_lue_dans_l_environnement_sans_argument(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    dossier = tmp_path / "media"
    dossier.mkdir(mode=0o755)
    monkeypatch.setattr(identite, "chemins_declares", lambda: [dossier])
    monkeypatch.setenv("PUID", "4242")
    monkeypatch.setenv("PGID", "4242")

    assert main([]) == 0
    # 4242 ne possede rien et le dossier n'est pas ouvert aux autres : le
    # proprietaire reel est propose a sa place.
    assert capsys.readouterr().out == f"{os.getuid()}:{os.stat(dossier).st_gid}\n"


def test_aucune_racine_declaree_reste_lisible(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(identite, "chemins_declares", list)
    assert main(["1000:1000"]) == 0
    sortie = capsys.readouterr()
    assert sortie.out == "1000:1000\n"
    assert "aucune racine déclarée" in sortie.err


# --- Diagnostic ---------------------------------------------------------------


def test_diagnostic_une_ligne_par_racine() -> None:
    racines = [
        racine("/storage/downloads", 999, 999, 0o755),
        racine("/storage/media", 1000, 1000, 0o755),
        Racine(Path("/storage/absente"), ecartee="absente ou illisible, ignorée"),
    ]
    lignes = lignes_diagnostic(racines, 1000, 1000, "raison")
    assert lignes[0].startswith("Sortilège")
    assert sum(1 for ligne in lignes if "/storage/" in ligne) == 3
    assert "drwxr-xr-x" in lignes[1]
    assert "ÉCRITURE IMPOSSIBLE" in lignes[1]
    assert "accessible en écriture" in lignes[2]
    assert "ignorée" in lignes[3]


def test_diagnostic_nomme_le_proprietaire_fautif() -> None:
    lignes = lignes_diagnostic([racine("/storage/downloads", 999, 999, 0o755)], 1000, 1000, "x")
    assert "999:999" in lignes[1]


def test_diagnostic_signale_le_compromis_root() -> None:
    lignes = lignes_diagnostic([racine("/storage/downloads", 999, 999, 0o755)], 0, 0, "x")
    assert "(root)" in lignes[0]
    assert any("compromis" in ligne.lower() for ligne in lignes)


def test_diagnostic_compte_les_racines_ignorees() -> None:
    """Une racine ecartee ne compte pas dans la decision : il faut le dire.

    Sans cette ligne, une identite ordinaire peut etre retenue alors que root
    etait necessaire, et l'utilisateur retrouve « Permission denied » au premier
    rangement, une fois le volume enfin monte — sans rien pour faire le lien.
    """
    racines = [
        racine("/storage/media", 1000, 1000, 0o755),
        Racine(Path("/storage/absente"), ecartee="absente ou illisible, ignorée"),
        Racine(Path("/storage/muette"), ecartee="n'a pas répondu en 3 s (montage figé ?), ignorée"),
    ]
    lignes = lignes_diagnostic(racines, 1000, 1000, "raison")
    assert any("2 racine(s) ignorée(s)" in ligne for ligne in lignes)
    assert any("relance le conteneur" in ligne for ligne in lignes)


def test_diagnostic_sans_racine_ignoree_ne_dit_rien() -> None:
    lignes = lignes_diagnostic([racine("/storage/media", 1000, 1000, 0o755)], 1000, 1000, "x")
    assert not any("ignorée" in ligne for ligne in lignes)


def test_diagnostic_previent_pour_les_groupes_secondaires() -> None:
    lignes = lignes_diagnostic([racine("/storage/downloads", 999, 999, 0o755)], 1000, 1000, "x")
    assert any("groupes secondaires" in ligne for ligne in lignes)
