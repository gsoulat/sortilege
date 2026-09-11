"""Ce qui doit rester vrai quand on recupere un sous-titre.

Les cinq points verifies ici correspondent aux cinq facons de rater cette
fonctionnalite en la faisant marcher :

- un fichier depose sous un nom que Jellyfin ne lit pas — il existe et n'existe
  pas, et personne ne le cherchera une seconde fois ;
- « hi » interprete comme « malentendants » alors qu'il designe le hindi ;
- des accents en losanges parce que l'encodage d'origine n'etait pas de l'UTF-8 ;
- un sous-titre cale a la main, ecrase en silence par une trouvaille
  automatique ;
- un « .srt » tronque par une coupure de courant, qui a l'air present.

S'y ajoute la regle du projet : aucun etat muet. Une cle absente doit se dire.

Aucun test ne touche le reseau — les reponses du service sont simulees. Ce n'est
pas seulement pour aller vite : un test qui dependrait du catalogue reel
echouerait un jour pour une raison etrangere au code.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import pytest

from sortilege.core import subtitles
from sortilege.core.subtitles import (
    Outcome,
    existing_languages,
    fetch_missing,
    missing_languages,
    normalise_language,
    read_tag,
    subtitle_path,
    to_subrip,
    write_subtitle,
)
from sortilege.providers.base import RateLimiter, forget_auth_errors
from sortilege.providers.opensubtitles import (
    API,
    OpenSubtitlesProvider,
    SubtitleCandidate,
)

CLE = "cle-opensubtitles-factice"

SRT = b"1\n00:00:01,000 --> 00:00:04,000\nBonjour\n"


@pytest.fixture(autouse=True)
def _oublie_les_refus() -> None:
    """Le registre des refus est global au processus : sans remise a zero, un
    401 simule ici contaminerait les tests suivants."""
    forget_auth_errors()


def video(tmp_path: Path, nom: str = "Film (2024).mkv") -> Path:
    chemin = tmp_path / nom
    chemin.write_bytes(b"\0" * 1024)
    return chemin


# --- Nommage attendu par Jellyfin -------------------------------------------


def test_le_nom_porte_la_langue(tmp_path: Path) -> None:
    film = video(tmp_path)
    assert subtitle_path(film, "fr").name == "Film (2024).fr.srt"


def test_le_nom_porte_les_modificateurs(tmp_path: Path) -> None:
    film = video(tmp_path)

    assert subtitle_path(film, "fr", forced=True).name == "Film (2024).fr.forced.srt"
    assert subtitle_path(film, "en", hearing_impaired=True).name == "Film (2024).en.sdh.srt"


def test_la_langue_est_normalisee_avant_d_etre_ecrite(tmp_path: Path) -> None:
    """Les preferences stockent « fr-FR » pour les metadonnees ; le nom d'un
    sous-titre, lui, ne prend que la langue."""
    film = video(tmp_path)

    assert subtitle_path(film, "fr-FR").name == "Film (2024).fr.srt"
    assert subtitle_path(film, "fre").name == "Film (2024).fr.srt"
    assert normalise_language("pob") == "pt-br"


def test_jamais_de_hi_pour_un_sous_titre_malentendants(tmp_path: Path) -> None:
    """Le piege documente par Jellyfin, pris a l'ecriture.

    « Film.hi.srt » designe du HINDI. Ecrire ce nom pour un sous-titre anglais
    pour malentendants ferait apparaitre une piste hindi dans le lecteur.
    """
    film = video(tmp_path)

    for langue in ("en", "fr", "de"):
        nom = subtitle_path(film, langue, hearing_impaired=True).name
        assert ".hi." not in nom, nom
        assert nom.endswith(f".{langue}.sdh.srt")


def test_le_hindi_reste_le_hindi(tmp_path: Path) -> None:
    """Et l'autre moitie du piege : la langue hindi doit pouvoir s'ecrire."""
    film = video(tmp_path)

    assert subtitle_path(film, "hi").name == "Film (2024).hi.srt"
    assert subtitle_path(film, "hin", hearing_impaired=True).name == "Film (2024).hi.sdh.srt"


def test_lecture_hi_seul_vaut_hindi(tmp_path: Path) -> None:
    tag = read_tag("Film (2024)", tmp_path / "Film (2024).hi.srt")

    assert tag is not None
    assert tag.language == "hi"
    assert tag.hearing_impaired is False


def test_lecture_hi_apres_une_langue_vaut_malentendants(tmp_path: Path) -> None:
    tag = read_tag("Film (2024)", tmp_path / "Film (2024).en.hi.srt")

    assert tag is not None
    assert tag.language == "en"
    assert tag.hearing_impaired is True


def test_lecture_des_forces(tmp_path: Path) -> None:
    tag = read_tag("Film (2024)", tmp_path / "Film (2024).fr.forced.srt")

    assert tag is not None
    assert (tag.language, tag.forced) == ("fr", True)


# --- Ce qui manque ----------------------------------------------------------


def test_un_sous_titre_present_n_est_pas_redemande(tmp_path: Path) -> None:
    film = video(tmp_path)
    (tmp_path / "Film (2024).fr.srt").write_bytes(SRT)

    assert existing_languages(film) == {"fr"}
    assert missing_languages(film, ["fr", "en"]) == ["en"]


def test_un_force_ne_compte_pas_comme_present(tmp_path: Path) -> None:
    """Il ne traduit que les repliques en langue etrangere : le compter
    priverait definitivement le spectateur du vrai sous-titre."""
    film = video(tmp_path)
    (tmp_path / "Film (2024).fr.forced.srt").write_bytes(SRT)

    assert missing_languages(film, ["fr"]) == ["fr"]


def test_les_langues_du_conteneur_comptent(tmp_path: Path) -> None:
    film = video(tmp_path)

    assert missing_languages(film, ["fr", "en"], container_languages=["fre"]) == ["en"]


def test_l_ordre_de_preference_est_conserve(tmp_path: Path) -> None:
    film = video(tmp_path)

    assert missing_languages(film, ["fr", "en", "fr-FR"]) == ["fr", "en"]


def test_la_sonde_est_lue_si_elle_sait(tmp_path: Path) -> None:
    """``FileProbe`` collecte les pistes de sous-titres integrees ; une sonde
    partielle qui les porte est lue de la meme facon."""
    film = video(tmp_path)

    class SondeFuture:
        audio_languages = ("eng",)
        subtitle_languages = ("fre",)

    assert missing_languages(film, ["fr", "en"], probe=SondeFuture()) == ["en"]  # type: ignore[arg-type]


# --- Conversion en SubRip UTF-8 ---------------------------------------------


def test_un_srt_windows_devient_de_l_utf8() -> None:
    """Le cas le plus frequent, et le plus visible : sans conversion, chaque
    accent s'affiche en losange."""
    brut = "1\n00:00:01,000 --> 00:00:02,000\nVoilà l'été\n".encode("cp1252")

    contenu, extension = to_subrip(brut, ".srt")

    assert extension == ".srt"
    assert "Voilà l'été" in contenu.decode("utf-8")


def test_un_srt_utf16_devient_de_l_utf8() -> None:
    brut = "1\n00:00:01,000 --> 00:00:02,000\nÉté\n".encode("utf-16")

    contenu, _ = to_subrip(brut, ".srt")

    assert contenu.decode("utf-8").strip().endswith("Été")


def test_un_vtt_devient_du_subrip() -> None:
    vtt = (
        b"WEBVTT\n\nNOTE une remarque\n\n"
        b"intro\n01:23.000 --> 01:25.500 align:start position:10%\n"
        b"<v Roger>Bonjour &amp; bonsoir\n"
    )

    contenu, extension = to_subrip(vtt, ".vtt")
    texte = contenu.decode("utf-8")

    assert extension == ".srt"
    # Heures completees, virgule decimale, reglages de placement retires.
    assert "00:01:23,000 --> 00:01:25,500" in texte
    assert "align:start" not in texte
    assert texte.startswith("1\n")
    assert "Bonjour & bonsoir" in texte
    assert "<v Roger>" not in texte


def test_un_ass_devient_du_subrip() -> None:
    ass = (
        b"[Events]\n"
        b"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        b"Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,{\\pos(1,2)}Salut, toi\\Net moi\n"
    )

    contenu, extension = to_subrip(ass, ".ass")
    texte = contenu.decode("utf-8")

    assert extension == ".srt"
    # Centiemes vers millisecondes : « .50 » est une demi-seconde.
    assert "00:00:01,500 --> 00:00:03,000" in texte
    assert "{\\pos(1,2)}" not in texte
    # La virgule du texte n'est pas un separateur de colonne.
    assert "Salut, toi\net moi" in texte


def test_un_format_exotique_est_conserve_intact() -> None:
    """Mieux vaut un sous-titre qu'on ne sait pas convertir qu'un sous-titre
    corrompu par une conversion approximative."""
    vobsub = b"\x00\x01binaire\xff"

    contenu, extension = to_subrip(vobsub, ".sub")

    assert (contenu, extension) == (vobsub, ".sub")


def test_un_texte_illisible_ne_devient_pas_un_srt_vide() -> None:
    contenu, extension = to_subrip(b"rien qui ressemble a du webvtt", ".vtt")

    assert extension == ".vtt"
    assert contenu == b"rien qui ressemble a du webvtt"


# --- Ecriture ---------------------------------------------------------------


def test_ecriture_a_cote_de_la_video(tmp_path: Path) -> None:
    film = video(tmp_path)

    resultat = write_subtitle(film, SRT, language="fr")

    assert resultat.outcome is Outcome.WRITTEN
    assert resultat.path == tmp_path / "Film (2024).fr.srt"
    assert resultat.path.read_bytes() == SRT


def test_un_sous_titre_existant_n_est_jamais_ecrase(tmp_path: Path) -> None:
    film = video(tmp_path)
    existant = tmp_path / "Film (2024).fr.srt"
    existant.write_text("cale a la main", encoding="utf-8")

    resultat = write_subtitle(film, SRT, language="fr")

    assert resultat.outcome is Outcome.EXISTS
    assert existant.read_text(encoding="utf-8") == "cale a la main"


def test_le_refus_d_ecraser_se_dit(tmp_path: Path) -> None:
    """Un refus silencieux serait indiscernable d'une reussite."""
    film = video(tmp_path)
    (tmp_path / "Film (2024).fr.srt").write_text("deja la", encoding="utf-8")

    resultat = write_subtitle(film, SRT, language="fr")

    assert "Film (2024).fr.srt" in resultat.message
    assert resultat.ok is False


def test_l_ecrasement_demande_est_annonce(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    film = video(tmp_path)
    cible = tmp_path / "Film (2024).fr.srt"
    cible.write_text("ancien", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        resultat = write_subtitle(film, SRT, language="fr", overwrite=True)

    assert resultat.outcome is Outcome.WRITTEN
    assert cible.read_bytes() == SRT
    assert "Film (2024).fr.srt" in caplog.text


def test_une_ecriture_interrompue_ne_laisse_rien(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le seul scenario ou un sous-titre serait pire qu'absent.

    Un « .srt » tronque se presente comme present : il ne sera plus jamais
    redemande, et il coupera au milieu du film. On verifie donc qu'un echec ne
    publie RIEN sous le nom definitif, et ne laisse pas de residu.
    """
    film = video(tmp_path)

    def coupure(*_args, **_kwargs):
        raise OSError("coupure de courant")

    monkeypatch.setattr(subtitles.os, "replace", coupure)
    resultat = write_subtitle(film, SRT, language="fr")

    assert resultat.outcome is Outcome.FAILED
    assert not (tmp_path / "Film (2024).fr.srt").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["Film (2024).mkv"]


def test_le_nom_definitif_n_apparait_qu_une_fois_complet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """L'ecriture passe par un temporaire : au moment ou le nom final apparait,
    le contenu est deja entier."""
    film = video(tmp_path)
    vu: list[bytes | None] = []
    remplacement = subtitles.os.replace

    def espion(source, destination):
        cible = Path(destination)
        vu.append(cible.read_bytes() if cible.exists() else None)
        # Le temporaire porte deja tout le contenu avant le renommage.
        assert Path(source).read_bytes() == SRT
        return remplacement(source, destination)

    monkeypatch.setattr(subtitles.os, "replace", espion)
    write_subtitle(film, SRT, language="fr")

    assert vu == [None], "le nom definitif existait avant que le contenu soit complet"


# --- Le fournisseur ---------------------------------------------------------


class Service:
    """Le service simule : repond ce qu'on lui dit, et retient chaque requete."""

    def __init__(self, reponses: list[httpx.Response]) -> None:
        self.reponses = reponses
        self.requetes: list[httpx.Request] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requetes.append(request)
        if not self.reponses:
            return httpx.Response(200, json={"data": []})
        return self.reponses.pop(0)


def sous_titre(**attributs) -> dict:
    base = {
        "language": "fr",
        "download_count": 10,
        "moviehash_match": True,
        "files": [{"file_id": 4242, "file_name": "Film.srt"}],
    }
    base.update(attributs)
    return {"attributes": base}


def fournisseur(service: Service, cle: str = CLE) -> OpenSubtitlesProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(service.handle))
    provider = OpenSubtitlesProvider(cle, client=client)
    # Ni attente entre deux requetes, ni attente apres un 429 : le test verifie
    # la politique de reprise, pas la patience de l'horloge.
    provider._limiter = RateLimiter(per_second=0)
    provider.retry_wait_cap = 0.0
    return provider


async def test_sans_cle_le_module_le_dit(caplog: pytest.LogCaptureFixture) -> None:
    """Aucun etat muet : une cle absente degrade exactement comme une
    bibliotheque deja complete, et rien ne les distinguerait."""
    service = Service([])
    provider = fournisseur(service, cle="")

    with caplog.at_level(logging.WARNING):
        resultats = await provider.find(moviehash="a" * 16, title="Dune", languages=["fr"])
    await provider.aclose()

    assert resultats == []
    assert service.requetes == [], "une requete est partie sans cle"
    assert provider.available is False
    assert "clé" in provider.unavailable_reason.lower()
    assert "opensubtitles.com" in caplog.text


async def test_une_cle_refusee_se_dit_aussi() -> None:
    service = Service([httpx.Response(401, json={"message": "invalid api key"})])
    provider = fournisseur(service)

    await provider.find(moviehash="a" * 16, languages=["fr"])
    raison = provider.unavailable_reason
    await provider.aclose()

    assert "401" in raison
    assert "Consumers" in raison


async def test_l_empreinte_est_essayee_en_premier() -> None:
    """C'est ce qui separe un sous-titre synchronise d'un sous-titre
    approximatif : l'empreinte designe le fichier, pas l'oeuvre."""
    service = Service([httpx.Response(200, json={"data": [sous_titre()]})])
    provider = fournisseur(service)

    resultats = await provider.find(moviehash="0123456789abcdef", title="Dune", languages=["fr"])
    await provider.aclose()

    assert len(service.requetes) == 1, "le titre a ete interroge alors que l'empreinte suffisait"
    assert "moviehash=0123456789abcdef" in str(service.requetes[0].url)
    assert resultats[0].from_hash is True


async def test_le_titre_ne_sert_que_de_repli() -> None:
    service = Service(
        [
            httpx.Response(200, json={"data": []}),
            httpx.Response(200, json={"data": [sous_titre(moviehash_match=False)]}),
        ]
    )
    provider = fournisseur(service)

    resultats = await provider.find(
        moviehash="0123456789abcdef",
        title="Dune",
        year=2021,
        season=1,
        episode=2,
        languages=["fr"],
    )
    await provider.aclose()

    assert len(service.requetes) == 2
    repli = str(service.requetes[1].url)
    assert "query=Dune" in repli
    assert "season_number=1" in repli and "episode_number=2" in repli
    assert resultats[0].from_hash is False, "un repli ne doit pas se presenter comme certain"


async def test_les_langues_sont_filtrees() -> None:
    """Deux fois : dans la requete, et sur la reponse — un sous-titre espagnol
    depose en « .fr.srt » serait pire qu'un sous-titre absent."""
    service = Service(
        [httpx.Response(200, json={"data": [sous_titre(language="es"), sous_titre()]})]
    )
    provider = fournisseur(service)

    resultats = await provider.find(moviehash="a" * 16, languages=["fr", "en"])
    await provider.aclose()

    # Codes en minuscules et TRIES : l'API l'exige.
    assert "languages=en%2Cfr" in str(service.requetes[0].url)
    assert [c.language for c in resultats] == ["fr"]


async def test_l_agent_utilisateur_nomme_l_application() -> None:
    """Un client HTTP generique recoit un 403 sans explication."""
    service = Service([httpx.Response(200, json={"data": []})])
    provider = fournisseur(service)

    await provider.find(moviehash="a" * 16, languages=["fr"])
    await provider.aclose()

    assert service.requetes[0].headers["user-agent"].startswith("Sortilege v")
    assert service.requetes[0].headers["api-key"] == CLE


async def test_une_seule_reprise_apres_un_429() -> None:
    """Un 429 dit « trop vite » ; une boucle de reprises dit « encore plus
    vite ». On retente une fois, puis ce fichier attendra le prochain scan."""
    service = Service(
        [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(429, headers={"Retry-After": "1"}),
        ]
    )
    provider = fournisseur(service)

    resultats = await provider.find(moviehash="a" * 16, languages=["fr"])
    await provider.aclose()

    assert resultats == []
    assert len(service.requetes) == 2


async def test_un_429_passager_est_rattrape() -> None:
    service = Service(
        [
            httpx.Response(429, headers={"Retry-After": "1"}),
            httpx.Response(200, json={"data": [sous_titre()]}),
        ]
    )
    provider = fournisseur(service)

    resultats = await provider.find(moviehash="a" * 16, languages=["fr"])
    await provider.aclose()

    assert len(resultats) == 1


async def test_la_cle_ne_part_pas_vers_l_hote_du_lien() -> None:
    """Le lien de telechargement pointe vers un autre hote : une cle envoyee a
    un hote qu'on n'a pas choisi est une cle divulguee."""
    service = Service(
        [
            httpx.Response(200, json={"link": "https://cdn.example.org/f.srt", "remaining": 20}),
            httpx.Response(200, content=SRT),
        ]
    )
    provider = fournisseur(service)

    contenu = await provider.download(SubtitleCandidate(file_id=4242, language="fr"))
    await provider.aclose()

    assert contenu == SRT
    retrait = service.requetes[1]
    assert str(retrait.url).startswith("https://cdn.example.org")
    assert "api-key" not in retrait.headers
    assert service.requetes[0].url == httpx.URL(f"{API}/download")


async def test_le_quota_epuise_arrete_les_tentatives() -> None:
    """Insister sur un quota epuise, c'est depenser des requetes pour se faire
    limiter."""
    service = Service(
        [
            httpx.Response(200, json={"link": "https://cdn.example.org/f.srt", "remaining": 0}),
            httpx.Response(200, content=SRT),
        ]
    )
    provider = fournisseur(service)
    candidat = SubtitleCandidate(file_id=4242, language="fr")

    premier = await provider.download(candidat)
    second = await provider.download(candidat)
    raison = provider.unavailable_reason
    await provider.aclose()

    assert premier == SRT, "le lien deja paye devait etre suivi"
    assert second is None
    assert len(service.requetes) == 2, "une requete est partie malgre le quota epuise"
    assert "quota" in raison.lower()


def test_le_classement_place_l_empreinte_en_tete() -> None:
    provider = OpenSubtitlesProvider(CLE)
    donnees = {
        "data": [
            sous_titre(download_count=9000, machine_translated=True),
            sous_titre(download_count=12),
        ]
    }

    candidats = provider._vers_candidats(donnees, languages=["fr"], par_empreinte=True)

    assert candidats[0].downloads == 12, "une traduction automatique populaire est passee devant"


# --- Le tout, bout a bout ---------------------------------------------------


class SourceSimulee:
    """Source conforme au protocole, sans la moindre couche HTTP."""

    def __init__(self, offres: list[SubtitleCandidate], contenu: bytes = SRT) -> None:
        self.offres = offres
        self.contenu = contenu
        self.recherches: list[dict] = []
        self.telechargements: list[int] = []

    async def find(self, **kwargs):
        self.recherches.append(kwargs)
        return self.offres

    async def download(self, candidate):
        self.telechargements.append(candidate.file_id)
        return self.contenu


async def test_fetch_missing_depose_la_langue_manquante(tmp_path: Path) -> None:
    film = video(tmp_path)
    source = SourceSimulee([SubtitleCandidate(file_id=1, language="fr")])

    resultats = await fetch_missing(film, ["fr"], source, title="Film")

    assert [r.outcome for r in resultats] == [Outcome.WRITTEN]
    assert (tmp_path / "Film (2024).fr.srt").read_bytes() == SRT


async def test_fetch_missing_n_interroge_rien_si_tout_est_la(tmp_path: Path) -> None:
    film = video(tmp_path)
    (tmp_path / "Film (2024).fr.srt").write_bytes(SRT)
    source = SourceSimulee([SubtitleCandidate(file_id=1, language="fr")])

    resultats = await fetch_missing(film, ["fr"], source, title="Film")

    assert resultats == []
    assert source.recherches == [], "une requete est partie pour une langue deja presente"


async def test_fetch_missing_prefere_un_sous_titre_complet(tmp_path: Path) -> None:
    """Un force depose comme piste principale donne un film ou trois repliques
    sur mille sont traduites."""
    film = video(tmp_path)
    source = SourceSimulee(
        [
            SubtitleCandidate(file_id=1, language="fr", forced=True),
            SubtitleCandidate(file_id=2, language="fr"),
        ]
    )

    await fetch_missing(film, ["fr"], source)

    assert source.telechargements == [2]
    assert (tmp_path / "Film (2024).fr.srt").exists()


async def test_fetch_missing_transmet_l_empreinte(tmp_path: Path) -> None:
    """Le consommateur qui manquait a ``core/filehash``."""
    film = tmp_path / "Film (2024).mkv"
    film.write_bytes(b"\x01" * (256 * 1024))
    source = SourceSimulee([SubtitleCandidate(file_id=1, language="fr")])

    await fetch_missing(film, ["fr"], source, title="Film")

    empreinte = source.recherches[0]["moviehash"]
    assert len(empreinte) == 16
    assert int(empreinte, 16)
