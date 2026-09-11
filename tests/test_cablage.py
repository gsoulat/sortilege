"""Le cablage des modules ecrits a part : sous-titres, sortie reseau, livres.

Un module peut etre juste, teste, et n'etre appele par personne. C'est le
defaut que ce projet a deja rencontre deux fois — une echelle typographique
definie et jamais appliquee, un composant de confirmation que personne
n'importait — et ces tests-la ne verifient donc PAS ce que les modules font,
leurs propres suites s'en chargent. Ils verifient qu'ils sont branches :

- que le reglage existe, se relit, et refuse ce qui n'a pas de sens ;
- que l'API le rend et l'accepte, sans jamais laisser sortir une cle ;
- que le garde-fou de sortie reseau REFUSE vraiment, au lieu d'orner un ecran.

Aucun de ces tests ne touche le reseau : la politique « libre » ne mesure rien
par construction, et les deux autres sont examinees avec une mesure fabriquee.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api.deps import get_store
from sortilege.core import vpn
from sortilege.core.preferences import PreferenceError, SubtitleSettings, VpnSettings
from sortilege.main import app


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


@pytest.fixture(autouse=True)
def _preferences_neuves():
    """Chaque test repart de reglages vierges.

    Le magasin est un singleton mis en cache : sans cela, un test qui active
    les sous-titres laisserait la recherche active pour le suivant, qui
    passerait ou echouerait selon l'ordre d'execution.
    """
    store = get_store()
    # Copie PROFONDE : ``load`` rend l'objet mis en cache, pas une copie. Sans
    # cela, un test qui pose une valeur invalide la poserait aussi dans ce
    # qu'on croit etre l'etat d'avant, et la remise en etat echouerait.
    avant = copy.deepcopy(store.load())
    yield
    store._cache = None
    store.save(avant)
    vpn.reset_cache()


# --- Les reglages existent et se relisent -----------------------------------


def test_les_deux_blocs_ont_des_defauts_inoffensifs():
    """Rien ne s'active tout seul : les deux ajoutent du trafic sortant."""
    assert SubtitleSettings().enabled is False
    assert VpnSettings().policy == vpn.Policy.FREE
    # L'ordre est un ordre de preference, et il doit avoir une valeur de
    # depart : une liste vide interdirait d'activer la recherche.
    assert SubtitleSettings().languages == ["fr", "en"]


def test_les_reglages_survivent_a_un_aller_retour_sur_le_disque():
    store = get_store()
    prefs = copy.deepcopy(store.load())
    prefs.subtitles = SubtitleSettings(
        enabled=True, opensubtitles_api_key="cle-de-test", languages=["fr", "en"]
    )
    prefs.vpn = VpnSettings(policy=vpn.Policy.WARN, reference_ip="203.0.113.7")
    store.save(prefs)

    store._cache = None  # relecture reelle du fichier, pas du cache
    relu = store.load()
    assert relu.subtitles.enabled is True
    assert relu.subtitles.languages == ["fr", "en"]
    assert relu.vpn.policy == "warn"
    assert relu.vpn.reference_ip == "203.0.113.7"


# --- Les refus, qui sont l'interet du reglage -------------------------------


def test_activer_les_sous_titres_sans_cle_est_refuse():
    store = get_store()
    prefs = copy.deepcopy(store.load())
    prefs.subtitles = SubtitleSettings(enabled=True, opensubtitles_api_key="")
    with pytest.raises(PreferenceError, match="OpenSubtitles"):
        store.save(prefs)


def test_activer_les_sous_titres_sans_langue_est_refuse():
    store = get_store()
    prefs = copy.deepcopy(store.load())
    prefs.subtitles = SubtitleSettings(enabled=True, opensubtitles_api_key="k", languages=[])
    with pytest.raises(PreferenceError, match="langue"):
        store.save(prefs)


def test_les_langues_sont_normalisees_et_dedoublonnees():
    """« FR », « fre » et « fr-FR » designent la meme langue.

    Deux ecritures de la meme langue feraient chercher deux fois ce qui vient
    d'etre depose, et l'ordre — qui est un ordre de preference — deviendrait
    illisible.
    """
    store = get_store()
    prefs = copy.deepcopy(store.load())
    prefs.subtitles = SubtitleSettings(
        enabled=True,
        opensubtitles_api_key="k",
        languages=["FR", "fre", "en-US", "fr-FR"],
    )
    store.save(prefs)
    assert store.load().subtitles.languages == ["fr", "en"]


def test_une_langue_inventee_est_refusee():
    store = get_store()
    prefs = copy.deepcopy(store.load())
    prefs.subtitles = SubtitleSettings(
        enabled=True, opensubtitles_api_key="k", languages=["klingon"]
    )
    with pytest.raises(PreferenceError, match="klingon"):
        store.save(prefs)


def test_une_politique_inconnue_est_refusee():
    store = get_store()
    prefs = copy.deepcopy(store.load())
    prefs.vpn = VpnSettings(policy="peut-etre")
    with pytest.raises(PreferenceError, match="politique"):
        store.save(prefs)


def test_une_adresse_de_reference_qui_n_en_est_pas_une_est_refusee():
    store = get_store()
    prefs = copy.deepcopy(store.load())
    prefs.vpn = VpnSettings(reference_ip="chez moi")
    with pytest.raises(PreferenceError, match="adresse IP"):
        store.save(prefs)


# --- L'API les rend et les accepte ------------------------------------------


def test_les_deux_blocs_sortent_par_l_api(client: TestClient):
    prefs = client.get("/api/settings/preferences").json()
    assert "subtitles" in prefs, "le bloc n'atteint pas l'interface"
    assert "vpn" in prefs

    # Les trois politiques voyagent avec leur phrase : l'interface ne doit pas
    # avoir a redupliquer une liste qui vit dans le noyau.
    cles = {p["key"] for p in prefs["vpn"]["policies"]}
    assert cles == {"free", "warn", "require"}
    assert all(p["summary"] for p in prefs["vpn"]["policies"])


def test_la_cle_opensubtitles_ne_sort_jamais(client: TestClient):
    """Le meme contrat que toutes les autres cles du projet."""
    reponse = client.put(
        "/api/settings/preferences",
        json={"subtitles": {"opensubtitles_api_key": "cle-tres-secrete"}},
    )
    assert reponse.status_code == 200
    corps = reponse.text
    assert "cle-tres-secrete" not in corps
    assert reponse.json()["subtitles"]["opensubtitles_key_set"] is True


def test_une_cle_vide_conserve_celle_qui_est_enregistree(client: TestClient):
    """L'interface renvoie le formulaire entier sans connaitre la valeur.

    Sans cette convention, changer une langue effacerait la cle.
    """
    client.put(
        "/api/settings/preferences",
        json={"subtitles": {"opensubtitles_api_key": "gardee"}},
    )
    client.put("/api/settings/preferences", json={"subtitles": {"overwrite": True}})
    assert get_store().load().subtitles.opensubtitles_api_key == "gardee"

    # « - » reste le seul moyen d'effacer : sans lui, une cle saisie par erreur
    # ne pourrait plus jamais etre retiree depuis l'interface.
    client.put("/api/settings/preferences", json={"subtitles": {"opensubtitles_api_key": "-"}})
    assert get_store().load().subtitles.opensubtitles_api_key == ""


def test_le_temoin_vpn_s_efface_par_un_tiret(client: TestClient):
    client.put("/api/settings/preferences", json={"vpn": {"reference_ip": "198.51.100.4"}})
    assert get_store().load().vpn.reference_ip == "198.51.100.4"
    client.put("/api/settings/preferences", json={"vpn": {"reference_ip": "-"}})
    assert get_store().load().vpn.reference_ip == ""


def test_une_politique_refusee_rend_400_et_pas_500(client: TestClient):
    reponse = client.put("/api/settings/preferences", json={"vpn": {"policy": "peut-etre"}})
    assert reponse.status_code == 400
    assert "politique" in reponse.json()["detail"]


# --- Le garde-fou refuse vraiment -------------------------------------------


def _mesure(**kwargs) -> vpn.Measure:
    return vpn.Measure(**kwargs)


def test_le_mode_libre_n_emet_aucune_mesure(monkeypatch):
    """Mesurer pour n'exiger rien ajouterait une requete par lot."""
    appels = 0

    async def _jamais(*args, **kwargs):
        nonlocal appels
        appels += 1
        return _mesure()

    monkeypatch.setattr(vpn, "measure_egress", _jamais)
    decision = asyncio.run(vpn.egress_allowed(vpn.Policy.FREE))
    assert decision.allowed is True
    assert appels == 0


def test_le_mode_exiger_refuse_quand_la_mesure_echoue(monkeypatch):
    """Le point entier du module.

    Une politique qui s'ouvre au moindre doute ne protege rien : il suffirait
    de faire tomber la verification pour la contourner.
    """

    async def _panne(*args, **kwargs):
        return _mesure(error="service d'echo injoignable")

    monkeypatch.setattr(vpn, "measure_egress", _panne)
    decision = asyncio.run(vpn.egress_allowed(vpn.Policy.REQUIRE))
    assert decision.allowed is False
    assert decision.reason


def test_le_mode_signaler_laisse_passer_en_avertissant(monkeypatch):
    async def _panne(*args, **kwargs):
        return _mesure(error="service d'echo injoignable")

    monkeypatch.setattr(vpn, "measure_egress", _panne)
    decision = asyncio.run(vpn.egress_allowed(vpn.Policy.WARN))
    assert decision.allowed is True
    assert decision.warn is True


def test_identifier_est_refuse_en_409_quand_la_sortie_ne_l_est_pas(client: TestClient, monkeypatch):
    """Le garde-fou est pose AVANT le premier appel sortant.

    409 et non 400 : la demande est valable, c'est l'etat du moment qui s'y
    oppose — elle redeviendra recevable sans que rien n'ait ete ressaisi.
    """
    from sortilege.api import review
    from sortilege.core.parser import parse
    from sortilege.core.probe import FileProbe
    from sortilege.core.scanner import ScannedFile, ScanResult

    async def _refus(*args, **kwargs):
        return vpn.Decision(allowed=False, warn=True, reason="Emission refusee : essai.")

    monkeypatch.setattr(review, "egress_allowed", _refus)
    # Un scan non vide et une cle : sans eux, la route repondait 400 avant
    # d'atteindre le garde-fou, et le test passait sans rien prouver.
    film = Path("/tmp/sortilege-test/downloads/Dune.2021.mkv")
    scanne = ScannedFile(
        path=film, size_bytes=1, parsed=parse(film, ["downloads"]), probe=FileProbe()
    )
    monkeypatch.setattr(review, "last_scan", lambda: ScanResult(files=[scanne]))
    monkeypatch.setattr(review, "tmdb_key", lambda: "cle-de-test")

    reponse = client.post("/api/review/plan")
    assert reponse.status_code == 409
    assert "refusee" in reponse.json()["detail"]


# --- Les livres et les sous-titres ont une porte d'entree -------------------


def test_la_route_des_sous_titres_refuse_tant_que_c_est_desactive(client: TestClient):
    reponse = client.post("/api/review/subtitles-library")
    assert reponse.status_code == 400
    assert "désactivée" in reponse.json()["detail"]


def test_la_route_des_metadonnees_locales_cite_les_deux_formats(client: TestClient):
    """Un utilisateur qui ne range que des livres doit pouvoir s'en servir.

    Le refus ne parlait que des fiches ``.nfo``, qui ne concernent pas les
    livres : il envoyait activer un reglage sans effet sur eux.
    """
    reponse = client.post("/api/review/nfo-library")
    assert reponse.status_code == 400
    detail = reponse.json()["detail"]
    assert "manifestes" in detail or "livres" in detail


def test_le_prereglage_jellyfin_donne_un_dossier_par_livre():
    """Jellyfin ne lit les metadonnees d'un livre que dans son propre dossier.

    Deux livres dans le meme dossier n'auraient qu'un « metadata.opf » pour
    deux — c'est la raison d'etre du niveau supplementaire, pas un rangement
    de plus par gout.
    """
    from sortilege.core.template import PRESETS, render

    gabarit = PRESETS["jellyfin"]["book"]
    rendu = render(
        gabarit,
        {
            "author": "Frank Herbert",
            "series": "Dune",
            "volume": 1,
            "title": "Dune",
            "year": 1965,
        },
    )
    dossier, _, fichier = rendu.rpartition("/")
    assert dossier.endswith("01 - Dune (1965)")
    assert fichier == "01 - Dune (1965)"


def test_un_livre_range_recoit_son_manifeste(tmp_path):
    """Le depot passe par le meme chemin que les fiches video.

    Verifie ici et pas seulement dans ``test_opf`` : le module etait juste et
    n'etait appele par personne. Le test passe par ``apply_plan``, la porte
    qu'emprunte un vrai rangement, et non par la fonction interne qu'elle
    appelle : c'est le branchement qu'on veut prouver.
    """
    from sortilege.core.journal import Journal, apply_plan
    from sortilege.core.nfo import LocalMetadataSettings
    from sortilege.core.planner import Plan
    from sortilege.core.scoring import Decision

    journal = Journal(tmp_path / "j.jsonl")

    def ranger(nom, reglages):
        source = tmp_path / "telechargements" / f"{nom}.epub"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"pas un vrai epub")
        plan = Plan(
            id=nom,
            source=source,
            destination=tmp_path / "Livres" / nom / f"{nom}.epub",
            kind="book",
            score=1.0,
            decision=Decision.AUTO,
            title=nom,
        )
        resultat = apply_plan(plan, journal, dry_run=False, local_metadata=reglages)
        assert resultat.ok, resultat.message
        assert plan.destination.is_file()
        return plan.destination.parent, resultat.message

    # Desactive : le livre est range, rien d'autre n'apparait.
    dossier, message = ranger("Auteur - Premier", LocalMetadataSettings())
    assert not (dossier / "metadata.opf").exists()
    assert "manifeste" not in message

    # Active : le manifeste est ecrit, meme sur un fichier illisible - le repli
    # par le nom sert exactement a cela.
    dossier, message = ranger("Auteur - Second", LocalMetadataSettings(opf=True))
    assert "manifeste" in message
    assert (dossier / "metadata.opf").is_file()
