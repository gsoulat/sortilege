"""Ce que Sortilege pose a cote d'un media : a qui c'est, et qui peut le lire.

Deux defauts distincts sont verifies ici, et seul le PREMIER a quelque chose a
voir avec root.

1. **Les droits.** Une fiche ``.nfo`` ou une affiche naissent d'un fichier
   temporaire, donc en 0600. Publiees telles quelles, elles sont illisibles pour
   le serveur multimedia, qui tourne sous un autre compte — et le symptome est
   muet des deux cotes : Sortilege a reussi son depot, Jellyfin voit un fichier
   qu'il ne peut pas ouvrir. Ce defaut frappe TOUTES les installations, root ou
   non : c'est pourquoi il a ses propres tests, sans aucun detournement.

2. **Le proprietaire.** En root — seule identite qui traverse des montages
   appartenant a 999 d'un cote et a 1000 de l'autre — tout ce qui est depose
   naitrait root:root dans une bibliotheque qui appartient a l'utilisateur.

Ces tests tournent en NON-root, en CI comme sur un poste : ils ne peuvent donc
pas vraiment changer de proprietaire. Deux detournements le simulent, comme dans
``test_mode_root`` — ``os.geteuid`` pour faire croire qu'on est root,
``os.chown`` pour capturer ce qui aurait ete fait. Ce qu'on verifie n'est pas le
resultat du chown, que le noyau garantit, mais la DECISION : sur quels chemins,
d'apres quel modele, et ce qu'il advient du depot quand l'appel echoue.
"""

from __future__ import annotations

import logging
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from sortilege.core import transcode
from sortilege.core.companions import send_to_trash
from sortilege.core.nfo import MediaInfo, movie_xml, place_file, write_nfo
from sortilege.core.proprietaire import MODE_FICHIER_DEPOSE, adopte_identite_du_dossier
from sortilege.core.subtitles import write_subtitle

Appel = tuple[Path, int, int]

JPEG = b"\xff\xd8\xff\xe0garni\xff\xd9"
SRT = b"1\n00:00:01,000 --> 00:00:02,000\nBonjour.\n"


def capture_chown(monkeypatch: pytest.MonkeyPatch) -> list[Appel]:
    """Remplace ``os.chown`` par un carnet. Rend la liste des appels."""
    appels: list[Appel] = []

    def faux_chown(chemin, uid, gid, *, follow_symlinks=True) -> None:
        appels.append((Path(chemin), uid, gid))

    monkeypatch.setattr(os, "chown", faux_chown)
    return appels


def fait_croire_a_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)


def droits(chemin: Path) -> int:
    return stat.S_IMODE(chemin.stat().st_mode)


@pytest.fixture
def umask_severe() -> Iterator[int]:
    """Un umask de NAS durci, ou tout ce qui est cree naitrait prive.

    C'est le reglage qui distingue un depot reellement corrige d'un depot qui
    marche par chance : sous l'umask habituel (022), un ``open`` ordinaire donne
    deja 0644 et la verification ne prouverait rien.
    """
    precedent = os.umask(0o077)
    try:
        yield precedent
    finally:
        os.umask(precedent)


@pytest.fixture(params=[0o000, 0o077], ids=["umask-000", "umask-077"])
def les_deux_extremes_d_umask(request: pytest.FixtureRequest) -> Iterator[int]:
    """Les deux bouts du reglage, parce que le defaut ne prouve rien.

    Sous 077 — NAS durci — un fichier nait prive et le serveur multimedia ne
    peut pas l'ouvrir. Sous 000, a l'inverse, un ``open`` ordinaire le cree
    world-writable, ce qu'on ne veut pas davantage. Le mode depose doit etre le
    meme aux deux bouts : exactement 0644.
    """
    precedent = os.umask(request.param)
    try:
        yield request.param
    finally:
        os.umask(precedent)


def echange_contre_un_lien(
    monkeypatch: pytest.MonkeyPatch, victime: Path, cible: Path | None = None
) -> None:
    """Remplace le nom publie par un lien vers ``victime``, juste apres le renommage.

    C'est la fenetre que le controle a reproduite : le renommage a eu lieu, le
    nom definitif existe, et un tiers qui peut ecrire dans ce dossier — le
    dossier de telechargement d'un NAS est ouvert a plus d'un compte — le
    remplace par un lien symbolique avant que Sortilege ne pose les droits. Un
    ``chmod`` par CHEMIN suivrait le lien et rendrait 0644 un fichier situe
    ailleurs.

    ``cible`` restreint l'echange a un chemin precis ; sans elle, le premier
    renommage rencontre est detourne.
    """
    vrai_replace = os.replace

    def replace_puis_echange(source, destination, **kwargs) -> None:
        vrai_replace(source, destination)
        publie = Path(destination)
        if cible is None or publie == cible:
            publie.unlink()
            publie.symlink_to(victime)

    monkeypatch.setattr(os, "replace", replace_puis_echange)


def fichier_prive(tmp_path: Path) -> Path:
    """Un fichier d'ailleurs, que personne d'autre que son proprietaire ne lit."""
    victime = tmp_path / "ailleurs" / "secret.txt"
    victime.parent.mkdir()
    victime.write_text("privé", encoding="utf-8")
    victime.chmod(0o600)
    return victime


def film(tmp_path: Path) -> Path:
    dossier = tmp_path / "media" / "Dune (2024)"
    dossier.mkdir(parents=True)
    video = dossier / "Dune (2024).mkv"
    video.write_bytes(b"video")
    return video


# --- Les droits : le defaut qui ne depend pas de root -----------------------


def test_une_fiche_deposee_est_lisible_hors_root(tmp_path: Path, umask_severe: int) -> None:
    """Le cas le plus courant, et le plus silencieux.

    ``NamedTemporaryFile`` cree en 0600 : sans correction, la fiche publiee
    n'appartient qu'au compte qui l'ecrit, alors qu'elle n'existe QUE pour etre
    relue par un serveur multimedia qui tourne sous un autre compte.
    """
    video = film(tmp_path)
    fiche = video.with_suffix(".nfo")

    write_nfo(fiche, movie_xml(MediaInfo(title="Dune", year=2024)))

    assert droits(fiche) == MODE_FICHIER_DEPOSE
    assert droits(fiche) & 0o044, "ni le groupe ni les autres ne peuvent lire la fiche"


def test_une_affiche_deposee_est_lisible_hors_root(tmp_path: Path, umask_severe: int) -> None:
    """Meme primitive, meme piege : une affiche que le serveur ne peut pas
    ouvrir laisse une jaquette vide, sans le moindre message."""
    video = film(tmp_path)
    affiche = video.parent / "poster.jpg"

    place_file(affiche, JPEG)

    assert droits(affiche) == MODE_FICHIER_DEPOSE


def test_un_sous_titre_depose_est_lisible_hors_root(tmp_path: Path, umask_severe: int) -> None:
    """Le sous-titre ne passe pas par ``tempfile`` mais herite du umask : sous
    077 il naitrait tout aussi prive, et le lecteur n'afficherait rien."""
    video = film(tmp_path)

    resultat = write_subtitle(video, SRT, language="fr")

    assert resultat.path is not None
    assert droits(resultat.path) == MODE_FICHIER_DEPOSE


def test_le_mode_depose_est_le_meme_aux_deux_bouts_de_l_umask(
    tmp_path: Path, les_deux_extremes_d_umask: int
) -> None:
    """Le mode d'un fichier depose ne doit rien devoir a l'umask du conteneur.

    Sous 077, ``tempfile`` et ``open`` donnent un fichier prive : illisible pour
    le serveur multimedia. Sous 000, ``open`` donne un fichier world-writable :
    n'importe quel compte de la machine peut reecrire la fiche. Les deux sont
    des defauts, et c'est le meme correctif qui les ferme.
    """
    video = film(tmp_path)
    fiche = video.with_suffix(".nfo")
    affiche = video.parent / "poster.jpg"

    write_nfo(fiche, movie_xml(MediaInfo(title="Dune", year=2024)))
    place_file(affiche, JPEG)
    resultat = write_subtitle(video, SRT, language="fr")

    assert resultat.path is not None
    for depose in (fiche, affiche, resultat.path):
        assert droits(depose) == MODE_FICHIER_DEPOSE, depose
        assert not droits(depose) & 0o111, f"{depose} porte un bit d'exécution"
        assert not droits(depose) & stat.S_IWOTH, f"{depose} est ouvert en écriture à tous"


def test_un_lien_pose_apres_la_publication_d_une_fiche_ne_touche_rien_ailleurs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask_severe: int
) -> None:
    """Le defaut reproduit par le controle, et la raison du correctif.

    Les droits sont poses sur le DESCRIPTEUR du temporaire, avant le renommage :
    a l'instant ou le nom definitif existe, il n'y a plus rien a faire dessus, et
    le lien pose par le tiers ne mene nulle part utile pour lui. Avec un
    ``chmod`` par chemin apres le renommage, ``secret.txt`` passait en 0644.
    """
    video = film(tmp_path)
    fiche = video.with_suffix(".nfo")
    victime = fichier_prive(tmp_path)
    echange_contre_un_lien(monkeypatch, victime, fiche)

    write_nfo(fiche, movie_xml(MediaInfo(title="Dune", year=2024)))

    assert droits(victime) == 0o600
    assert victime.read_text(encoding="utf-8") == "privé"


def test_un_lien_pose_apres_la_publication_d_un_sous_titre_ne_touche_rien_ailleurs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, umask_severe: int
) -> None:
    """Meme fenetre, autre primitive d'ecriture : les sous-titres ne passent pas
    par ``tempfile`` et avaient le meme defaut."""
    video = film(tmp_path)
    victime = fichier_prive(tmp_path)
    echange_contre_un_lien(monkeypatch, victime)

    write_subtitle(video, SRT, language="fr")

    assert droits(victime) == 0o600


def test_l_identite_ne_suit_jamais_un_lien(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le pendant du precedent pour le ``chown``.

    Lui ne peut pas etre pose avant la publication — il lui faut le dossier
    d'accueil comme modele, et un ``fchown`` sur le temporaire journaliserait un
    descripteur au lieu d'un chemin. Il n'en a pas besoin : ``follow_symlinks``
    existe pour ``chown``, et c'est precisement ce qui manque a ``chmod``.
    """
    suivis: list[bool] = []

    def chown_espionne(chemin, uid, gid, *, follow_symlinks=True) -> None:
        suivis.append(follow_symlinks)

    monkeypatch.setattr(os, "chown", chown_espionne)
    fait_croire_a_root(monkeypatch)
    video = film(tmp_path)

    write_nfo(video.with_suffix(".nfo"), movie_xml(MediaInfo(title="Dune", year=2024)))
    write_subtitle(video, SRT, language="fr")

    assert suivis == [False, False]


def test_le_contenu_depose_reste_intact(tmp_path: Path) -> None:
    """Rien de ce qui precede ne doit changer ce qui est ecrit."""
    video = film(tmp_path)
    affiche = video.parent / "poster.jpg"

    place_file(affiche, JPEG)

    assert affiche.read_bytes() == JPEG


def test_des_droits_refuses_laissent_le_fichier_depose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Sur un partage reseau, poser les droits peut etre refuse. Le fichier, lui,
    est ecrit et a sa place : le declarer en echec perdrait le travail utile."""
    video = film(tmp_path)
    fiche = video.with_suffix(".nfo")

    def fchmod_refuse(descripteur, mode) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "fchmod", fchmod_refuse)

    with caplog.at_level(logging.WARNING, logger="sortilege.core.proprietaire"):
        write_nfo(fiche, movie_xml(MediaInfo(title="Dune", year=2024)))

    assert fiche.is_file()
    assert "operation not permitted" in caplog.text
    # Le nom du fichier est dans le message : un descripteur ne se nomme pas,
    # et un avertissement qui ne dit pas de quel fichier il parle ne sert a rien.
    assert fiche.name in caplog.text


# --- Le proprietaire : uniquement en root -----------------------------------


def test_hors_root_aucun_chown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Le chemin courant reste gratuit : pas un seul appel sur les
    installations ou l'identite suffit deja."""
    appels = capture_chown(monkeypatch)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    video = film(tmp_path)

    write_nfo(video.with_suffix(".nfo"), movie_xml(MediaInfo(title="Dune", year=2024)))
    place_file(video.parent / "poster.jpg", JPEG)
    write_subtitle(video, SRT, language="fr")
    send_to_trash(video, tmp_path / "corbeille")

    assert appels == []


def test_en_root_la_fiche_adopte_son_dossier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'identite vient du dossier qui accueille la fiche — celui du media,
    c'est-a-dire celui de la bibliotheque — et non de root."""
    video = film(tmp_path)
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    fiche = video.with_suffix(".nfo")
    modele = video.parent.stat()

    write_nfo(fiche, movie_xml(MediaInfo(title="Dune", year=2024)))

    assert appels == [(fiche, modele.st_uid, modele.st_gid)]


def test_en_root_l_affiche_adopte_son_dossier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = film(tmp_path)
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    affiche = video.parent / "poster.jpg"
    modele = video.parent.stat()

    place_file(affiche, JPEG)

    assert appels == [(affiche, modele.st_uid, modele.st_gid)]


def test_en_root_le_sous_titre_adopte_son_dossier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    video = film(tmp_path)
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    modele = video.parent.stat()

    resultat = write_subtitle(video, SRT, language="fr")

    assert appels == [(resultat.path, modele.st_uid, modele.st_gid)]


def test_en_root_le_dossier_de_la_fiche_adopte_aussi_l_identite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Une fiche de serie se depose dans un dossier qui n'existe pas encore.

    Sous Unix, c'est le dossier CONTENANT qui donne le droit de supprimer : un
    « Severance (2022) » cree par root rendrait sa fiche indeboulonnable, meme
    correctement etiquetee.
    """
    bibliotheque = tmp_path / "media"
    bibliotheque.mkdir()
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    serie = bibliotheque / "Severance (2022)"
    fiche = serie / "tvshow.nfo"
    modele = bibliotheque.stat()

    write_nfo(fiche, movie_xml(MediaInfo(title="Severance", year=2022)))

    assert appels == [
        (serie, modele.st_uid, modele.st_gid),
        (fiche, modele.st_uid, modele.st_gid),
    ]


def test_un_chown_refuse_laisse_le_fichier_depose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Le depot a REUSSI. Le declarer en echec pour une etiquette qu'un chown
    repare perdrait la fiche elle-meme."""
    video = film(tmp_path)
    fait_croire_a_root(monkeypatch)

    def chown_refuse(chemin, uid, gid, *, follow_symlinks=True) -> None:
        raise PermissionError("operation not permitted")

    monkeypatch.setattr(os, "chown", chown_refuse)
    fiche = video.with_suffix(".nfo")

    with caplog.at_level(logging.WARNING, logger="sortilege.core.proprietaire"):
        write_nfo(fiche, movie_xml(MediaInfo(title="Dune", year=2024)))

    assert "<title>Dune</title>" in fiche.read_text(encoding="utf-8")
    assert droits(fiche) == MODE_FICHIER_DEPOSE
    assert "operation not permitted" in caplog.text


def test_un_dossier_modele_illisible_ne_fait_rien_deviner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Sans modele lisible, aucune identite n'est inventee — et rien n'est leve.

    Les droits, eux, ne dependent d'aucun modele : ils sont deja poses a cet
    instant, sur le descripteur du temporaire, avant la publication.
    """
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    orphelin = tmp_path / "jamais-monte" / "fiche.nfo"
    orphelin.parent.mkdir()
    orphelin.write_text("x", encoding="utf-8")
    monkeypatch.setattr(Path, "stat", _stat_qui_refuse_le_dossier(orphelin.parent))

    with caplog.at_level(logging.WARNING, logger="sortilege.core.proprietaire"):
        adopte_identite_du_dossier(orphelin, orphelin.parent)

    assert appels == []
    assert "illisible" in caplog.text


def _stat_qui_refuse_le_dossier(interdit: Path):
    """``Path.stat`` qui echoue sur UN dossier precis, et se comporte
    normalement partout ailleurs — le montage disparu, sans le demonter."""
    vrai = Path.stat

    def stat_filtre(self: Path, **kwargs):
        if self == interdit:
            raise PermissionError("dossier illisible")
        return vrai(self, **kwargs)

    return stat_filtre


# --- Les dossiers crees hors deplacement ------------------------------------


def test_le_lot_de_corbeille_adopte_la_corbeille(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La corbeille est le seul endroit ou l'utilisateur ira vraiment a la main.

    Un lot du jour cree par root l'empecherait de la vider, alors meme que les
    fichiers deplaces dedans gardent, eux, leur proprietaire d'origine.
    """
    video = film(tmp_path)
    corbeille = tmp_path / "corbeille"
    corbeille.mkdir()
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    modele = corbeille.stat()

    parti = send_to_trash(video, corbeille)

    assert parti.is_file()
    assert appels == [(parti.parent, modele.st_uid, modele.st_gid)]


def test_le_dossier_de_reencodage_adopte_la_bibliotheque(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un reencodage interrompu laisse un fichier a demi ecrit dans ce dossier :
    l'utilisateur doit pouvoir le retirer lui-meme."""
    bibliotheque = tmp_path / "media"
    bibliotheque.mkdir()
    fait_croire_a_root(monkeypatch)
    appels = capture_chown(monkeypatch)
    job = transcode.Job(
        id="j1",
        source=bibliotheque / "Dune (2024).mkv",
        relative_path="Dune (2024).mkv",
        title="Dune",
        target_height=720,
        source_bytes=1,
    )
    modele = bibliotheque.stat()

    sortie = transcode.output_path(job, bibliotheque)

    assert sortie.parent == transcode.staging_root(bibliotheque)
    assert appels == [(sortie.parent, modele.st_uid, modele.st_gid)]


def test_hors_root_les_dossiers_sont_crees_comme_avant(tmp_path: Path) -> None:
    """Aucune regression sur le chemin ordinaire : les dossiers manquants sont
    toujours crees, corbeille comprise, sans que rien ne soit exige d'eux."""
    video = film(tmp_path)

    parti = send_to_trash(video, tmp_path / "corbeille" / "jamais-creee")

    assert parti.is_file()
    assert parti.parent.is_dir()
