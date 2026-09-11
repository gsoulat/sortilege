"""Archive de l'etat : ce qui doit survivre a un conteneur recree.

Tout ce que Sortilege a appris vit dans un seul volume monte — les gabarits,
les decisions tranchees a la main, le journal d'annulation. Un conteneur
reconstruit sans ce volume repart de zero, y compris sur des centaines
d'arbitrages deja rendus, et rien dans le produit ne permettait jusqu'ici d'en
sortir une copie. Radarr sauvegarde et restaure depuis son interface ; ne pas
le faire, c'est demander a l'utilisateur de connaitre le chemin du volume et de
savoir se servir de `tar` sur un NAS.

Trois decisions gouvernent ce module.

**Les secrets ne partent PAS dans l'archive.** Cle TheMovieDB, cle du resolveur
IA, webhook Discord, cle du serveur multimedia, cle d'API de Sortilege, cle
d'API et jeton VIP OpenSubtitles : tous retires du fichier de reglages avant
l'ecriture (liste de reference : ``SECRET_FIELDS``). Une archive se telecharge, se
copie sur un disque externe, se depose dans un nuage et se transmet — y mettre
une cle facturee a l'usage ou un droit d'ecriture sur un canal transformerait
un fichier de secours en fichier a proteger. Le produit ne renvoie jamais un
secret au navigateur ; une archive telechargee par ce meme navigateur ne peut
pas etre l'exception. La contrepartie est assumee et reduite : ces champs ne
sont pas ecrases a la restauration, donc une instance qui a deja ses cles les
garde, et ce sont les seules choses qu'on reobtient en trois clics. Ce qui ne
se reobtient pas — les arbitrages, le journal, les gabarits — est integralement
dans l'archive.

**La politique est ecrite DANS l'archive.** Un `LISEZMOI.txt` a la racine dit
ce qu'elle contient et ce qui en a ete retire. Enterrer cette decision dans un
commentaire de code la rendrait invisible a celui qui, six mois plus tard,
ouvre le zip et se demande pourquoi son webhook ne repond plus.

**Le format porte un numero.** Une archive est un objet qu'on relit longtemps
apres l'avoir faite, souvent depuis une version differente de celle qui l'a
produite. Un manifeste nomme le format et sa version : une archive etrangere ou
un schema inconnu se refusent avec un message, jamais avec une exception au
milieu d'une restauration a moitie faite.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

FORMAT = "sortilege-backup"
"""Marqueur d'origine. C'est lui qui distingue une archive de Sortilege d'un
zip quelconque glisse dans le formulaire par erreur."""

VERSION = 1
"""Numero de schema de l'archive. A incrementer des qu'un fichier change de
sens ou de nom. Contrairement a l'instantane de travail, une archive n'est PAS
reconstructible : le jour ou une migration deviendra necessaire, elle se fera
ici — mais tant qu'il n'y en a pas, une version inconnue se refuse au lieu de
s'interpreter au hasard."""

MANIFEST_NAME = "sortilege-backup.json"
README_NAME = "LISEZMOI.txt"
PAYLOAD_DIR = "data"

PREVIOUS_NAME = "avant-restauration.zip"
"""Filet pose juste avant d'ecraser l'etat. Une restauration est le seul geste
du produit qui detruit sans corbeille ; celui qui se trompe d'archive doit
pouvoir revenir en arriere."""

MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
"""Plafond a la reception. Le contenu reel tient en quelques megaoctets ; au
dela, c'est autre chose qu'une sauvegarde, et le corps est deja en memoire au
moment ou on le mesure."""

MAX_UNPACKED_BYTES = 512 * 1024 * 1024
"""Plafond apres decompression. Un zip de quelques kilo-octets peut se
deployer en gigaoctets : la taille annoncee dans l'archive est verifiee AVANT
d'extraire quoi que ce soit."""

PREFERENCES_NAME = "preferences.json"
DB_NAME = "sortilege.db"
JOURNAL_NAME = "journal.jsonl"


@dataclass(frozen=True)
class Piece:
    """Un fichier du volume, et ce qu'on perd en ne l'ayant plus."""

    name: str
    label: str


PIECES: tuple[Piece, ...] = (
    Piece(PREFERENCES_NAME, "Réglages : sources, destinations, gabarits, seuils"),
    Piece(DB_NAME, "Décisions mémorisées et état de travail"),
    Piece(JOURNAL_NAME, "Journal d'annulation"),
)

BY_NAME = {p.name: p for p in PIECES}

SECRET_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("metadata", "tmdb_api_key", "Clé TheMovieDB"),
    ("ai", "api_key", "Clé du résolveur IA"),
    ("notifications", "webhook_url", "Webhook Discord"),
    ("media_server", "api_key", "Clé du serveur multimédia"),
    ("integration", "api_key", "Clé d'API de Sortilège"),
    ("subtitles", "opensubtitles_api_key", "Clé d'API OpenSubtitles"),
    ("subtitles", "opensubtitles_token", "Jeton VIP OpenSubtitles"),
    # Pas une cle, mais l'adresse publique de la maison : exactement ce que la
    # notification Discord masque. Une instance restauree sans elle reste
    # fermee en politique « exiger » — l'ecart est donc sans risque.
    ("vpn", "reference_ip", "Adresse publique de référence du VPN"),
)
"""Champs retires du fichier de reglages avant l'archivage, et conserves tels
quels a la restauration. Le libelle est celui qui sera lu par un humain dans le
LISEZMOI et dans le compte rendu de restauration — il doit nommer le service,
pas le champ JSON.

Une cle absente de cette liste part EN CLAIR dans une archive annoncee « secrets
exclus », et ecrase celle de l'instance a la restauration : tout nouveau champ
secret des preferences doit y entrer le jour meme ou il est cree."""

COUPES_SANS_SECRET: dict[tuple[str, str], str] = {
    ("notifications", "webhook_url"): "notifications désactivées jusqu'à ce qu'il soit ressaisi",
    ("subtitles", "opensubtitles_api_key"): (
        "recherche de sous-titres désactivée jusqu'à ce qu'elle soit ressaisie"
    ),
}
"""Fonctions qui ne peuvent pas rester actives sans leur secret.

La validation des preferences refuse « active sans cle » pour ces deux blocs. Une
restauration qui les laisserait actives avec un secret vide produirait un
fichier que la validation rejette ensuite en bloc : plus AUCUN enregistrement de
reglages ne passerait, pour une cle que personne n'a encore eu l'occasion de
ressaisir. On les coupe donc, et le compte rendu le dit."""


class BackupError(ValueError):
    """Archive refusee — message destine a l'utilisateur, pas au journal."""


# --- Fabrication ------------------------------------------------------------


def _copie_sqlite(source: Path, destination: Path) -> None:
    """Copie coherente d'une base en cours d'utilisation.

    Un `shutil.copy` sur un fichier SQLite pendant qu'une ecriture est en vol
    produit une base tronquee, et elle ne se decouvre qu'a la restauration —
    c'est-a-dire au pire moment. L'API `backup` de SQLite, elle, prend un
    verrou de lecture et rend une copie utilisable.
    """
    lecture = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
    try:
        cible = sqlite3.connect(destination)
        try:
            lecture.backup(cible)
        finally:
            cible.close()
    finally:
        lecture.close()


def _lit_preferences(source: Path) -> dict:
    try:
        brut = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(
            "Le fichier de réglages est illisible, il ne peut pas être sauvegardé sans risque "
            "d'y laisser une clé. Ouvre Réglages et enregistre une fois pour le réécrire, "
            "puis relance la sauvegarde."
        ) from exc
    if not isinstance(brut, dict):
        raise BackupError("Le fichier de réglages n'a pas la forme attendue.")
    return brut


def _sans_secrets(brut: dict) -> tuple[dict, list[str]]:
    """Retire les secrets, et dit lesquels ont reellement ete retires.

    Un champ vide n'est pas signale : annoncer « clé TheMovieDB retirée » sur
    une installation qui n'en a jamais eu ferait chercher une perte qui n'a pas
    eu lieu.
    """
    propre = json.loads(json.dumps(brut))
    retires: list[str] = []
    for bloc, champ, libelle in SECRET_FIELDS:
        section = propre.get(bloc)
        if not isinstance(section, dict):
            continue
        if str(section.get(champ) or "").strip():
            retires.append(libelle)
        section[champ] = ""
    return propre, retires


def configured_secrets(data_dir: Path) -> list[str]:
    """Libelles des secrets actuellement renseignes, donc de ceux qui seront
    retires de l'archive. Sert a l'annoncer AVANT le telechargement."""
    source = data_dir / PREFERENCES_NAME
    if not source.is_file():
        return []
    try:
        _, retires = _sans_secrets(_lit_preferences(source))
    except BackupError:
        return []
    return retires


def _lisezmoi(manifeste: dict) -> str:
    """Le texte que lira celui qui ouvre le zip six mois plus tard."""
    lignes = [
        "Sauvegarde Sortilège",
        "====================",
        "",
        f"Créée le {manifeste['created_at']} par Sortilège {manifeste['app_version'] or '?'}.",
        f"Format « {manifeste['format']} », version {manifeste['version']}.",
        "",
        "CE QU'ELLE CONTIENT",
    ]
    if manifeste["contents"]:
        lignes += [
            f"  - {c['label']} ({c['name']}, {c['bytes']} octets)" for c in manifeste["contents"]
        ]
    else:
        lignes.append("  - rien : l'installation d'origine n'avait encore aucun état sur disque.")

    lignes += [
        "",
        "CE QU'ELLE NE CONTIENT PAS, VOLONTAIREMENT : LES SECRETS",
    ]
    if manifeste["secrets_removed"]:
        lignes.append("  Ces clés ont été retirées du fichier de réglages avant l'archivage :")
        lignes += [f"    - {s}" for s in manifeste["secrets_removed"]]
    else:
        lignes.append("  Aucune clé n'était renseignée sur l'installation d'origine.")
    lignes += [
        "",
        "  Une archive se télécharge, se copie sur un disque externe, se dépose dans un",
        "  nuage et se transmet. Y mettre une clé facturée à l'usage, un droit d'écriture",
        "  sur un canal Discord ou la clé d'API de Sortilège ferait d'un fichier de secours",
        "  un fichier à protéger. Sortilège ne renvoie jamais un secret au navigateur ;",
        "  cette archive suit la même règle.",
        "",
        "  Conséquence à la restauration : ces champs ne sont PAS écrasés. Une instance qui",
        "  a déjà ses clés les garde. Une instance neuve les laisse vides — ressaisis-les",
        "  dans Réglages. Tout le reste est restauré à l'identique, et c'est précisément ce",
        "  qui ne se retrouve pas en trois clics : les arbitrages, le journal, les gabarits.",
        "",
        "RESTAURER",
        "  Interface : Réglages → Système → Sauvegarde.",
        "  Ligne de commande :",
        '    curl -X POST "http://NAS:8117/api/backup/restore?confirm=true" \\',
        '         -H "X-Api-Key: <ta-clé>" \\',
        '         -H "Content-Type: application/zip" \\',
        "         --data-binary @cette-archive.zip",
        "",
        "  La restauration ÉCRASE l'état en place. L'état précédent est malgré tout",
        f"  conservé dans « {PREVIOUS_NAME} », à la racine du volume de données.",
        "",
    ]
    return "\n".join(lignes)


def build(data_dir: Path, *, app_version: str = "") -> bytes:
    """Fabrique l'archive complete, en memoire.

    En memoire et non sur disque : l'archive part directement dans la reponse
    HTTP, et un fichier temporaire de plus dans le volume serait un fichier de
    plus a nettoyer le jour ou le telechargement echoue.
    """
    contenus: list[dict[str, object]] = []
    retires: list[str] = []
    tampon = io.BytesIO()

    with (
        tempfile.TemporaryDirectory(prefix="sortilege-backup-") as tmp,
        zipfile.ZipFile(tampon, "w", zipfile.ZIP_DEFLATED) as archive,
    ):
        for piece in PIECES:
            source = data_dir / piece.name
            if not source.is_file():
                continue
            if piece.name == PREFERENCES_NAME:
                propre, ces_secrets = _sans_secrets(_lit_preferences(source))
                retires.extend(ces_secrets)
                brut = json.dumps(propre, indent=2, ensure_ascii=False).encode("utf-8")
            elif piece.name == DB_NAME:
                copie = Path(tmp) / DB_NAME
                _copie_sqlite(source, copie)
                brut = copie.read_bytes()
            else:
                brut = source.read_bytes()

            archive.writestr(f"{PAYLOAD_DIR}/{piece.name}", brut)
            contenus.append({"name": piece.name, "label": piece.label, "bytes": len(brut)})

        manifeste = {
            "format": FORMAT,
            "version": VERSION,
            "app_version": app_version,
            "created_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "secrets_included": False,
            "secrets_removed": retires,
            "contents": contenus,
        }
        archive.writestr(MANIFEST_NAME, json.dumps(manifeste, indent=2, ensure_ascii=False))
        archive.writestr(README_NAME, _lisezmoi(manifeste))

    return tampon.getvalue()


def filename(app_version: str = "") -> str:
    """Nom propose au telechargement, date pour que deux archives ne se
    recouvrent pas dans un dossier de telechargements."""
    horodatage = datetime.now(UTC).strftime("%Y-%m-%d-%H%M")
    suffixe = f"-{app_version}" if app_version else ""
    return f"sortilege{suffixe}-{horodatage}.zip"


# --- Relecture --------------------------------------------------------------


def _refus_de_version(trouvee: object) -> str:
    if isinstance(trouvee, int) and trouvee > VERSION:
        return (
            f"Archive au format {trouvee}, cette installation lit le format {VERSION}. "
            "Elle a été produite par une version plus récente de Sortilège : mets "
            "l'application à jour avant de restaurer."
        )
    return (
        f"Archive au format {trouvee!r}, cette installation lit le format {VERSION}. "
        "Le format a changé depuis et il n'existe pas de conversion : reprends une "
        "sauvegarde faite par la version en place."
    )


def read_archive(blob: bytes) -> tuple[dict, dict[str, bytes], list[str]]:
    """Ouvre, valide, et rend le manifeste avec les fichiers reconnus.

    Rien n'est ecrit sur disque ici : c'est ce qui permet a l'inspection et a
    la restauration de partager exactement les memes controles, et donc a
    l'utilisateur de savoir avant de confirmer ce qu'il s'apprete a ecraser.
    """
    if not blob:
        raise BackupError(
            "Aucune archive reçue. Envoie le fichier .zip dans le corps de la requête."
        )
    if len(blob) > MAX_ARCHIVE_BYTES:
        raise BackupError(
            f"Archive trop volumineuse ({len(blob) // (1024 * 1024)} Mo, maximum "
            f"{MAX_ARCHIVE_BYTES // (1024 * 1024)} Mo). Une sauvegarde de Sortilège pèse "
            "quelques mégaoctets : vérifie que c'est bien le bon fichier."
        )

    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise BackupError(
            "Ce fichier n'est pas une archive ZIP. Reprends celle produite par "
            "Réglages → Système → Sauvegarde."
        ) from exc

    with archive:
        if sum(info.file_size for info in archive.infolist()) > MAX_UNPACKED_BYTES:
            raise BackupError(
                "Le contenu décompressé de cette archive dépasse la taille admise. "
                "Elle ne vient pas de Sortilège."
            )

        try:
            manifeste = json.loads(archive.read(MANIFEST_NAME))
        except KeyError as exc:
            raise BackupError(
                f"Archive étrangère : « {MANIFEST_NAME} » est absent. Ce zip ne vient pas "
                "de Sortilège — reprends celui produit par Réglages → Système → Sauvegarde."
            ) from exc
        except ValueError as exc:
            raise BackupError(
                f"Le manifeste « {MANIFEST_NAME} » est illisible : l'archive est abîmée."
            ) from exc

        if not isinstance(manifeste, dict) or manifeste.get("format") != FORMAT:
            raise BackupError(
                "Archive étrangère : le manifeste n'annonce pas une sauvegarde Sortilège. "
                "Reprends celle produite par Réglages → Système → Sauvegarde."
            )
        if manifeste.get("version") != VERSION:
            raise BackupError(_refus_de_version(manifeste.get("version")))

        # Seuls les noms attendus sont extraits, et ils sont compares a une
        # liste close. C'est ce qui rend une entree « ../../etc/passwd »
        # inoperante par construction, plutot que par un assainissement qu'il
        # faudrait avoir raison de faire a chaque fois.
        fichiers: dict[str, bytes] = {}
        ignores: list[str] = []
        for info in archive.infolist():
            if info.filename in (MANIFEST_NAME, README_NAME):
                continue
            attendu = info.filename.removeprefix(f"{PAYLOAD_DIR}/")
            if info.filename.startswith(f"{PAYLOAD_DIR}/") and attendu in BY_NAME:
                fichiers[attendu] = archive.read(info)
            else:
                ignores.append(info.filename)

    return manifeste, fichiers, ignores


def inspect(blob: bytes) -> dict[str, object]:
    """Ce que contient une archive, sans rien ecraser.

    Permet a la confirmation de porter sur un contenu reel plutot que sur une
    promesse : « restaurer 312 décisions et un journal du 3 août » se decide,
    « restaurer » se clique par reflexe.
    """
    manifeste, fichiers, ignores = read_archive(blob)
    return {
        "manifest": manifeste,
        "contents": [
            {"name": nom, "label": BY_NAME[nom].label, "bytes": len(donnees)}
            for nom, donnees in fichiers.items()
        ],
        "ignored": ignores,
        "secrets_included": bool(manifeste.get("secrets_included")),
        "secrets_removed": list(manifeste.get("secrets_removed") or []),
    }


# --- Restauration -----------------------------------------------------------


def _secrets_en_place(source: Path) -> dict[tuple[str, str], tuple[str, str]]:
    """Secrets actuellement configures, indexes par (bloc, champ)."""
    if not source.is_file():
        return {}
    try:
        brut = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(brut, dict):
        return {}

    garde: dict[tuple[str, str], tuple[str, str]] = {}
    for bloc, champ, libelle in SECRET_FIELDS:
        section = brut.get(bloc)
        if isinstance(section, dict) and str(section.get(champ) or "").strip():
            garde[(bloc, champ)] = (str(section[champ]), libelle)
    return garde


def _reinjecte_secrets(
    donnees: bytes, en_place: dict, retires: set[str] | None = None
) -> tuple[bytes, list[str], list[str]]:
    """Remet dans les reglages restaures les secrets de l'instance courante.

    Sans cela, restaurer effacerait la cle TheMovieDB d'une instance qui
    fonctionne — l'utilisateur verrait son identification s'arreter net pour
    avoir voulu recuperer ses gabarits.

    ``retires`` : les libelles que le manifeste de l'archive declare retires,
    c'est-a-dire les secrets que l'instance d'origine AVAIT. Seuls ceux-la
    peuvent manquer. ``None`` (archive sans manifeste de secrets) garde
    l'ancienne conduite : tout secret vide est signale.
    """
    try:
        prefs = json.loads(donnees)
    except ValueError as exc:
        raise BackupError(
            "Le fichier de réglages contenu dans l'archive est illisible : elle est abîmée."
        ) from exc
    if not isinstance(prefs, dict):
        raise BackupError(
            "Le fichier de réglages contenu dans l'archive n'a pas la forme attendue."
        )

    conserves: list[str] = []
    manquants: list[str] = []
    for bloc, champ, libelle in SECRET_FIELDS:
        section = prefs.get(bloc)
        if not isinstance(section, dict):
            section = {}
            prefs[bloc] = section
        valeur, _ = en_place.get((bloc, champ), ("", ""))
        # Une archive produite avant qu'un champ soit classe secret le porte EN
        # CLAIR sans le declarer : il compte comme declare, sinon il serait
        # efface a la restauration sans un mot.
        porte = bool(str(section.get(champ) or "").strip())
        section[champ] = valeur
        if valeur:
            conserves.append(libelle)
            continue

        effet = COUPES_SANS_SECRET.get((bloc, champ))
        if effet and section.get("enabled"):
            # Voir COUPES_SANS_SECRET : active sans son secret, le bloc ferait
            # refuser tout enregistrement ulterieur des reglages.
            section["enabled"] = False
            manquants.append(f"{libelle} ({effet})")
            continue
        # Un secret que l'archive ne portait pas n'a pas « manque » : l'instance
        # d'origine ne l'avait pas non plus. Le signaler ferait lire, a chaque
        # restauration, la liste des services jamais configures.
        if retires is None or libelle in retires or porte:
            manquants.append(libelle)

    return json.dumps(prefs, indent=2, ensure_ascii=False).encode("utf-8"), conserves, manquants


def _pose_le_filet(data_dir: Path, app_version: str) -> str:
    """Ecrit « avant-restauration.zip », ou refuse la restauration.

    Refuse, et non « continue en le signalant » : la restauration est le seul
    geste du produit qui detruit sans corbeille. La laisser passer sans filet
    reviendrait a promettre un retour arriere qui n'existe pas — et le compte
    rendu qui le signalerait serait lu apres coup, une fois l'etat ecrase.

    Le filet lui-meme passe par un fichier temporaire : un zip tronque ne doit
    pas remplacer un filet precedent encore valable.
    """
    cible = data_dir / PREVIOUS_NAME
    provisoire = cible.with_name(f"{PREVIOUS_NAME}.tmp")
    try:
        provisoire.write_bytes(build(data_dir, app_version=app_version))
        os.replace(provisoire, cible)
    except (OSError, BackupError) as exc:
        with contextlib.suppress(OSError):
            provisoire.unlink(missing_ok=True)
        logger.warning("filet de restauration non ecrit, restauration refusee : %s", exc)
        cause = (
            str(exc)
            if isinstance(exc, BackupError)
            else (
                f"Cause : {exc}. Vérifie que le volume de données est monté en écriture et "
                "qu'il y reste de la place."
            )
        )
        raise BackupError(
            f"Restauration refusée : la copie de l'état en place (« {PREVIOUS_NAME} ») n'a "
            "pas pu être écrite, et sans elle cette restauration ne pourrait pas être "
            f"annulée. Rien n'a été modifié. {cause}"
        ) from exc
    return PREVIOUS_NAME


def restore(blob: bytes, data_dir: Path, *, app_version: str = "") -> dict[str, object]:
    """Ecrase l'etat en place par celui de l'archive.

    L'ordre compte : on valide tout, on pose le filet, on ecrit ensuite. Une
    archive refusee n'a alors touche a rien, et une archive acceptee laisse
    derriere elle de quoi revenir.
    """
    manifeste, fichiers, ignores = read_archive(blob)
    if not fichiers:
        raise BackupError(
            "Cette archive ne contient aucun fichier restaurable. Elle a probablement été "
            "produite sur une installation qui n'avait encore rien enregistré."
        )

    # Tout ce qui peut encore refuser l'archive passe AVANT la premiere
    # ecriture, filet compris : des reglages illisibles dans le zip ne doivent
    # pas avoir remplace un filet precedent pour rien.
    en_place = _secrets_en_place(data_dir / PREFERENCES_NAME)
    conserves: list[str] = []
    manquants: list[str] = []
    if PREFERENCES_NAME in fichiers:
        # Les secrets que l'instance d'origine avait reellement : seuls ceux-la
        # peuvent « manquer ». Une archive sans cette liste garde l'ancienne conduite.
        declares = manifeste.get("secrets_removed") if isinstance(manifeste, dict) else None
        retires = set(declares) if isinstance(declares, list) else None
        fichiers[PREFERENCES_NAME], conserves, manquants = _reinjecte_secrets(
            fichiers[PREFERENCES_NAME], en_place, retires
        )

    data_dir.mkdir(parents=True, exist_ok=True)
    filet = _pose_le_filet(data_dir, app_version)

    # Deux temps. D'abord TOUS les fichiers temporaires : un disque plein ou un
    # droit manquant se decouvre ici, alors que rien n'a encore ete remplace, et
    # les temporaires deja ecrits sont retires. Ensuite les remplacements a la
    # suite — de simples renommages dans le meme dossier, qui n'echouent
    # pratiquement plus. Remplacer au fil de l'ecriture pouvait laisser des
    # reglages neufs a cote d'un journal ancien, un melange qu'aucune des deux
    # versions n'a jamais connu.
    provisoires: list[tuple[Path, Path]] = []
    try:
        for nom, donnees in fichiers.items():
            cible = data_dir / nom
            provisoire = cible.with_name(f"{nom}.restauration")
            provisoires.append((provisoire, cible))
            provisoire.write_bytes(donnees)
    except OSError:
        for provisoire, _ in provisoires:
            with contextlib.suppress(OSError):
                provisoire.unlink(missing_ok=True)
        raise

    restaures: list[str] = []
    for provisoire, cible in provisoires:
        os.replace(provisoire, cible)
        restaures.append(cible.name)

    logger.info("restauration : %s fichier(s) remplace(s)", len(restaures))
    return {
        "restored": [{"name": n, "label": BY_NAME[n].label} for n in restaures],
        "ignored": ignores,
        "secrets_kept": conserves,
        "secrets_missing": manquants,
        "previous_saved_as": filet,
        "manifest": manifeste,
    }
