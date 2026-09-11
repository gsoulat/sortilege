"""OpenSubtitles — chercher un sous-titre par l'EMPREINTE du fichier.

Ce fournisseur est le premier de Sortilege a interroger une base avec autre
chose qu'un titre. La difference n'est pas de degre, elle est de nature.

Une recherche par titre demande « quel sous-titre existe pour ce film ? » et
recoit des dizaines de reponses valables pour l'oeuvre et fausses pour le
fichier : la version cinema et la version longue ne durent pas pareil, une
release WEB et un remux Blu-ray n'ont pas le meme generique, et un sous-titre
decale de huit secondes est un sous-titre inutilisable. L'utilisateur ne le
decouvre qu'a la lecture, et il ne saura pas que c'est reparable.

Une recherche par empreinte demande « quel sous-titre a ete synchronise sur CE
fichier ? ». Quand elle repond, la synchronisation est acquise : l'empreinte
designe un encodage precis, et celui qui a televerse le sous-titre l'a cale sur
ce meme encodage. C'est pourquoi ``core/filehash`` — ecrit, teste, et jusqu'ici
sans aucun appelant — trouve enfin son emploi ici.

D'ou l'ordre, qui n'est pas une preference mais une hierarchie de certitude :
empreinte d'abord, titre en repli, et le repli est signale comme tel jusqu'au
choix final (``from_hash``).

Deux dettes de l'API v1 valent d'etre connues :

- La cle d'API n'est pas le mot de passe du compte. Elle se cree dans l'espace
  « Consumers » du site, et une installation qui n'en a pas ne doit pas echouer
  en silence : elle doit le DIRE (voir ``unavailable_reason``).
- L'en-tete ``User-Agent`` doit nommer l'application et sa version. Un client
  HTTP generique recoit un 403 sans explication, et le service se reserve le
  droit de bloquer une version precise qui se comporte mal — ce qu'il ne peut
  faire que si elle se nomme.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from .. import __version__
from .base import AUTH_STATUSES, BaseHTTPProvider

logger = logging.getLogger(__name__)

API = "https://api.opensubtitles.com/api/v1"

USER_AGENT = f"Sortilege v{__version__}"
"""Nom et version, comme l'exige le service.

La version bouge avec l'application, volontairement : c'est le seul moyen pour
OpenSubtitles de bloquer une livraison qui abuse de son quota sans bloquer tous
les utilisateurs de Sortilege."""

TAILLE_MAX = 5 * 1024 * 1024
"""Plafond du corps telecharge.

Un sous-titre de trois heures pese quelques dizaines de kilo-octets. Ce plafond
ne protege donc pas d'un gros sous-titre : il protege du cas ou le lien servi
ne pointe pas vers ce qu'on croit."""

AUCUNE_CLE = (
    "Aucune clé OpenSubtitles n'est configurée : les sous-titres manquants ne seront "
    "pas récupérés. La clé se crée sur opensubtitles.com, section « Consumers » — ce "
    "n'est pas le mot de passe du compte."
)

QUOTA_EPUISE = (
    "Quota de téléchargement OpenSubtitles épuisé pour aujourd'hui : les sous-titres "
    "restants seront récupérés au prochain passage. Un compte VIP relève ce plafond."
)


@dataclass(frozen=True, slots=True)
class SubtitleCandidate:
    """Un sous-titre propose, avec de quoi le departager ET le nommer.

    ``forced`` et ``hearing_impaired`` ne servent pas au choix : ils voyagent
    jusqu'a l'ecriture parce que Jellyfin les lit dans le NOM du fichier. Les
    perdre ici produirait un sous-titre force depose comme piste principale,
    c'est-a-dire trois lignes de dialogue la ou le lecteur en annonce deux
    mille.
    """

    file_id: int
    language: str
    release: str = ""
    extension: str = ".srt"
    downloads: int = 0
    from_hash: bool = False
    """Trouve par l'empreinte : la synchronisation est acquise, pas esperee."""

    from_trusted: bool = False
    hearing_impaired: bool = False
    forced: bool = False
    machine_translated: bool = False
    ai_translated: bool = False


class OpenSubtitlesProvider(BaseHTTPProvider):
    """Client REST v1. Ne leve jamais : une panne coute un sous-titre, pas un rangement."""

    name = "opensubtitles"

    # Le service annonce un debit de l'ordre de cinq requetes par seconde par
    # adresse, et repond 429 au-dela. On s'y tient plutot que de decouvrir la
    # limite en la franchissant : un blocage se compte en heures, une seconde
    # d'attente en millisecondes.
    requests_per_second = 5.0

    retry_wait_cap = 8.0
    """Plafond de l'attente consentie apres un 429.

    Le service peut demander de patienter longtemps ; un scan ne peut pas se
    figer pour autant. Au-dela, on abandonne CE fichier — il repassera au
    prochain scan, ou le quota sera revenu."""

    def __init__(self, api_key: str, *, user_agent: str = "", token: str = "", **kwargs) -> None:
        super().__init__(**kwargs)
        self._api_key = (api_key or "").strip()
        self._user_agent = (user_agent or "").strip() or USER_AGENT
        # Jeton de session facultatif : il ne sert qu'a relever le quota d'un
        # compte VIP. Sans lui le fournisseur fonctionne, plus modestement.
        self._token = (token or "").strip()
        self._quota_epuise = False
        self._absence_signalee = False

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def unavailable_reason(self) -> str:
        """Pourquoi ce fournisseur ne rendra rien, ou chaine vide.

        La regle du projet est qu'aucun etat ne reste muet. Une cle absente et
        une cle refusee degradent de la meme facon — « aucun sous-titre » — et
        rien ne les distinguerait d'une bibliotheque deja complete. On les
        nomme donc, et la difference compte : l'une se corrige en collant une
        cle, l'autre en la regenerant.
        """
        if not self.available:
            return AUCUNE_CLE
        if self._quota_epuise:
            return QUOTA_EPUISE
        return self.last_auth_error

    def _auth_message(self, status_code: int) -> str:
        """Nomme les deux confusions qui expliquent presque tous les refus ici."""
        return (
            f"Clé OpenSubtitles refusée ({status_code}) : vérifie qu'il s'agit bien d'une "
            "clé d'API créée dans « Consumers » sur opensubtitles.com, et non du mot de "
            "passe du compte. Un refus persistant peut aussi venir d'un User-Agent "
            "rejeté par le service."
        )

    def _signaler_l_absence(self) -> None:
        """Dit une fois, pas a chaque fichier.

        Une bibliotheque de mille episodes ecrirait mille fois la meme ligne et
        noierait le journal — la ou l'information, elle, est unique.
        """
        if not self._absence_signalee:
            self._absence_signalee = True
            logger.warning(AUCUNE_CLE)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Api-Key": self._api_key,
            "User-Agent": self._user_agent,
            "Accept": "application/json",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    # --- Recherche ----------------------------------------------------------

    async def find(
        self,
        *,
        moviehash: str = "",
        title: str = "",
        year: int | None = None,
        season: int | None = None,
        episode: int | None = None,
        languages: tuple[str, ...] | list[str] = (),
    ) -> list[SubtitleCandidate]:
        """Empreinte d'abord, titre en repli. Jamais l'inverse.

        Le repli n'est tente que si l'empreinte n'a RIEN donne : melanger les
        deux jeux de resultats ferait remonter, pour un fichier reconnu, des
        sous-titres cales sur une autre version — exactement ce que l'empreinte
        servait a eviter.
        """
        if not self.available:
            self._signaler_l_absence()
            return []

        if moviehash:
            if resultats := await self.search_by_hash(moviehash, languages=languages):
                return resultats

        if title:
            return await self.search_by_title(
                title, year=year, season=season, episode=episode, languages=languages
            )
        return []

    async def search_by_hash(
        self, moviehash: str, *, languages: tuple[str, ...] | list[str] = ()
    ) -> list[SubtitleCandidate]:
        """Sous-titres cales sur CE fichier.

        Une seule chose voyage : l'empreinte. L'ancien XML-RPC exigeait aussi la
        taille du fichier ; la v1 ne la demande plus, et ce n'est pas un oubli —
        la taille est deja l'un des trois termes du calcul de l'empreinte.
        """
        params = self._params(moviehash=moviehash, languages=languages)
        donnees = await self._appel("GET", f"{API}/subtitles", params=params)
        return self._vers_candidats(donnees, languages=languages, par_empreinte=True)

    async def search_by_title(
        self,
        title: str,
        *,
        year: int | None = None,
        season: int | None = None,
        episode: int | None = None,
        languages: tuple[str, ...] | list[str] = (),
    ) -> list[SubtitleCandidate]:
        """Repli : le titre, et pour une serie la saison et l'episode.

        Sans saison ni episode, une recherche de serie renvoie les sous-titres
        de toute la serie, et le premier resultat serait un episode au hasard.
        """
        params = self._params(query=title, languages=languages)
        if year:
            params["year"] = str(year)
        if season is not None and episode is not None:
            params["season_number"] = str(season)
            params["episode_number"] = str(episode)
            params["type"] = "episode"
        else:
            params["type"] = "movie"

        donnees = await self._appel("GET", f"{API}/subtitles", params=params)
        return self._vers_candidats(donnees, languages=languages, par_empreinte=False)

    def _params(self, *, languages: tuple[str, ...] | list[str], **extra: str) -> dict[str, str]:
        """Parametres communs, langues comprises.

        Les codes sont mis en minuscules et TRIES : l'API documente cette
        contrainte et repond une erreur peu bavarde quand on l'ignore. Ce genre
        de detail se paye une fois, en production, un dimanche.
        """
        params = {cle: valeur for cle, valeur in extra.items() if valeur}
        if codes := sorted({code.strip().lower() for code in languages if code.strip()}):
            params["languages"] = ",".join(codes)
        return params

    # --- Telechargement -----------------------------------------------------

    async def download(self, candidate: SubtitleCandidate) -> bytes | None:
        """Contenu brut du sous-titre, ou None.

        Deux temps imposes par l'API : un POST qui decompte un jeton du quota et
        rend un lien ephemere, puis le retrait du fichier sur ce lien. Le second
        appel part SANS la cle : le lien pointe vers un autre hote, et une cle
        d'API envoyee a un hote qu'on n'a pas choisi est une cle divulguee.
        """
        if not self.available:
            self._signaler_l_absence()
            return None
        if self._quota_epuise:
            # On n'insiste pas : chaque tentative supplementaire est un appel
            # qui sera refuse, et une chance de plus de se faire limiter.
            logger.info("%s : quota epuise, telechargement non tente", self.name)
            return None

        donnees = await self._appel("POST", f"{API}/download", json={"file_id": candidate.file_id})
        if not donnees:
            return None

        restant = donnees.get("remaining")
        if isinstance(restant, int) and restant <= 0:
            # Le lien de CETTE reponse reste valable : le jeton est deja
            # decompte. On le suit, et on ferme la porte pour les suivants.
            self._quota_epuise = True
            logger.warning(QUOTA_EPUISE)

        lien = str(donnees.get("link") or "")
        if not lien.startswith("https://"):
            logger.warning("%s : lien de telechargement inattendu, ignore", self.name)
            return None

        return await self._retirer(lien)

    async def _retirer(self, lien: str) -> bytes | None:
        """Le fichier au bout du lien, lu EN FLUX et jamais au-dela de ``TAILLE_MAX``.

        Lire la reponse entiere avant de la mesurer laissait l'hote distant —
        un hote que l'API designe, pas nous — decider de la memoire consommee :
        le plafond ne protegeait que ce qui venait apres. Une taille annoncee
        trop grande est refusee sans rien lire ; une taille non annoncee, ou
        mensongere, interrompt la lecture des le plafond franchi.
        """
        await self._limiter.wait()
        contenu = bytearray()
        try:
            async with self.client.stream("GET", lien, follow_redirects=True) as reponse:
                reponse.raise_for_status()
                annonce = reponse.headers.get("content-length", "").strip()
                if annonce.isdigit() and int(annonce) > TAILLE_MAX:
                    logger.warning(
                        "%s : contenu annonce a %s octets refuse sans etre lu, "
                        "ce n'est pas un sous-titre",
                        self.name,
                        annonce,
                    )
                    return None
                async for bloc in reponse.aiter_bytes():
                    contenu += bloc
                    if len(contenu) > TAILLE_MAX:
                        logger.warning(
                            "%s : contenu au-dela de %d octets, lecture interrompue",
                            self.name,
                            TAILLE_MAX,
                        )
                        return None
        except httpx.HTTPError as exc:
            logger.warning(
                "%s : retrait du sous-titre impossible (%s)", self.name, type(exc).__name__
            )
            return None
        return bytes(contenu) or None

    # --- Transport ----------------------------------------------------------

    async def _appel(self, methode: str, url: str, **kwargs: Any) -> dict[str, Any] | None:
        """Un appel, UNE reprise au plus, et jamais d'exception qui sorte d'ici.

        La reprise unique est le coeur de la politesse envers ce service : un
        429 dit « trop vite », et une boucle de reprises dit « encore plus
        vite ». On patiente le temps demande, on retente une fois, puis on
        abandonne ce fichier — il repassera au prochain scan.
        """
        reponse = await self._envoyer(methode, url, **kwargs)

        if reponse is not None and reponse.status_code == 429:
            attente = self._attente_demandee(reponse)
            logger.info("%s limite le debit : une seule reprise dans %.1f s", self.name, attente)
            await asyncio.sleep(attente)
            reponse = await self._envoyer(methode, url, **kwargs)

        if reponse is None:
            return None

        if reponse.status_code in AUTH_STATUSES:
            self._note_auth(reponse.status_code)
            logger.warning("%s : identifiant refuse (%s)", self.name, reponse.status_code)
            return None

        if reponse.status_code == 429:
            logger.warning("%s : debit toujours limite, ce fichier attendra", self.name)
            return None

        if reponse.status_code >= 400:
            logger.warning("%s : reponse %s pour %s", self.name, reponse.status_code, url)
            return None

        self._note_auth(None)
        try:
            charge = reponse.json()
        except ValueError:
            logger.warning("%s : reponse illisible (JSON invalide)", self.name)
            return None
        return charge if isinstance(charge, dict) else None

    async def _envoyer(self, methode: str, url: str, **kwargs: Any) -> httpx.Response | None:
        await self._limiter.wait()
        try:
            return await self.client.request(methode, url, headers=self._headers(), **kwargs)
        except httpx.HTTPError as exc:
            logger.warning("%s injoignable (%s)", self.name, type(exc).__name__)
            return None

    def _attente_demandee(self, reponse: httpx.Response) -> float:
        """Le temps annonce par le service, borne par le notre.

        On respecte ce qu'il demande — c'est lui qui sait — mais un scan ne peut
        pas se suspendre pendant plusieurs minutes sur un seul fichier.
        """
        brut = reponse.headers.get("retry-after", "")
        try:
            demande = float(brut)
        except ValueError:
            demande = 1.0
        return max(0.0, min(demande, self.retry_wait_cap))

    # --- Lecture de la reponse ---------------------------------------------

    def _vers_candidats(
        self,
        donnees: dict[str, Any] | None,
        *,
        languages: tuple[str, ...] | list[str],
        par_empreinte: bool,
    ) -> list[SubtitleCandidate]:
        if not donnees or not isinstance(donnees.get("data"), list):
            return []

        voulues = {code.strip().lower() for code in languages if code.strip()}
        candidats: list[SubtitleCandidate] = []

        for entree in donnees["data"]:
            attributs = (entree or {}).get("attributes") or {}
            fichier = self._premier_fichier(attributs)
            if fichier is None:
                continue

            langue = str(attributs.get("language") or "").strip().lower()
            # Second filtrage, cote client : le parametre de langue est parfois
            # ignore pour les entrees mal etiquetees, et un sous-titre espagnol
            # depose en « .fr.srt » serait pire qu'un sous-titre absent.
            if voulues and langue not in voulues:
                continue

            identifiant = _entier(fichier.get("file_id"))
            if identifiant is None:
                continue

            candidats.append(
                SubtitleCandidate(
                    file_id=identifiant,
                    language=langue,
                    release=str(attributs.get("release") or ""),
                    extension=_extension(fichier.get("file_name"), attributs.get("format")),
                    downloads=_entier(attributs.get("download_count")) or 0,
                    # Le service ne marque « moviehash_match » que sur une
                    # recherche par empreinte ; hors de ce cas la certitude ne
                    # doit pas etre inventee.
                    from_hash=par_empreinte and bool(attributs.get("moviehash_match", True)),
                    from_trusted=bool(attributs.get("from_trusted")),
                    hearing_impaired=bool(attributs.get("hearing_impaired")),
                    # « Parties etrangeres seulement » : c'est le sous-titre
                    # force, sous son nom d'origine.
                    forced=bool(attributs.get("foreign_parts_only")),
                    machine_translated=bool(attributs.get("machine_translated")),
                    ai_translated=bool(attributs.get("ai_translated")),
                )
            )

        return sorted(candidats, key=_rang)

    @staticmethod
    def _premier_fichier(attributs: dict[str, Any]) -> dict[str, Any] | None:
        """Le premier CD d'une entree multi-fichiers.

        Un sous-titre decoupe en deux pour un film en deux disques n'a plus de
        sens sur un fichier unique : en prendre le premier morceau donnerait un
        sous-titre qui s'arrete au milieu. On preferera de toute facon une
        entree a fichier unique, mieux classee plus bas.
        """
        fichiers = attributs.get("files")
        if not isinstance(fichiers, list) or not fichiers:
            return None
        premier = fichiers[0]
        return premier if isinstance(premier, dict) else None


def _rang(candidat: SubtitleCandidate) -> tuple:
    """Ordre de preference, du plus sur au plus douteux.

    L'empreinte prime sur tout le reste : un sous-titre moins telecharge mais
    synchronise vaut mieux qu'un succes populaire cale sur une autre version.
    La traduction automatique passe en dernier — elle est lisible, rarement
    juste, et l'utilisateur qui la decouvre croit que l'outil s'est trompe de
    fichier.
    """
    return (
        not candidat.from_hash,
        candidat.machine_translated or candidat.ai_translated,
        not candidat.from_trusted,
        -candidat.downloads,
    )


def _entier(valeur: object) -> int | None:
    try:
        return int(str(valeur).strip())
    except (TypeError, ValueError):
        return None


def _extension(nom_de_fichier: object, format_annonce: object) -> str:
    """Extension reelle du sous-titre, « .srt » a defaut.

    Elle decide de la conversion en aval : convertir un ASS comme un SRT
    produirait un fichier de balises que le lecteur afficherait telles quelles.
    """
    nom = str(nom_de_fichier or "")
    if "." in nom:
        extension = nom[nom.rfind(".") :].lower()
        if 2 <= len(extension) <= 5:
            return extension
    if format_annonce:
        return f".{str(format_annonce).strip().lower().lstrip('.')}"
    return ".srt"
