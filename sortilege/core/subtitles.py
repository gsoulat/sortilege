"""Les sous-titres qui manquent : lesquels, sous quel nom, dans quel format.

Sortilege savait deja DEPLACER un sous-titre trouve a cote d'une video
(``core/companions``). Il ne savait pas en chercher un quand il n'y en avait
pas — c'est precisement le trou que FileBot et tinyMediaManager comblent, et
dont Bazarr a fait un produit entier.

Trois exigences gouvernent ce module, et chacune vient d'un echec observable :

1. **Le nom fait foi.** Jellyfin ne lit pas le contenu d'un sous-titre pour en
   deviner la langue : il lit le NOM du fichier. Un « Film.francais.srt » est
   depose, visible dans le dossier, et absent du lecteur. Le fichier existe
   sans exister — la pire des deux issues, parce qu'on ne le cherchera plus.

2. **Le format doit etre du SubRip en UTF-8.** Ce qui arrive des bases
   publiques est ecrit depuis vingt ans par tout le monde : du WebVTT, du
   SubStation Alpha, et du SubRip encode en Windows-1252 dont les accents
   s'affichent en losanges. Convertir est ici du rangement, pas de la
   cosmetique. Sans dependance nouvelle : un decodage tolerant et deux
   conversions de texte couvrent l'essentiel, et ce qu'on ne sait pas convertir
   est conserve INTACT — un fichier exotique se lit peut-etre, un fichier
   corrompu jamais.

3. **Un fichier present est un fichier juste.** Ne jamais ecraser sans le dire,
   ne jamais laisser un fichier a moitie ecrit. Un « .srt » tronque par une
   coupure est pire qu'un « .srt » absent : il se presente comme present, on ne
   le redemandera plus, et il s'arretera au milieu du film.

Le module ne connait ni fournisseur ni preferences. Il recoit les langues
voulues et une source qui respecte ``SubtitleSource`` — c'est ce qui permet de
le tester sans reseau, et de changer de base de sous-titres sans y toucher.
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .companions import SUBTITLE_EXTENSIONS
from .filehash import compute as _empreinte_du_fichier
from .probe import FileProbe

logger = logging.getLogger(__name__)

FORCED_TAG = "forced"

HEARING_IMPAIRED_TAG = "sdh"
"""On ecrit « sdh », JAMAIS « hi ».

Jellyfin accepte les deux, mais « hi » est aussi le code de la langue hindi, et
sa documentation avertit du piege : « Film.hi.srt » est lu comme un sous-titre
HINDI, pas comme un sous-titre pour malentendants. Le meme jeton place apres une
langue — « Film.en.hi.srt » — reprend son sens de modificateur.

Choisir « sdh » a l'ecriture rend l'ambiguite structurellement impossible ; a la
LECTURE, elle existe toujours et se traite explicitement (voir ``read_tag``)."""

_MODIFICATEURS_FORCES = {"forced", "foreign"}
_MODIFICATEURS_SOURDS = {"sdh", "cc"}
_MODIFICATEURS_IGNORES = {"default", "und", "undefined"}

_ALIAS_LANGUES = {
    "fre": "fr",
    "fra": "fr",
    "french": "fr",
    "francais": "fr",
    "eng": "en",
    "english": "en",
    "ger": "de",
    "deu": "de",
    "spa": "es",
    "ita": "it",
    "por": "pt",
    "pob": "pt-br",
    "dut": "nl",
    "nld": "nl",
    "jpn": "ja",
    "chi": "zh",
    "zho": "zh",
    "kor": "ko",
    "rus": "ru",
    "ara": "ar",
    "hin": "hi",
    "pol": "pl",
    "swe": "sv",
    "dan": "da",
    "nor": "no",
    "fin": "fi",
    "tur": "tr",
    "gre": "el",
    "ell": "el",
    "heb": "he",
    "cze": "cs",
    "ces": "cs",
    "ukr": "uk",
    "rum": "ro",
    "ron": "ro",
    "hun": "hu",
    "vie": "vi",
    "tha": "th",
    "ind": "id",
    "may": "ms",
    "msa": "ms",
    "bul": "bg",
    "hrv": "hr",
    "srp": "sr",
    "slo": "sk",
    "slk": "sk",
    "slv": "sl",
    "est": "et",
    "lav": "lv",
    "lit": "lt",
    "cat": "ca",
    "fas": "fa",
    "per": "fa",
}

_CODES_CONNUS = set(_ALIAS_LANGUES.values()) | {
    "ar", "bg", "ca", "cs", "da", "de", "el", "en", "es", "et", "fa", "fi", "fr", "he", "hi",
    "hr", "hu", "id", "is", "it", "ja", "ko", "lt", "lv", "ms", "nl", "no", "pl", "pt", "ro",
    "ru", "sk", "sl", "sr", "sv", "th", "tr", "uk", "vi", "zh",
}  # fmt: skip

_REGIONS_UTILES = {"pt-br", "zh-cn", "zh-tw", "es-mx"}
"""Variantes ou la region change la traduction, pas seulement l'orthographe.

Ailleurs on jette la region : « fr-FR » et « fr-CA » designent le meme
sous-titre pour un spectateur, et les preferences stockent deja la langue des
metadonnees sous la forme « fr-FR »."""


class Outcome(StrEnum):
    WRITTEN = "written"
    EXISTS = "exists"
    """Un fichier occupait deja la place. Rien n'a ete ecrit, et ce n'est pas
    une erreur : c'est un refus delibere, qui doit remonter jusqu'a l'humain."""

    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class WriteResult:
    outcome: Outcome
    path: Path
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is Outcome.WRITTEN


@dataclass(frozen=True, slots=True)
class SubtitleTag:
    """Ce qu'un nom de fichier declare : une langue et deux modificateurs."""

    language: str = ""
    forced: bool = False
    hearing_impaired: bool = False


class SubtitleOffer(Protocol):
    """Le minimum qu'un fournisseur doit exposer pour qu'on sache nommer."""

    language: str
    forced: bool
    hearing_impaired: bool
    extension: str


class SubtitleSource(Protocol):
    """Ce que ``core`` attend d'une base de sous-titres.

    Un protocole plutot qu'un import de ``providers`` : le noyau n'a pas a
    connaitre OpenSubtitles, et un test n'a pas a simuler du HTTP pour verifier
    une regle de nommage.
    """

    async def find(
        self,
        *,
        moviehash: str = "",
        title: str = "",
        year: int | None = None,
        season: int | None = None,
        episode: int | None = None,
        languages: tuple[str, ...] | list[str] = (),
    ) -> list[SubtitleOffer]: ...

    async def download(self, candidate: SubtitleOffer) -> bytes | None: ...


# --- Langues ----------------------------------------------------------------


def normalise_language(code: str) -> str:
    """Ramene un code de langue a la forme qu'on ecrira dans les noms.

    Tout se croise ici : « fre » du conteneur MKV, « fr-FR » des preferences,
    « French » d'un nom de release. Sans mise a plat, la meme langue serait
    comptee absente trois fois et telechargee trois fois.
    """
    brut = code.strip().lower().replace("_", "-")
    if not brut:
        return ""
    if brut in _ALIAS_LANGUES:
        return _ALIAS_LANGUES[brut]
    if brut in _REGIONS_UTILES:
        return brut
    if "-" in brut:
        base = brut.split("-", 1)[0]
        return _ALIAS_LANGUES.get(base, base)
    return brut


def is_language_code(code: str) -> bool:
    """Ce code a-t-il la FORME d'une langue ? Pour valider une saisie.

    Distinct de ``_est_une_langue``, et la difference compte. Celui-la lit un
    jeton pris dans un nom de fichier, ou « vff » et « yts » trainent : il ne
    peut accepter qu'une liste connue, sous peine de croire une langue deja
    presente et de ne jamais la telecharger.

    Celui-ci lit ce qu'un utilisateur a tape dans ses reglages. La liste connue
    y serait un mauvais juge : elle couvre une quarantaine de langues, et
    refuser « mk » parce qu'on n'y avait pas pense priverait quelqu'un de la
    sienne pour rien. On verifie donc la forme — deux lettres, eventuellement
    suivies d'une region — ce qui ecarte « klingon » sans borner personne.
    """
    brut = normalise_language(code)
    if not brut:
        return False
    langue, _, region = brut.partition("-")
    if len(langue) != 2 or not langue.isalpha():
        return False
    return not region or (len(region) == 2 and region.isalpha())


def _est_une_langue(jeton: str) -> bool:
    """Ce jeton est-il une langue, ou le nom du groupe de release ?

    On n'accepte que des codes CONNUS. Prendre pour une langue un jeton
    inconnu — « yts », « vff » — ferait croire que la langue est deja presente
    et empecherait de la telecharger ; l'inverse ne coute qu'un fichier de plus,
    qui ne recouvrira rien puisqu'on n'ecrase jamais.
    """
    return normalise_language(jeton) in _CODES_CONNUS


def read_tag(video_stem: str, subtitle: Path) -> SubtitleTag | None:
    """Lit « .en.hi.srt » et repond « anglais, malentendants ».

    Le piege documente par Jellyfin se traite ICI, et il tient en une regle :
    « hi » vaut hindi tant qu'aucune langue n'a ete lue, et malentendants
    ensuite. « Film.hi.srt » est donc du hindi, « Film.en.hi.srt » de l'anglais
    pour malentendants — se tromper reviendrait a redemander eternellement un
    hindi qu'on possede, ou a traiter un fichier hindi comme un doublon anglais.
    """
    nom = subtitle.name
    if not nom.lower().startswith(video_stem.lower()):
        return None

    reste = nom[len(video_stem) :]
    jetons = [j for j in reste.split(".") if j]
    if not jetons:
        return None
    # Le dernier jeton est l'extension ; elle a deja fait son office.
    jetons = [j.lower() for j in jetons[:-1]]

    langue = ""
    forced = False
    sourds = False

    for jeton in jetons:
        if jeton in _MODIFICATEURS_FORCES:
            forced = True
        elif jeton in _MODIFICATEURS_SOURDS:
            sourds = True
        elif jeton == "hi":
            if langue:
                sourds = True
            else:
                langue = "hi"
        elif not langue and jeton not in _MODIFICATEURS_IGNORES and _est_une_langue(jeton):
            langue = normalise_language(jeton)

    return SubtitleTag(language=langue, forced=forced, hearing_impaired=sourds)


def external_tags(video: Path) -> list[SubtitleTag]:
    """Ce que declarent les sous-titres deja poses a cote de la video."""
    dossier = video.parent
    if not dossier.is_dir():
        return []

    stem = video.stem
    trouves: list[SubtitleTag] = []
    try:
        voisins = sorted(dossier.iterdir())
    except OSError as exc:
        logger.warning("lecture du dossier impossible (%s)", exc)
        return []

    for voisin in voisins:
        if not voisin.is_file() or voisin.suffix.lower() not in SUBTITLE_EXTENSIONS:
            continue
        if (tag := read_tag(stem, voisin)) is not None:
            trouves.append(tag)
    return trouves


def container_subtitle_languages(probe: FileProbe | None) -> set[str]:
    """Langues des pistes de sous-titres INCLUSES dans le conteneur.

    Remontees par ``probe._fill_streams``, pistes forcees exclues. Sans elles,
    un MKV qui embarque deja un sous-titre francais se verrait proposer un
    « .fr.srt » externe — redondant, jamais destructeur, mais Jellyfin
    afficherait deux pistes pour une.

    Lu sans exiger l'attribut : une sonde partielle, construite ailleurs que
    par ``core/probe``, n'a pas a le porter.
    """
    if probe is None:
        return set()
    brut = getattr(probe, "subtitle_languages", None) or ()
    return {langue for code in brut if (langue := normalise_language(str(code)))}


def existing_languages(
    video: Path,
    *,
    probe: FileProbe | None = None,
    container_languages: Sequence[str] = (),
) -> set[str]:
    """Langues deja disponibles pour cette video, externes et internes.

    Un sous-titre FORCE ne compte pas. Il ne traduit que les dialogues en langue
    etrangere — quelques repliques sur deux heures — et le compter comme
    presence francaise priverait definitivement le spectateur du vrai
    sous-titre. C'est l'erreur classique, et elle est silencieuse.
    """
    langues = {tag.language for tag in external_tags(video) if tag.language and not tag.forced}
    langues |= container_subtitle_languages(probe)
    langues |= {langue for code in container_languages if (langue := normalise_language(code))}
    return langues


def missing_languages(
    video: Path,
    wanted: Sequence[str],
    *,
    probe: FileProbe | None = None,
    container_languages: Sequence[str] = (),
    audio_is_enough: bool = False,
) -> list[str]:
    """Langues demandees et absentes, dans l'ordre de preference recu.

    L'ordre est conserve parce qu'il porte une intention : une bibliotheque
    francaise veut le francais d'abord, et l'anglais en second choix. Le rendre
    en ensemble desordonne perdrait cette hierarchie.

    ``audio_is_enough`` est faux par defaut, et ce defaut est un choix : une
    piste audio francaise ne remplace un sous-titre francais que pour ceux qui
    entendent. Le reglage existe pour ceux qui ne veulent pas de sous-titres sur
    ce qu'ils comprennent a l'oreille.
    """
    presentes = existing_languages(video, probe=probe, container_languages=container_languages)
    if audio_is_enough and probe is not None:
        presentes |= {
            langue for code in (probe.audio_languages or ()) if (langue := normalise_language(code))
        }

    manquantes: list[str] = []
    for code in wanted:
        langue = normalise_language(code)
        if langue and langue not in presentes and langue not in manquantes:
            manquantes.append(langue)
    return manquantes


# --- Nommage ----------------------------------------------------------------


def subtitle_path(
    video: Path,
    language: str,
    *,
    forced: bool = False,
    hearing_impaired: bool = False,
    extension: str = ".srt",
) -> Path:
    """Le chemin exact ou Jellyfin ira chercher ce sous-titre.

    A COTE de la video et portant son nom : c'est la seule disposition que les
    trois lecteurs (Jellyfin, Plex, Kodi) lisent sans configuration. Un
    sous-dossier « subs/ » demande un reglage que personne n'active.

    Le seul jeton ambigu du format — « hi » — n'est jamais ecrit ici : voir
    ``HEARING_IMPAIRED_TAG``. Une langue hindi produit bien « .hi.srt », et un
    hindi pour malentendants « .hi.sdh.srt », ou le premier jeton est la langue
    et le second son modificateur.
    """
    suffixe = extension if extension.startswith(".") else f".{extension}"
    morceaux = [video.stem]

    if langue := normalise_language(language):
        morceaux.append(langue)
    if forced:
        morceaux.append(FORCED_TAG)
    if hearing_impaired:
        morceaux.append(HEARING_IMPAIRED_TAG)

    return video.with_name(".".join(morceaux) + suffixe.lower())


# --- Conversion -------------------------------------------------------------

CONVERTIBLES = {".srt", ".vtt", ".ass", ".ssa"}
"""Formats TEXTE qu'on sait relire et reecrire.

Tout le reste — VobSub (.sub/.idx), PGS, formats rares — est binaire ou trop
particulier. On le depose tel quel : un sous-titre exotique se lira peut-etre,
un sous-titre corrompu par une conversion approximative jamais."""

_ENCODAGES = ("utf-8", "cp1252", "latin-1")

_HORODATAGE_VTT = re.compile(
    r"(\d{1,2}:)?(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{1,2}:)?(\d{2}):(\d{2})[.,](\d{1,3})"
)
_BALISES = re.compile(r"<[^>]+>")
_ACCOLADES = re.compile(r"\{[^}]*\}")
_HORODATAGE_ASS = re.compile(r"(\d+):(\d{2}):(\d{2})[.,](\d{1,3})")

_DESSIN_ASS = re.compile(r"\\p[1-9]")
"""Mode dessin vectoriel, et LUI SEUL.

Chercher « \\p » tout court paraissait suffisant : c'est faux, et joliment
piegeux — « \\pos », la commande de positionnement, commence par les memes deux
caracteres et se trouve sur presque toutes les repliques d'un ASS soigne. Le
raccourci jetait donc l'essentiel du fichier, et rendait un SubRip vide."""


def decode(raw: bytes) -> str:
    """Texte, quel que soit l'encodage d'origine. Ne leve jamais.

    L'ordre des tentatives est le seul point delicat : ``latin-1`` accepte
    N'IMPORTE QUELLE suite d'octets sans jamais protester. Le placer ailleurs
    qu'en dernier ferait passer pour du texte occidental un fichier UTF-8
    parfaitement valide, dont chaque accent deviendrait deux caracteres.

    Les alphabets non latins sans BOM (cyrillique en cp1251, grec en cp1253)
    restent hors de portee : les reconnaitre demande une detection statistique,
    donc une dependance. Ils sortent lisibles a l'octet pres, mais illisibles a
    l'ecran — cas assume, plutot qu'une devinette qui abimerait les autres.
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    # UTF-16 sans BOM : un texte latin y produit un octet nul sur deux, ce
    # qu'aucun encodage sur un octet ne fait jamais.
    tete = raw[:512]
    if tete and tete.count(0) > len(tete) // 4:
        return raw.decode("utf-16", errors="replace")

    for encodage in _ENCODAGES:
        try:
            return raw.decode(encodage)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace")


def to_subrip(raw: bytes, extension: str = ".srt") -> tuple[bytes, str]:
    """Contenu en SubRip UTF-8, et l'extension a lui donner.

    Renvoie l'original INCHANGE quand le format n'est pas reconnu, ou quand la
    conversion ne produit rien d'exploitable. Le principe est le meme que pour
    la corbeille : en cas de doute, ne rien abimer.
    """
    suffixe = (extension if extension.startswith(".") else f".{extension}").lower()
    if suffixe not in CONVERTIBLES:
        return raw, suffixe

    texte = decode(raw)
    if suffixe == ".srt":
        corps = _normalise_srt(texte)
    elif suffixe == ".vtt":
        corps = _vtt_vers_srt(texte)
    else:
        corps = _ass_vers_srt(texte)

    if not corps.strip():
        # On a cru savoir lire, et on n'a rien produit : le fichier n'a
        # peut-etre que l'extension d'un format qu'il n'a pas.
        logger.info("conversion en SubRip sans resultat, contenu conserve tel quel")
        return raw, suffixe

    return corps.encode("utf-8"), ".srt"


def _normalise_srt(texte: str) -> str:
    """Un SubRip qui l'est deja : il ne reste que l'encodage et les fins de ligne."""
    propre = texte.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return propre.strip("\n") + "\n"


def _vtt_vers_srt(texte: str) -> str:
    """WebVTT vers SubRip.

    Trois differences seulement, mais aucune n'est facultative : le point
    decimal des horodatages devient une virgule, les blocs sont numerotes, et
    les reglages de placement (« align:start position:10% ») doivent disparaitre
    — un lecteur SubRip les afficherait comme du texte, en plein milieu de
    l'image.
    """
    lignes = _normalise_srt(texte).split("\n")
    blocs: list[str] = []
    courant: list[str] = []
    horodatage = ""

    def cloturer() -> None:
        nonlocal horodatage, courant
        if horodatage and courant:
            blocs.append(f"{len(blocs) + 1}\n{horodatage}\n" + "\n".join(courant))
        horodatage = ""
        courant = []

    for ligne in lignes:
        nue = ligne.strip()
        if nue.startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            cloturer()
            continue
        if not nue:
            cloturer()
            continue
        if (converti := _horodatage_vtt(nue)) is not None:
            # Une ligne d'horodatage cloture le bloc precedent : certains
            # fichiers n'ont pas de ligne vide entre deux repliques.
            cloturer()
            horodatage = converti
            continue
        if horodatage:
            courant.append(_texte_propre(ligne))
        # Sinon : identifiant de bloc, qu'on remplace par notre numerotation.

    cloturer()
    return "\n\n".join(blocs) + "\n" if blocs else ""


def _horodatage_vtt(ligne: str) -> str | None:
    trouve = _HORODATAGE_VTT.match(ligne)
    if trouve is None:
        return None
    debut = _mmss(trouve.group(1), trouve.group(2), trouve.group(3), trouve.group(4))
    fin = _mmss(trouve.group(5), trouve.group(6), trouve.group(7), trouve.group(8))
    return f"{debut} --> {fin}"


def _mmss(heures: str | None, minutes: str, secondes: str, fraction: str) -> str:
    """Horodatage SubRip complet.

    WebVTT autorise d'omettre les heures ; SubRip ne l'autorise pas, et un
    lecteur qui recoit « 01:23,000 » cale la replique n'importe ou.
    """
    h = int((heures or "0").rstrip(":") or 0)
    return f"{h:02d}:{minutes}:{secondes},{fraction.ljust(3, '0')[:3]}"


def _ass_vers_srt(texte: str) -> str:
    """SubStation Alpha vers SubRip.

    L'ASS est un format de mise en scene : positions, polices, karaoke,
    dessins vectoriels. On n'en garde que ce que SubRip sait porter — le texte
    et son minutage — et c'est une perte assumee : un ASS integral affiche par
    un lecteur qui l'ignore est un mur de balises par-dessus l'image.

    L'ordre des colonnes est LU dans la ligne « Format: » plutot que suppose :
    il varie d'un fichier a l'autre, et se tromper de colonne place le texte a
    la place du nom de style.
    """
    colonnes: list[str] = []
    repliques: list[tuple[str, str, str]] = []

    for ligne in _normalise_srt(texte).split("\n"):
        nue = ligne.strip()
        if nue.lower().startswith("format:") and not colonnes:
            colonnes = [c.strip().lower() for c in nue.split(":", 1)[1].split(",")]
            continue
        if not nue.lower().startswith("dialogue:") or not colonnes:
            continue

        # maxsplit : le texte est la derniere colonne et contient lui-meme des
        # virgules. Un split naif les prendrait pour des separateurs et
        # tronquerait la replique a sa premiere ponctuation.
        valeurs = nue.split(":", 1)[1].split(",", len(colonnes) - 1)
        if len(valeurs) < len(colonnes):
            continue
        champs = dict(zip(colonnes, valeurs, strict=False))

        debut = _horodatage_ass(champs.get("start", ""))
        fin = _horodatage_ass(champs.get("end", ""))
        brut = champs.get("text", "")
        if debut is None or fin is None:
            continue
        if _DESSIN_ASS.search(brut):
            # Commande de dessin vectoriel : son « texte » est une suite de
            # coordonnees, qui s'afficherait comme un charabia de chiffres.
            continue

        contenu = _texte_propre(_ACCOLADES.sub("", brut).replace("\\N", "\n").replace("\\n", "\n"))
        contenu = contenu.replace("\\h", " ").strip()
        if contenu:
            repliques.append((debut, fin, contenu))

    repliques.sort(key=lambda r: r[0])
    blocs = [f"{i}\n{d} --> {f}\n{t}" for i, (d, f, t) in enumerate(repliques, start=1)]
    return "\n\n".join(blocs) + "\n" if blocs else ""


def _horodatage_ass(valeur: str) -> str | None:
    trouve = _HORODATAGE_ASS.fullmatch(valeur.strip())
    if trouve is None:
        return None
    heures, minutes, secondes, fraction = trouve.groups()
    # L'ASS compte en CENTIEMES de seconde, SubRip en millisecondes : completer
    # a droite plutot qu'a gauche, sinon « .50 » (une demi-seconde) devient
    # cinquante millisecondes.
    return f"{int(heures):02d}:{minutes}:{secondes},{fraction.ljust(3, '0')[:3]}"


def _texte_propre(ligne: str) -> str:
    """Retire le balisage d'affichage et rend les entites HTML.

    Les balises de couleur et de voix (« <v Roger> », « <c.yellow> ») sont du
    WebVTT ; les entites (« &amp;», « &#39; ») viennent des sous-titres extraits
    de pages web. Les laisser afficherait le balisage a l'ecran.
    """
    return html.unescape(_BALISES.sub("", ligne)).rstrip()


# --- Ecriture ---------------------------------------------------------------


def write_subtitle(
    video: Path,
    raw: bytes,
    *,
    language: str,
    forced: bool = False,
    hearing_impaired: bool = False,
    extension: str = ".srt",
    overwrite: bool = False,
) -> WriteResult:
    """Depose le sous-titre a cote de la video, converti, sans jamais surprendre.

    Un fichier deja present n'est pas ecrase : il a peut-etre ete cale a la main
    ou choisi expres, et le remplacer par une trouvaille automatique detruirait
    un travail invisible depuis ici. Le refus REMONTE — c'est ce qui le
    distingue d'un silence.
    """
    contenu, suffixe = to_subrip(raw, extension)
    cible = subtitle_path(
        video,
        language,
        forced=forced,
        hearing_impaired=hearing_impaired,
        extension=suffixe,
    )

    if cible.exists():
        if not overwrite:
            return WriteResult(
                Outcome.EXISTS,
                cible,
                f"« {cible.name} » existe déjà : rien n'a été écrit.",
            )
        logger.warning("remplacement du sous-titre existant %s", cible.name)

    try:
        _ecrire_atomiquement(cible, contenu)
    except OSError as exc:
        logger.warning("ecriture impossible de %s (%s)", cible.name, exc)
        return WriteResult(Outcome.FAILED, cible, f"Écriture impossible : {exc}")

    return WriteResult(Outcome.WRITTEN, cible, f"« {cible.name} » déposé.")


def _ecrire_atomiquement(cible: Path, contenu: bytes) -> None:
    """Ecrit ailleurs, force sur le disque, puis renomme.

    Le renommage est l'operation atomique : a aucun instant le nom definitif ne
    designe un fichier incomplet. Sans cela, une coupure pendant l'ecriture
    laisserait un « .srt » tronque — present pour le lecteur, present pour nous,
    donc jamais retelecharge, et coupant au milieu du film.

    Le ``fsync`` n'est pas du zele : le renommage ne garantit que la coherence
    des noms, pas celle des donnees. Sur un NAS coupe brutalement, on
    retrouverait un fichier au bon nom et au contenu vide — la meme panne, avec
    l'air d'un succes.

    Le temporaire est cree dans le MEME dossier, faute de quoi le renommage
    traverserait un systeme de fichiers et cesserait d'etre atomique. Son nom
    commence par un point : les fichiers caches sont ignores par le balayage des
    dossiers vides, un residu ne ferait donc pas passer un dossier pour occupe.
    """
    temporaire = cible.with_name(f".{cible.name}.{os.getpid()}.partiel")
    try:
        with temporaire.open("wb") as sortie:
            sortie.write(contenu)
            sortie.flush()
            os.fsync(sortie.fileno())
        os.replace(temporaire, cible)
    except OSError:
        temporaire.unlink(missing_ok=True)
        raise


# --- Orchestration ----------------------------------------------------------


def fingerprint(video: Path) -> str:
    """L'empreinte OpenSubtitles de la video, ou chaine vide.

    Enfin un appelant pour ``core/filehash``, ecrit et teste de longue date sans
    en avoir aucun. C'est le seul endroit de l'application ou l'on cesse de
    deviner : le nom, les tags et le fournisseur proposent, l'empreinte designe.
    """
    return _empreinte_du_fichier(video) or ""


async def fetch_missing(
    video: Path,
    wanted: Sequence[str],
    source: SubtitleSource,
    *,
    title: str = "",
    year: int | None = None,
    season: int | None = None,
    episode: int | None = None,
    probe: FileProbe | None = None,
    container_languages: Sequence[str] = (),
    overwrite: bool = False,
    audio_is_enough: bool = False,
) -> list[WriteResult]:
    """Complete les langues manquantes de cette video. Ne leve jamais.

    Une seule recherche pour toutes les langues : l'API les accepte ensemble, et
    interroger le service une fois par langue triplerait les appels pour le meme
    resultat.

    ``audio_is_enough`` : une piste audio dans la langue la compte comme
    presente (voir ``missing_languages``). Il faut une ``probe`` pour connaitre
    les pistes ; sans elle, le reglage ne peut rien retirer.

    L'empreinte est calculee dans un thread : elle lit le debut et la fin du
    fichier, et sur un NAS endormi cette lecture figerait la boucle
    d'evenements — donc l'application entiere — le temps que le disque reponde.
    """
    manquantes = missing_languages(
        video,
        wanted,
        probe=probe,
        container_languages=container_languages,
        audio_is_enough=audio_is_enough,
    )
    if not manquantes:
        return []

    offres = await source.find(
        moviehash=await asyncio.to_thread(fingerprint, video),
        title=title,
        year=year,
        season=season,
        episode=episode,
        languages=manquantes,
    )
    if not offres:
        return []

    resultats: list[WriteResult] = []
    for langue in manquantes:
        offre = _meilleure(offres, langue)
        if offre is None:
            continue
        contenu = await source.download(offre)
        if not contenu:
            continue
        resultats.append(
            write_subtitle(
                video,
                contenu,
                language=langue,
                forced=offre.forced,
                hearing_impaired=offre.hearing_impaired,
                extension=offre.extension,
                overwrite=overwrite,
            )
        )
    return resultats


def _meilleure(offres: Sequence[SubtitleOffer], langue: str) -> SubtitleOffer | None:
    """La premiere offre utilisable pour cette langue.

    Les offres arrivent deja classees par le fournisseur ; on ne refait pas son
    travail, on ecarte seulement les sous-titres FORCES tant qu'un sous-titre
    complet reste disponible. Un force depose comme piste principale donne un
    film ou trois repliques sur mille sont traduites, et le spectateur en conclut
    que le sous-titre est casse.
    """
    pour_la_langue = [o for o in offres if normalise_language(o.language) == langue]
    if not pour_la_langue:
        return None
    return next((o for o in pour_la_langue if not o.forced), pour_la_langue[0])
