"""Verifier par ou sort le trafic — et surtout, ne jamais mentir sur ce qu'on sait.

Ces tests portent moins sur la mesure que sur la separation de trois questions
qu'il est tentant de confondre, et couteux de confondre :

- **une interface de tunnel existe** ne dit pas que le trafic passe dedans ;
- **la mesure a echoue** ne dit pas qu'il n'y a pas de VPN ;
- **l'organisation est inconnue** ne dit pas qu'on sort en clair.

Chacune de ces confusions produit la meme faute, dans un sens ou dans l'autre :
un etat affiche qui ne correspond pas a la realite du trafic. Un interrupteur
« VPN » qui se trompe ainsi est pire que pas d'interrupteur du tout, puisqu'il
fait ranger en confiance.

Aucun test ne touche le reseau : les services d'echo sont simules. Un test qui
sortirait vraiment ferait exactement ce que le module cherche a controler — et
son resultat dependrait de la connexion du poste.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from pathlib import Path

import httpx
import pytest

from sortilege.core import vpn

IP_MAISON = "88.120.4.17"
IP_TUNNEL = "193.32.127.66"
ORG_MULLVAD = "AS20473 Mullvad VPN AB"
ORG_QUELCONQUE = "AS3215 Orange S.A."


# --- Outillage ---------------------------------------------------------------


def reponse(ip: str, organisation: str | None = None) -> httpx.Response:
    """Une reponse de service d'echo.

    L'organisation est posee sous les deux noms de champ utilises par les
    services connus : la reponse reste exploitable quel que soit celui qui a
    fini par repondre, donc le test ne casse pas si l'ordre change.
    """
    charge: dict[str, str] = {"ip": ip}
    if organisation:
        charge["org"] = organisation
        charge["asn_org"] = organisation
    return httpx.Response(200, json=charge)


class Echo:
    """Repond a la place des services d'echo, et compte les appels."""

    def __init__(self, *reponses: httpx.Response | Exception) -> None:
        self._reponses: list[httpx.Response | Exception] = list(reponses) or [
            reponse(IP_TUNNEL, ORG_MULLVAD)
        ]
        self.urls: list[str] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        # La derniere reponse est repetee indefiniment : « tous les services
        # echouent » s'ecrit alors avec une seule.
        prevue = self._reponses[min(len(self.urls) - 1, len(self._reponses) - 1)]
        if isinstance(prevue, Exception):
            raise prevue
        return prevue

    @property
    def appels(self) -> int:
        return len(self.urls)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handle))


async def statut_de(echo: Echo, **kwargs) -> vpn.VpnStatus:
    client = echo.client()
    try:
        return await vpn.check(client=client, **kwargs)
    finally:
        await client.aclose()


async def decision_de(echo: Echo, policy: vpn.Policy, **kwargs) -> vpn.Decision:
    client = echo.client()
    try:
        return await vpn.egress_allowed(policy, client=client, **kwargs)
    finally:
        await client.aclose()


def ecrire_proc_net_dev(chemin: Path, noms: Iterable[str]) -> Path:
    """Reproduit le format reel, en-tete comprise.

    L'en-tete compte : sa seconde ligne ressemble a une interface et doit etre
    ignoree sans regle particuliere.
    """
    lignes = [
        "Inter-|   Receive                    |  Transmit",
        " face |bytes    packets errs drop fifo frame compressed multicast",
    ]
    lignes += [f"{nom:>7}: 1234 10 0 0 0 0 0 0 5678 9 0 0 0 0 0 0" for nom in noms]
    chemin.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    return chemin


@pytest.fixture
def interfaces(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fixe ce que le systeme montre de ses interfaces."""

    def poser(*noms: str, lisible: bool = True, via_sysfs: bool = False) -> None:
        monkeypatch.setattr(vpn, "PROC_NET_DEV", tmp_path / "proc-absent")
        monkeypatch.setattr(vpn, "SYS_CLASS_NET", tmp_path / "sysfs-absent")
        if not lisible:
            return
        if via_sysfs:
            racine = tmp_path / "sysfs"
            racine.mkdir(exist_ok=True)
            for nom in noms:
                (racine / nom).mkdir(exist_ok=True)
            monkeypatch.setattr(vpn, "SYS_CLASS_NET", racine)
            return
        monkeypatch.setattr(vpn, "PROC_NET_DEV", ecrire_proc_net_dev(tmp_path / "dev", noms))

    return poser


@pytest.fixture(autouse=True)
def _environnement(interfaces) -> Iterable[None]:
    """Machine ordinaire par defaut, et cache vide entre deux tests.

    Sans ce point de depart fixe, les tests liraient les interfaces du poste ou
    du coureur d'integration : verts sur un portable, rouges sur une machine
    qui porte un tunnel.
    """
    vpn.reset_cache()
    interfaces("lo", "eth0")
    yield
    vpn.reset_cache()


# --- Les trois questions, tenues separees ------------------------------------


async def test_une_interface_de_tunnel_ne_prouve_pas_la_sortie(interfaces) -> None:
    """LE cas qui justifie tout le module.

    Un ``wg0`` monte, et pourtant l'adresse publique est celle de la maison :
    la route par defaut ignore le tunnel. Un module qui se contenterait de
    lister les interfaces afficherait « protege » pendant que tout sort en
    clair.
    """
    interfaces("lo", "eth0", "wg0")
    statut = await statut_de(Echo(reponse(IP_MAISON)), reference_ip=IP_MAISON)

    assert statut.tunnel_present is True
    assert statut.egress_confirmed is False
    assert statut.measured is True
    assert statut.verdict is vpn.Verdict.EXPOSED


async def test_une_mesure_qui_echoue_ne_dit_pas_qu_il_n_y_a_pas_de_vpn() -> None:
    """Un service injoignable est une panne, pas un constat."""
    statut = await statut_de(Echo(httpx.Response(503)), reference_ip=IP_MAISON)

    assert statut.measured is False
    assert statut.verdict is vpn.Verdict.UNAVAILABLE
    assert statut.egress_confirmed is None
    assert statut.verdict is not vpn.Verdict.EXPOSED
    assert "pas une absence de VPN" in statut.explanation


async def test_une_sortie_confirmee_sans_aucune_interface_visible(interfaces) -> None:
    """Le VPN porte par le routeur : rien a voir dans le conteneur, et
    pourtant le trafic ne sort pas par la maison. Faire dependre le verdict de
    la question 1 refuserait a tort ce montage, qui est courant."""
    interfaces("lo", "eth0")
    statut = await statut_de(Echo(reponse(IP_TUNNEL)), reference_ip=IP_MAISON)

    assert statut.tunnel_present is False
    assert statut.egress_confirmed is True
    assert statut.verdict is vpn.Verdict.PROTECTED
    assert "routeur" in statut.explanation


# --- L'identification du fournisseur -----------------------------------------


async def test_un_fournisseur_connu_est_nomme_sans_reference() -> None:
    """L'organisation suffit : c'est le seul indice qui ne perime pas."""
    statut = await statut_de(Echo(reponse(IP_TUNNEL, ORG_MULLVAD)))

    assert statut.verdict is vpn.Verdict.PROTECTED
    assert statut.provider == "Mullvad"
    assert statut.organization == "Mullvad VPN AB"
    assert "Mullvad" in statut.explanation


async def test_un_fournisseur_inconnu_est_dit_inconnu_pas_absent() -> None:
    """« Fournisseur non identifie » est vrai ; « pas de VPN » ne l'est pas."""
    statut = await statut_de(Echo(reponse(IP_TUNNEL, ORG_QUELCONQUE)))

    assert statut.verdict is vpn.Verdict.UNKNOWN
    assert statut.provider is None
    assert statut.egress_confirmed is None
    assert statut.measured is True
    assert "rien ne permet de dire par où elle sort" in statut.explanation


async def test_le_nom_d_interface_nomme_le_fournisseur_sans_confirmer_la_sortie(
    interfaces,
) -> None:
    """``nordlynx`` designe NordVPN, et ne dit rien de la route par defaut."""
    interfaces("lo", "eth0", "nordlynx")
    statut = await statut_de(Echo(reponse(IP_TUNNEL, ORG_QUELCONQUE)))

    assert statut.provider == "NordVPN"
    assert statut.tunnel_present is True
    assert statut.egress_confirmed is None
    assert statut.verdict is vpn.Verdict.UNKNOWN


# --- Le basculement entre services d'echo ------------------------------------


async def test_un_service_indisponible_ne_rend_pas_la_verification_indisponible() -> None:
    """Sans basculement, le mode « exiger » bloquerait tout le rangement des
    qu'un service tiers tousse — et serait desactive dans la foulee."""
    echo = Echo(httpx.ConnectError("panne"), httpx.Response(500), reponse(IP_TUNNEL))
    statut = await statut_de(echo, reference_ip=IP_MAISON)

    assert echo.appels == 3
    assert statut.measured is True
    assert statut.public_ip == IP_TUNNEL
    assert statut.echo_service == vpn.ECHO_SERVICES[2].name


async def test_une_adresse_privee_renvoyee_par_un_echo_fait_basculer() -> None:
    """Un service joignable depuis Internet ne peut pas voir une adresse
    privee : c'est un portail captif qui a repondu. La retenir la ferait
    passer pour une adresse « differente de la maison », donc pour une
    protection."""
    echo = Echo(reponse("192.168.1.10"), reponse(IP_TUNNEL, ORG_MULLVAD))
    statut = await statut_de(echo, reference_ip=IP_MAISON)

    assert echo.appels == 2
    assert statut.public_ip == IP_TUNNEL
    assert statut.error is None


async def test_tous_les_services_muets_donnent_une_mesure_indisponible() -> None:
    echo = Echo(httpx.ConnectTimeout("trop long"))
    statut = await statut_de(echo)

    assert echo.appels == len(vpn.ECHO_SERVICES)
    assert statut.verdict is vpn.Verdict.UNAVAILABLE
    assert vpn.ECHO_SERVICES[0].name in (statut.error or "")


# --- Les interfaces locales --------------------------------------------------


async def test_un_systeme_sans_proc_net_dev_le_dit_au_lieu_de_conclure(interfaces) -> None:
    """Sur un systeme qui n'expose pas ses interfaces, repondre « aucun
    tunnel » serait une conclusion tiree du vide."""
    interfaces(lisible=False)
    statut = await statut_de(Echo(reponse(IP_TUNNEL, ORG_MULLVAD)))

    assert statut.tunnel_present is None
    assert statut.tunnel_interfaces == ()
    assert "ne sont pas lisibles" in statut.explanation
    # La question 1 reste sans reponse, la question 2 est tranchee quand meme.
    assert statut.verdict is vpn.Verdict.PROTECTED


def test_les_interfaces_ordinaires_ne_sont_pas_des_tunnels(tmp_path: Path) -> None:
    """``ppp0`` est le piege : c'est le lien DSL, donc l'inverse d'un tunnel."""
    chemin = ecrire_proc_net_dev(
        tmp_path / "dev", ["lo", "eth0", "wlan0", "ppp0", "docker0", "veth7a1", "br-1f2e"]
    )
    tunnels, lisible = vpn.read_tunnel_interfaces(proc_net_dev=chemin, sys_class_net=tmp_path / "x")

    assert lisible is True
    assert tunnels == ()


def test_les_interfaces_de_tunnel_sont_reconnues(tmp_path: Path) -> None:
    chemin = ecrire_proc_net_dev(
        tmp_path / "dev", ["lo", "eth0", "wg0", "tun0", "nordlynx", "proton0", "utun3"]
    )
    tunnels, _ = vpn.read_tunnel_interfaces(proc_net_dev=chemin, sys_class_net=tmp_path / "x")

    assert tunnels == ("nordlynx", "proton0", "tun0", "utun3", "wg0")


def test_les_interfaces_se_lisent_aussi_dans_sys_class_net(tmp_path: Path) -> None:
    """Deux sources, parce qu'un noyau peut monter l'une sans l'autre."""
    racine = tmp_path / "net"
    for nom in ("lo", "eth0", "wg0"):
        (racine / nom).mkdir(parents=True)
    tunnels, lisible = vpn.read_tunnel_interfaces(
        proc_net_dev=tmp_path / "absent", sys_class_net=racine
    )

    assert lisible is True
    assert tunnels == ("wg0",)


def test_aucune_source_lisible_se_signale(tmp_path: Path) -> None:
    tunnels, lisible = vpn.read_tunnel_interfaces(
        proc_net_dev=tmp_path / "ni", sys_class_net=tmp_path / "l-un"
    )

    assert lisible is False
    assert tunnels == ()


# --- La politique ------------------------------------------------------------


async def test_le_mode_libre_n_emet_aucune_requete() -> None:
    """Mesurer pour annoncer qu'on n'exige rien ajouterait le trafic — et le
    tiers — que ce mode ne demande pas."""
    echo = Echo()
    decision = await decision_de(echo, vpn.Policy.FREE, reference_ip=IP_MAISON)

    assert echo.appels == 0
    assert decision.allowed is True
    assert decision.warn is False
    assert decision.status is None


async def test_le_mode_avertir_laisse_passer_en_signalant() -> None:
    decision = await decision_de(Echo(reponse(IP_MAISON)), vpn.Policy.WARN, reference_ip=IP_MAISON)

    assert decision.allowed is True
    assert decision.warn is True
    assert decision.status is not None
    assert decision.status.verdict is vpn.Verdict.EXPOSED


async def test_le_mode_avertir_ne_signale_rien_quand_la_sortie_est_confirmee() -> None:
    decision = await decision_de(Echo(reponse(IP_TUNNEL, ORG_MULLVAD)), vpn.Policy.WARN)

    assert decision.allowed is True
    assert decision.warn is False


async def test_le_mode_exiger_laisse_passer_une_sortie_confirmee() -> None:
    decision = await decision_de(Echo(reponse(IP_TUNNEL, ORG_MULLVAD)), vpn.Policy.REQUIRE)

    assert decision.allowed is True
    assert decision.warn is False


async def test_le_mode_exiger_refuse_une_sortie_exposee() -> None:
    decision = await decision_de(
        Echo(reponse(IP_MAISON)), vpn.Policy.REQUIRE, reference_ip=IP_MAISON
    )

    assert decision.allowed is False
    assert "refusée" in decision.reason


async def test_le_mode_exiger_refuse_aussi_quand_la_mesure_echoue() -> None:
    """Le point entier du module : une politique qui s'ouvre au moindre doute
    se contourne en faisant tomber la verification."""
    decision = await decision_de(
        Echo(httpx.Response(503)), vpn.Policy.REQUIRE, reference_ip=IP_MAISON
    )

    assert decision.allowed is False
    assert decision.status is not None
    assert decision.status.verdict is vpn.Verdict.UNAVAILABLE
    assert decision.status.measured is False


async def test_le_mode_exiger_refuse_quand_rien_ne_permet_de_trancher() -> None:
    """Fournisseur inconnu et pas de reference : « exiger » exige une
    confirmation, pas une absence d'infirmation."""
    decision = await decision_de(Echo(reponse(IP_TUNNEL, ORG_QUELCONQUE)), vpn.Policy.REQUIRE)

    assert decision.allowed is False
    assert decision.status is not None
    assert decision.status.verdict is vpn.Verdict.UNKNOWN


async def test_le_mode_se_lit_depuis_une_chaine() -> None:
    """Les preferences stockent du texte ; il doit arriver ici sans conversion."""
    decision = await decision_de(Echo(), "free")

    assert decision.allowed is True


# --- Le cache ----------------------------------------------------------------


async def test_la_mesure_n_est_pas_refaite_a_chaque_appel() -> None:
    """Verifier avant chaque appel de fournisseur emettrait plus de requetes
    que ce qu'on protege."""
    echo = Echo()
    client = echo.client()
    try:
        for _ in range(5):
            await vpn.check(client=client)
    finally:
        await client.aclose()

    assert echo.appels == 1


async def test_huit_appels_simultanes_ne_font_qu_une_mesure() -> None:
    """La rafale reelle : sept fournisseurs d'IA plus TheMovieDB demarrent
    ensemble et trouvent tous un cache encore vide."""
    echo = Echo()
    client = echo.client()
    try:
        await asyncio.gather(
            *(vpn.egress_allowed(vpn.Policy.WARN, client=client) for _ in range(8))
        )
    finally:
        await client.aclose()

    assert echo.appels == 1


async def test_une_mesure_perimee_est_refaite(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vpn, "CACHE_TTL", 0.0)
    echo = Echo()
    client = echo.client()
    try:
        await vpn.check(client=client)
        await vpn.check(client=client)
    finally:
        await client.aclose()

    assert echo.appels == 2


async def test_une_mesure_peut_etre_forcee() -> None:
    """Le bouton « verifier maintenant » ne doit pas relire une minute de cache."""
    echo = Echo()
    client = echo.client()
    try:
        await vpn.check(client=client)
        await vpn.check(client=client, force=True)
    finally:
        await client.aclose()

    assert echo.appels == 2


async def test_le_verdict_suit_la_reference_sans_remesurer() -> None:
    """La mesure coute une requete, le verdict depend d'un reglage : les
    changer ensemble ferait payer une requete a chaque changement d'avis."""
    echo = Echo(reponse(IP_MAISON))
    client = echo.client()
    try:
        sans = await vpn.check(client=client)
        avec = await vpn.check(client=client, reference_ip=IP_MAISON)
    finally:
        await client.aclose()

    assert echo.appels == 1
    assert sans.verdict is vpn.Verdict.UNKNOWN
    assert avec.verdict is vpn.Verdict.EXPOSED


# --- Ce que la politique promet ----------------------------------------------


def test_le_texte_d_exiger_nomme_ses_deux_exceptions() -> None:
    """« N'emet aucune requete » etait faux : le serveur multimedia et la
    mesure elle-meme continuent. Un texte qui promet plus que le code fait
    ranger en confiance sur une garantie qui n'existe pas."""
    exiger = next(p for p in vpn.POLICIES if p.key == vpn.Policy.REQUIRE)

    assert "aucune requête" not in exiger.summary
    assert "serveur multimédia" in exiger.summary
    assert "vérification" in exiger.summary
    for arrete in ("identification", "collections", "affiches", "sous-titres", "Discord"):
        assert arrete in exiger.summary
