"""Verifier par ou sort le trafic — sans jamais pretendre l'avoir detourne.

**Une correction d'abord.** On a d'abord repondu qu'une liaison VPN n'avait pas
de sens tant que Sortilege ne telecharge rien. C'etait faux. Sortilege emet
deja, a chaque identification : TheMovieDB, AniList, OpenSubtitles, le webhook
Discord et sept fournisseurs d'IA. Chacun de ces appels montre l'adresse
publique de la maison a un tiers, et la suite des requetes dessine, chez qui
les recoit, le catalogue de ce qu'on regarde et l'heure a laquelle on range.
Le trafic sortant existe depuis le premier jour ; c'est sa verification qui
manquait.

**Ce module ne monte aucun tunnel, et c'est delibere.** Etablir une liaison
WireGuard depuis un processus Python demanderait des privileges reseau qu'un
conteneur ne devrait jamais avoir (CAP_NET_ADMIN), la gestion de cles privees
qu'il faudrait alors stocker, et la reimplementation d'un travail que le
systeme ou un conteneur dedie fait deja mieux — ``gluetun`` route la totalite
du trafic d'un conteneur, y compris ce que Sortilege ne sait pas qu'il emet.
Se brancher dessus coute trois lignes de ``docker-compose``.

**Ce qui manque partout ailleurs, c'est la verification.** Un interrupteur
« VPN » qui ne verifie rien affiche une protection sans la constater : on
range en confiance pendant que tout sort en clair. C'est la seule facon de
faire pire que de ne rien afficher du tout — et c'est le seul travail de ce
module.

Trois questions sont donc tenues separees d'un bout a l'autre, parce que les
confondre est exactement l'erreur qu'on veut eviter :

1. **Une interface de tunnel existe-t-elle ?** Elle peut exister sans que la
   route par defaut passe dedans. Sa presence n'est pas une preuve.
2. **L'adresse publique est-elle celle attendue ?** C'est la seule mesure qui
   porte sur le trafic reellement emis, puisqu'elle est faite *par* ce trafic.
3. **La mesure a-t-elle abouti ?** Une verification qui echoue ne dit pas
   « non ». Confondre les deux, c'est bloquer l'application sur une panne
   reseau, ou — bien pire — la laisser emettre en croyant avoir verifie.

**Une limite, dite franchement.** La comparaison a l'adresse de reference
suppose que celle-ci soit a jour. Une adresse domestique attribuee en DHCP
change parfois toute seule ; ce jour-la, une reference perimee ferait conclure
a tort a une sortie protegee. L'identification du fournisseur par son
organisation ne souffre pas de ce defaut, et c'est pourquoi elle est preferee
quand elle est possible.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 5.0
"""Court, et pour une raison inhabituelle : ce delai s'ajoute AVANT chaque
appel de fournisseur en mode « exiger ». Une verification lente rendrait le
rangement penible, et un utilisateur excede coupe la protection."""

CACHE_TTL = 60.0
"""Duree de validite d'une mesure.

Sans cache, verifier avant chaque appel de fournisseur emettrait plus de
requetes que ce qu'on cherche a proteger — et vers un tiers de plus. Une
minute suffit : une liaison VPN qui tombe le fait pour plusieurs minutes, pas
pour trois secondes."""

PROC_NET_DEV = Path("/proc/net/dev")
SYS_CLASS_NET = Path("/sys/class/net")


@dataclass(frozen=True)
class EchoService:
    """Un service qui renvoie l'adresse publique de celui qui l'interroge."""

    name: str
    url: str
    ip_field: str = "ip"
    """Champ JSON portant l'adresse. Vide = le corps est l'adresse, en clair."""

    org_fields: tuple[str, ...] = ()
    asn_fields: tuple[str, ...] = ()


ECHO_SERVICES: tuple[EchoService, ...] = (
    EchoService("ipinfo.io", "https://ipinfo.io/json", org_fields=("org",)),
    EchoService(
        "ifconfig.co",
        "https://ifconfig.co/json",
        org_fields=("asn_org",),
        asn_fields=("asn",),
    ),
    EchoService("ipify", "https://api.ipify.org?format=json"),
    EchoService("icanhazip", "https://icanhazip.com", ip_field=""),
)
"""Plusieurs services, essayes dans l'ordre, jamais en parallele.

Un point de mesure unique qui tombe rendrait la verification indisponible,
donc bloquante en mode « exiger », donc desactivee par l'utilisateur au bout
de la deuxieme fois — un garde-fou contourne ne protege rien. D'ou le
basculement.

Ils ne sont pas interroges simultanement : ce serait tripler le trafic et
montrer l'adresse a trois tiers au lieu d'un, pour repondre a une question qui
n'en demande qu'un. Ceux qui nomment l'organisation viennent en tete, parce
qu'ils permettent d'identifier le fournisseur sans reference enregistree.

Le paradoxe assume : le service d'echo voit precisement l'adresse qu'on
cherche a masquer. Il ne peut pas en etre autrement — c'est la mesure elle-meme
qui doit emprunter le chemin qu'on verifie."""

VPN_ORGANIZATIONS: tuple[tuple[str, str], ...] = (
    ("mullvad", "Mullvad"),
    ("nordvpn", "NordVPN"),
    ("tefincom", "NordVPN"),
    ("protonvpn", "Proton VPN"),
    ("proton ag", "Proton VPN"),
    ("private internet access", "Private Internet Access"),
    ("expressvpn", "ExpressVPN"),
    ("surfshark", "Surfshark"),
    ("cyberghost", "CyberGhost"),
    ("windscribe", "Windscribe"),
    ("azirevpn", "AzireVPN"),
    ("airvpn", "AirVPN"),
    ("torguard", "TorGuard"),
    ("vyprvpn", "VyprVPN"),
    ("hide.me", "hide.me"),
    ("ivpn", "IVPN"),
)
"""Raisons sociales sans ambiguite, et rien d'autre.

La tentation serait d'ajouter les hebergeurs qui louent des serveurs aux
fournisseurs (M247, DataCamp, Datapacket). Ce serait deviner : les memes
machines hebergent des sites quelconques, et un « VPN detecte » faux est
exactement le mensonge rassurant que ce module existe pour eviter. Quand le
nom n'est pas reconnu, on repond « fournisseur non identifie », qui est vrai —
pas « pas de VPN », qui ne l'est pas."""

TUNNEL_PREFIXES: tuple[tuple[str, str | None], ...] = (
    ("nordlynx", "NordVPN"),
    ("proton", "Proton VPN"),
    ("mullvad", "Mullvad"),
    ("tailscale", "Tailscale"),
    ("wg", None),
    ("utun", None),
    ("tun", None),
    ("tap", None),
    ("ipsec", None),
)
"""Prefixes d'interfaces de tunnel.

``ppp`` en est volontairement absent : ``ppp0``, c'est le plus souvent le lien
DSL vers le fournisseur d'acces — soit exactement la connexion domestique
qu'on cherche a ne PAS emprunter. Le compter comme un tunnel inverserait le
verdict.

``tailscale`` y figure parce que l'interface existe bel et bien, mais un
reseau maille ne route la sortie Internet que si un noeud de sortie est
choisi : raison de plus pour que la presence d'une interface ne vaille jamais
preuve de sortie."""

_AS_PREFIX = re.compile(r"^(AS\d+)\s+(.+)$", re.IGNORECASE)

# --- Etat mesure et verdicts -------------------------------------------------


class Verdict(StrEnum):
    PROTECTED = "protected"
    """La sortie par le tunnel est constatee, pas supposee."""

    EXPOSED = "exposed"
    """L'adresse publique est celle de la maison : le trafic sort en clair."""

    UNKNOWN = "unknown"
    """Adresse mesuree, mais rien ne permet de dire par ou elle sort."""

    UNAVAILABLE = "unavailable"
    """La mesure n'a pas abouti. Ce n'est PAS une absence de VPN."""


class Policy(StrEnum):
    FREE = "free"
    """On n'exige rien — le comportement d'avant ce module."""

    WARN = "warn"
    """On emet, mais l'etat est signale."""

    REQUIRE = "require"
    """Rien ne part vers un tiers tant que la sortie par le tunnel n'est pas
    confirmee, y compris quand la mesure echoue. Sont arretes : l'identification,
    la recherche et le choix d'un candidat, la construction des collections, le
    telechargement des affiches, les sous-titres, les notifications Discord, et
    les boutons d'essai de TheMovieDB, du resolveur IA, d'OpenSubtitles et de
    Discord.

    Deux exceptions, et seulement deux. Le rafraichissement du serveur
    multimedia, qui reste sur le reseau local et ne montre donc l'adresse
    publique a personne ; et la mesure de sortie elle-meme, sans laquelle rien
    ne serait jamais confirme.

    Cette liste est celle que resume ``POLICIES`` a l'ecran : l'une ne change
    pas sans l'autre."""


@dataclass(frozen=True)
class PolicyChoice:
    """Une politique, et la phrase qui dit ce qu'elle fait vraiment.

    Le libelle vit ICI et non dans l'interface : « exiger » ne dit pas, a lui
    seul, qu'une mesure ratee vaut refus. Un utilisateur qui l'apprend en
    voyant ses identifications s'arreter l'apprend trop tard.
    """

    key: str
    label: str
    summary: str


POLICIES: tuple[PolicyChoice, ...] = (
    PolicyChoice(
        key=Policy.FREE,
        label="Libre",
        summary=(
            "Aucune vérification, aucune mesure. Les requêtes partent par la route "
            "habituelle du conteneur."
        ),
    ),
    PolicyChoice(
        key=Policy.WARN,
        label="Signaler",
        summary=(
            "Mesure la sortie avant chaque lot et affiche l'état, sans jamais bloquer. "
            "Pour constater ce qui se passe avant d'exiger quoi que ce soit."
        ),
    ),
    PolicyChoice(
        key=Policy.REQUIRE,
        label="Exiger",
        summary=(
            "Bloque tout envoi vers un tiers tant que la sortie par le tunnel n'est pas "
            "confirmée, y compris quand la vérification échoue : identification, recherche "
            "et choix d'un candidat, collections, affiches, sous-titres, notifications "
            "Discord et boutons d'essai. Seuls le rafraîchissement du serveur multimédia "
            "(réseau local) et la vérification elle-même continuent. Un refus se relance ; "
            "une fuite ne se rattrape pas."
        ),
    ),
)


@dataclass(frozen=True)
class Measure:
    """Ce qui a ete observe, sans interpretation.

    Separer la mesure du verdict n'est pas de la coquetterie : le verdict
    depend d'un reglage — l'adresse de reference — qui peut changer d'un appel
    a l'autre, alors que la mesure, elle, coute une requete reseau. On met donc
    en cache la premiere et on recalcule le second a chaque fois.
    """

    public_ip: str | None = None
    organization: str | None = None
    asn: str | None = None
    echo_service: str | None = None
    interfaces: tuple[str, ...] = ()
    interfaces_readable: bool = False
    error: str | None = None
    at: float = 0.0
    """Horloge monotone : sert la peremption du cache, pas l'affichage."""

    @property
    def succeeded(self) -> bool:
        return self.public_ip is not None


@dataclass(frozen=True)
class VpnStatus:
    """Le verdict, et les trois reponses qui le composent — distinctes."""

    verdict: Verdict
    explanation: str
    """Redigee pour etre affichee telle quelle, sans reformulation."""

    measured: bool
    """Question 3 : la mesure a-t-elle abouti ?"""

    tunnel_present: bool | None
    """Question 1. ``None`` quand le systeme ne montre pas ses interfaces —
    ce qui n'est pas la meme chose que « aucun tunnel »."""

    egress_confirmed: bool | None
    """Question 2. ``None`` quand rien ne permet de trancher."""

    tunnel_interfaces: tuple[str, ...] = ()
    public_ip: str | None = None
    organization: str | None = None
    provider: str | None = None
    echo_service: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class Decision:
    """Reponse a « ai-je le droit d'emettre maintenant ? »."""

    allowed: bool
    warn: bool
    reason: str
    status: VpnStatus | None = None
    """Absent en mode « libre » : ce mode ne mesure rien, donc n'a rien a dire."""


# --- Interfaces locales ------------------------------------------------------


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # Attendu hors Linux, et sur un noyau qui ne monte pas /proc. On le
        # note sans bruit : c'est l'appelant qui decidera quoi en dire.
        logger.debug("interfaces illisibles dans %s : %s", path, exc)
        return None


def interface_provider(name: str) -> tuple[bool, str | None]:
    """Cette interface est-elle un tunnel, et de quel fournisseur ?"""
    lowered = name.strip().lower()
    for prefix, provider in TUNNEL_PREFIXES:
        if lowered.startswith(prefix):
            return True, provider
    return False, None


def read_tunnel_interfaces(
    proc_net_dev: Path | None = None, sys_class_net: Path | None = None
) -> tuple[tuple[str, ...], bool]:
    """Les interfaces de tunnel presentes, et si l'on a pu regarder.

    Deux sources plutot qu'une, et aucune dependance nouvelle : la cible reelle
    est un conteneur Linux, ou ces deux fichiers suffisent. Le second element
    du couple est ce qui compte le plus — sur un systeme qui n'expose ni l'un
    ni l'autre, repondre « aucun tunnel » serait une conclusion tiree du vide.
    """
    proc = PROC_NET_DEV if proc_net_dev is None else proc_net_dev
    sysfs = SYS_CLASS_NET if sys_class_net is None else sys_class_net

    names: set[str] = set()
    readable = False

    content = _read_text(proc)
    if content is not None:
        readable = True
        for line in content.splitlines():
            # Format : « <nom>: <compteurs> ». Les deux lignes d'en-tete ne
            # contiennent pas de deux-points suivi de compteurs, sauf la
            # seconde — dont le nom vaut « face », qui ne matche aucun prefixe.
            head, separator, _ = line.partition(":")
            if separator:
                names.add(head.strip())

    try:
        entries = [entry.name for entry in sysfs.iterdir()]
    except OSError as exc:
        logger.debug("interfaces illisibles dans %s : %s", sysfs, exc)
    else:
        readable = True
        names.update(entries)

    tunnels = tuple(sorted(name for name in names if name and interface_provider(name)[0]))
    return tunnels, readable


def provider_from_interfaces(names: tuple[str, ...]) -> str | None:
    for name in names:
        _, provider = interface_provider(name)
        if provider:
            return provider
    return None


def provider_from_organization(organization: str | None) -> str | None:
    if not organization:
        return None
    lowered = organization.lower()
    for needle, provider in VPN_ORGANIZATIONS:
        if needle in lowered:
            return provider
    return None


# --- Mesure ------------------------------------------------------------------


def _public_ip(raw: str) -> str | None:
    """Normalise une adresse, et refuse tout ce qui n'est pas publiquement vu.

    Un service d'echo joignable depuis Internet ne peut pas voir une adresse
    privee. Si c'est ce qu'on recoit, c'est un portail captif ou un mandataire
    qui a repondu a sa place : une mesure manquante vaut mieux qu'une mesure
    fausse, qui serait ici prise pour une adresse « differente de la maison ».
    """
    try:
        address = ipaddress.ip_address(raw.strip())
    except ValueError:
        return None
    if address.is_private or address.is_loopback or address.is_link_local:
        return None
    return str(address)


def _first_field(payload: dict[str, object], fields: tuple[str, ...]) -> str | None:
    for field in fields:
        value = payload.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _parse_echo(
    service: EchoService, response: httpx.Response
) -> tuple[str, str | None, str | None] | None:
    """Extrait (adresse, organisation, ASN), ou rien si la reponse ne dit rien."""
    if not service.ip_field:
        # Reponse en clair, et bornee : une page d'erreur HTML arrive avec un
        # code 200 aussi souvent qu'une adresse.
        address = _public_ip(response.text[:64])
        return (address, None, None) if address else None

    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None

    raw = payload.get(service.ip_field)
    if not isinstance(raw, str):
        return None
    address = _public_ip(raw)
    if address is None:
        return None

    organization = _first_field(payload, service.org_fields)
    asn = _first_field(payload, service.asn_fields)
    if organization and asn is None:
        # ipinfo colle l'ASN devant le nom : « AS20473 Mullvad VPN AB ».
        matched = _AS_PREFIX.match(organization)
        if matched:
            asn, organization = matched.group(1), matched.group(2)
    return address, organization, asn


async def _measure_now(client: httpx.AsyncClient | None) -> Measure:
    interfaces, readable = read_tunnel_interfaces()
    owned = client is None
    # Aucune redirection suivie : ces URL sont fixes et connues, et une
    # redirection menerait la mesure — donc l'adresse — ailleurs que prevu.
    client = client or httpx.AsyncClient(timeout=TIMEOUT, follow_redirects=False)
    failures: list[str] = []
    try:
        for service in ECHO_SERVICES:
            try:
                response = await client.get(service.url)
            except httpx.HTTPError as exc:
                failures.append(f"{service.name} ({exc.__class__.__name__})")
                continue
            if response.status_code != 200:
                failures.append(f"{service.name} (HTTP {response.status_code})")
                continue
            parsed = _parse_echo(service, response)
            if parsed is None:
                failures.append(f"{service.name} (réponse inexploitable)")
                continue
            address, organization, asn = parsed
            return Measure(
                public_ip=address,
                organization=organization,
                asn=asn,
                echo_service=service.name,
                interfaces=interfaces,
                interfaces_readable=readable,
                at=time.monotonic(),
            )
    finally:
        if owned:
            await client.aclose()

    detail = " ; ".join(failures) or "aucun service d'écho configuré"
    logger.warning("adresse publique non mesurable : %s", detail)
    return Measure(
        interfaces=interfaces,
        interfaces_readable=readable,
        error=detail,
        at=time.monotonic(),
    )


_cache: Measure | None = None
_lock: asyncio.Lock | None = None
_lock_loop: asyncio.AbstractEventLoop | None = None


def _get_lock() -> asyncio.Lock:
    """Un verrou attache a la boucle courante.

    Un ``asyncio.Lock`` construit a l'import se lie a la premiere boucle qui
    l'utilise et leve ensuite des qu'une autre s'en sert — ce qui arrive au
    redemarrage d'un serveur comme entre deux tests. On le reconstruit plutot
    que de laisser une exception surgir loin de sa cause.
    """
    global _lock, _lock_loop
    loop = asyncio.get_running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock, _lock_loop = asyncio.Lock(), loop
    return _lock


def reset_cache() -> None:
    """Oublie la derniere mesure — apres un changement de reglage, ou en test."""
    global _cache, _lock, _lock_loop
    _cache = _lock = _lock_loop = None


async def measure_egress(
    *, client: httpx.AsyncClient | None = None, force: bool = False
) -> Measure:
    """Mesure la sortie, ou rend la derniere mesure encore valide.

    Le verrou ne sert pas qu'a proteger le cache : il regroupe les appels
    concurrents. Huit fournisseurs d'IA qui demarrent ensemble trouveraient un
    cache encore vide et emettraient huit requetes d'echo — la rafale exacte
    que le cache existe pour eviter.
    """
    global _cache
    async with _get_lock():
        cached = _cache
        if not force and cached is not None and time.monotonic() - cached.at < CACHE_TTL:
            return cached
        _cache = await _measure_now(client)
        return _cache


# --- Verdict -----------------------------------------------------------------


def _same_address(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return ipaddress.ip_address(left.strip()) == ipaddress.ip_address(right.strip())
    except ValueError:
        return left.strip() == right.strip()


def _tunnel_sentence(measure: Measure) -> str:
    if not measure.interfaces_readable:
        return (
            "Les interfaces réseau de ce système ne sont pas lisibles : "
            "impossible de dire si un tunnel existe."
        )
    if measure.interfaces:
        return f"Interface(s) de tunnel présente(s) : {', '.join(measure.interfaces)}."
    return (
        "Aucune interface de tunnel visible depuis le conteneur — "
        "ce qui est normal si le VPN est porté par le routeur."
    )


def evaluate(measure: Measure, reference_ip: str | None = None) -> VpnStatus:
    """Transforme une mesure en verdict, sans jamais combler un trou.

    L'ordre des questions n'est pas indifferent. Une mesure qui a echoue est
    traitee AVANT tout le reste : sans cela, l'absence d'adresse ressemblerait
    a une adresse qui ne correspond pas a la reference, et une panne reseau se
    lirait comme une preuve de protection.
    """
    tunnel = bool(measure.interfaces) if measure.interfaces_readable else None
    by_interface = provider_from_interfaces(measure.interfaces)
    by_organization = provider_from_organization(measure.organization)
    # Le nom d'interface renseigne le fournisseur, jamais la sortie : « wg0 »
    # peut exister pendant que la route par defaut l'ignore.
    provider = by_organization or by_interface
    reference = (reference_ip or "").strip() or None

    common = {
        "tunnel_present": tunnel,
        "tunnel_interfaces": measure.interfaces,
        "public_ip": measure.public_ip,
        "organization": measure.organization,
        "provider": provider,
        "echo_service": measure.echo_service,
        "error": measure.error,
    }

    if not measure.succeeded:
        return VpnStatus(
            verdict=Verdict.UNAVAILABLE,
            explanation=(
                f"Impossible de mesurer l'adresse publique ({measure.error}). "
                "Ce n'est pas une absence de VPN, c'est une vérification qui n'a "
                f"pas abouti. {_tunnel_sentence(measure)}"
            ),
            measured=False,
            egress_confirmed=None,
            **common,
        )

    if _same_address(measure.public_ip, reference):
        return VpnStatus(
            verdict=Verdict.EXPOSED,
            explanation=(
                f"L'adresse publique {measure.public_ip} est celle enregistrée hors "
                f"VPN : le trafic sort par la connexion domestique. "
                f"{_tunnel_sentence(measure)}"
            ),
            measured=True,
            egress_confirmed=False,
            **common,
        )

    if by_organization:
        return VpnStatus(
            verdict=Verdict.PROTECTED,
            explanation=(
                f"Le trafic sort par {by_organization} "
                f"(adresse {measure.public_ip}, {measure.organization}). "
                f"{_tunnel_sentence(measure)}"
            ),
            measured=True,
            egress_confirmed=True,
            **common,
        )

    if reference:
        nomme = ""
        if measure.organization:
            nomme = f" Fournisseur non identifié ({measure.organization})."
        return VpnStatus(
            verdict=Verdict.PROTECTED,
            explanation=(
                f"L'adresse publique {measure.public_ip} diffère de l'adresse "
                f"enregistrée hors VPN ({reference}) : le trafic ne sort pas par la "
                f"connexion domestique.{nomme} {_tunnel_sentence(measure)}"
            ),
            measured=True,
            egress_confirmed=True,
            **common,
        )

    return VpnStatus(
        verdict=Verdict.UNKNOWN,
        explanation=(
            f"Adresse publique mesurée ({measure.public_ip}, "
            f"{measure.organization or 'organisation inconnue'}), mais rien ne permet "
            "de dire par où elle sort. Enregistre ton adresse publique hors VPN pour "
            f"rendre la comparaison possible. {_tunnel_sentence(measure)}"
        ),
        measured=True,
        egress_confirmed=None,
        **common,
    )


async def check(
    *,
    reference_ip: str | None = None,
    client: httpx.AsyncClient | None = None,
    force: bool = False,
) -> VpnStatus:
    """Mesure puis juge. C'est ce qu'appelle l'interface pour afficher l'etat."""
    return evaluate(await measure_egress(client=client, force=force), reference_ip)


async def egress_allowed(
    policy: Policy | str,
    *,
    reference_ip: str | None = None,
    client: httpx.AsyncClient | None = None,
    force: bool = False,
) -> Decision:
    """Ai-je le droit d'emettre du trafic maintenant ?

    Le mode « exiger » refuse aussi quand la mesure a echoue, et c'est le point
    entier de ce module : une politique qui s'ouvre au moindre doute ne protege
    rien, puisqu'il suffit de faire tomber la verification pour la contourner.
    Un refus est reparable — on relance ; une fuite ne l'est pas.
    """
    mode = Policy(policy)

    if mode is Policy.FREE:
        # Aucune mesure : ce mode n'exige rien, et emettre une requete d'echo
        # pour l'annoncer ajouterait le trafic qu'il ne demande pas.
        return Decision(allowed=True, warn=False, reason="Aucune exigence de sortie par VPN.")

    status = await check(reference_ip=reference_ip, client=client, force=force)
    confirmed = status.verdict is Verdict.PROTECTED

    if mode is Policy.WARN:
        return Decision(allowed=True, warn=not confirmed, reason=status.explanation, status=status)

    if confirmed:
        return Decision(allowed=True, warn=False, reason=status.explanation, status=status)
    return Decision(
        allowed=False,
        warn=True,
        reason=(
            f"Émission refusée : la sortie par le tunnel n'est pas confirmée. {status.explanation}"
        ),
        status=status,
    )
