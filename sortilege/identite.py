"""Choix de l'identite sous laquelle le conteneur va tourner.

Deplacer ou supprimer un fichier n'exige aucun droit sur le fichier : cela
exige le droit d'ecrire sur le DOSSIER qui le contient. C'est cette regle Unix
qui fait echouer Sortilege sur un NAS ou le client de telechargement cree ses
dossiers en 999:999 drwxr-xr-x pendant que l'application tourne en 1000:1000 —
aucune astuce logicielle ne la contourne, seule l'identite du processus compte.

Ce module regarde les dossiers reellement concernes, dit qui les possede, et
retient l'identite capable d'ecrire PARTOUT. Quand deux dossiers appartiennent a
deux comptes differents sans droit d'ecriture pour le groupe, cette identite
n'existe pas en dehors de root : il le dit et le justifie, au lieu de laisser
decouvrir un « Permission denied » au premier rangement, une fois le fichier
a moitie deplace.

Une seule chose sort sur stdout : « uid:gid ». Tout le reste part sur stderr —
l'entrypoint consomme la premiere, l'humain lit le second.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import Settings, get_settings

ROOT = (0, 0)
"""La seule identite qui traverse tout, faute de mieux."""

ECRITURE_OK = "accessible en écriture"
ECRITURE_KO = "ÉCRITURE IMPOSSIBLE"
"""Le verdict d'une racine, en deux formulations et pas plus.

Le meme journal portait deux vocabulaires pour la meme chose : « ÉCRITURE
IMPOSSIBLE » ici, « ECRITURE REFUSEE » dans ``main``. Deux lexiques pour un
seul fait donnent l'impression de deux verdicts differents ; ces constantes sont
partagees avec ``main``, qui affiche le meme diagnostic une fois l'identite
prise.
"""

DELAI_STAT = 3.0
"""Secondes accordees au ``stat`` d'une racine avant de la declarer muette.

Un montage NFS ou SMB dont le serveur ne repond plus ne rend pas d'erreur : par
defaut (montage « hard ») le ``stat`` attend, eventuellement pour toujours. Or
ce module tourne AVANT le demarrage de l'application, et l'entrypoint attend sa
reponse pour faire son ``exec`` : sans delai de garde, un seul montage fige
empeche le conteneur de demarrer — y compris l'interface qui permettrait de
corriger le montage.

Trois secondes : assez pour un NAS qui sort de veille, trop peu pour qu'on
croie a un blocage, et paye une fois par racine, au demarrage seulement.
"""


class MontageFige(OSError):
    """Un ``stat`` qui n'a pas rendu la main dans le delai accorde.

    Une ``OSError`` a dessein : une racine muette est une racine inutilisable,
    et tout ce qui rattrape deja les refus du systeme de fichiers traite ce cas
    sans avoir a connaitre cette classe.
    """


def stat_avec_delai(chemin: Path, delai: float | None = None) -> os.stat_result:
    """``os.stat``, mais qui rend la main. Leve ``MontageFige`` au-dela du delai.

    Le releve part dans un fil et on ne l'attend pas plus que ``delai``. Le fil
    est un demon : reste-t-il bloque dans l'appel systeme, il n'empeche ni la
    suite ni la sortie de l'interpreteur, et le noyau le libere avec le
    processus. C'est le seul moyen d'abandonner un appel systeme qu'on ne peut
    pas interrompre.

    Pas de ``signal.alarm`` : un signal n'arrive que sur le fil principal,
    l'application est multi-fils, et poser un gestionnaire global pour un
    renseignement de confort deborderait de ce module.

    ``delai`` vaut ``DELAI_STAT`` quand on ne le precise pas — resolu a l'appel
    et non a la definition, pour qu'un test puisse abaisser la constante sans
    attendre trois secondes par racine.
    """
    if delai is None:
        delai = DELAI_STAT
    releves: list[os.stat_result | OSError] = []

    def releve() -> None:
        try:
            releves.append(os.stat(chemin))
        except OSError as exc:
            releves.append(exc)

    fil = threading.Thread(target=releve, daemon=True, name=f"stat {chemin}")
    fil.start()
    fil.join(delai)

    # La liste plutot que ``fil.is_alive()`` : une reponse arrivee juste apres
    # l'expiration du delai est une reponse, autant la lire.
    if not releves:
        raise MontageFige(
            errno.ETIMEDOUT,
            f"n'a pas répondu en {delai:g} s (montage figé ?), ignorée",
            str(chemin),
        )
    premier = releves[0]
    if isinstance(premier, OSError):
        raise premier
    return premier


DEFAUT = (1000, 1000)
"""Identite historique du conteneur.

Elle sert de point de depart meme en mode automatique : quand elle convient
partout, il n'y a aucune raison d'en changer, et une mise a jour d'image ne doit
pas se mettre a ecrire sous une autre identite dans le dos de l'utilisateur.
"""

FICHIER_PREFERENCES = Path("data/preferences.json")
"""Meme emplacement que ``api/deps.DATA_DIR``, relatif comme lui (WORKDIR /app).

Relu ici en JSON brut plutot que par ``PreferenceStore`` : le magasin importe
tout le module des preferences — clients reseau, validation, journalisation —
et en construire un avant le demarrage de l'application, pour une liste de
chaines, couterait plus cher que le renseignement obtenu. On ne lit que
``custom_sources`` ; un fichier absent ou casse ne vaut pas un mot, la
configuration suffit alors.
"""


@dataclass(frozen=True, slots=True)
class Racine:
    """Un dossier dans lequel Sortilege doit pouvoir ecrire, et ce qu'il en sait."""

    chemin: Path
    uid: int = -1
    gid: int = -1
    mode: int = 0

    ecartee: str = ""
    """Non vide quand la racine ne compte pas dans la decision — absente,
    illisible, muette, ou pas un dossier. Le texte est le motif, affiche tel
    quel.

    Une racine absente est IGNOREE et non bloquante : une source demontee ou une
    bibliotheque pas encore creee ne doit pas faire basculer tout le conteneur
    en root pour un dossier dont on ne sait rien."""

    @property
    def proprietaire(self) -> str:
        return f"{self.uid}:{self.gid}"


@dataclass(frozen=True, slots=True)
class Choix:
    """L'identite retenue et la phrase qui l'explique."""

    uid: int
    gid: int
    raison: str

    @property
    def texte(self) -> str:
        """Ce qui part sur stdout, et rien d'autre."""
        return f"{self.uid}:{self.gid}"


def lire_racine(chemin: Path) -> Racine:
    """Proprietaire, groupe et mode d'une racine. Ne leve jamais.

    Tout ce qui empeche de conclure — dossier absent, illisible, muet, fichier
    au lieu d'un dossier — devient un motif d'ecartement plutot qu'une
    exception : ce module tourne avant le demarrage de l'application, il n'a pas
    le droit de faire echouer le conteneur pour un chemin mal saisi ni pour un
    serveur de fichiers en panne.
    """
    try:
        infos = stat_avec_delai(chemin)
    except MontageFige as exc:
        # Le motif est deja redige : il nomme le delai, ce qu'aucune formule
        # generique ne dirait, et « montage figé ? » est la seule piste utile.
        return Racine(chemin, ecartee=str(exc.strerror))
    except OSError as exc:
        return Racine(chemin, ecartee=f"absente ou illisible ({exc.strerror}), ignorée")
    if not stat.S_ISDIR(infos.st_mode):
        return Racine(chemin, ecartee="ce n'est pas un dossier, ignoré")
    return Racine(chemin, uid=infos.st_uid, gid=infos.st_gid, mode=infos.st_mode)


def sources_ajoutees(fichier: Path = FICHIER_PREFERENCES) -> list[Path]:
    """Sources declarees depuis l'interface, si le fichier se lit sans effort.

    Elles comptent autant que les racines montees : une source ajoutee est un
    dossier qu'on videra, donc un dossier ou il faut ecrire. Silence complet en
    cas de probleme — ce fichier peut ne pas exister encore, et ce n'est pas ici
    qu'on diagnostique des preferences illisibles.
    """
    try:
        brut = json.loads(fichier.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(brut, dict):
        return []
    valeurs = brut.get("custom_sources")
    if not isinstance(valeurs, list):
        return []
    return [Path(v) for v in valeurs if isinstance(v, str) and v.startswith("/")]


def chemins_declares(reglages: Settings | None = None) -> list[Path]:
    """Les dossiers concernes, dedoublonnes et dans l'ordre de declaration.

    La bibliotheque ET les sources : un rangement ecrit dans la premiere et
    retire de la seconde. Les deux doivent passer — et c'est precisement quand
    elles appartiennent a deux comptes differents que le probleme apparait. Les
    sources AJOUTEES depuis l'interface comptent autant : ce sont elles qui
    peuvent faire basculer le conteneur en root, il serait absurde de les
    verifier au demarrage du conteneur et pas dans le diagnostic de
    l'application.

    Le dedoublonnage n'est pas cosmetique : ``library_root`` figure souvent
    aussi dans ``source_roots``, et le diagnostic affichait alors deux fois la
    meme ligne.

    ``reglages`` evite un second appel a ``get_settings`` quand l'appelant les
    a deja — et permet d'en fournir d'autres, ce dont les tests ont besoin.
    """
    if reglages is None:
        try:
            reglages = get_settings()
        except Exception as exc:
            # Une configuration invalide arretera l'application avec un vrai
            # message ; ici elle ne doit pas se transformer en trace de pile
            # illisible juste avant, ni empecher l'entrypoint de continuer.
            print(
                f"Sortilège : configuration illisible ({exc}), identité inchangée.",
                file=sys.stderr,
            )
            return []

    chemins: dict[str, Path] = {}
    for chemin in [reglages.library_root, *reglages.source_roots, *sources_ajoutees()]:
        chemins.setdefault(str(chemin), chemin)
    return list(chemins.values())


def peut_ecrire(racine: Racine, uid: int, gid: int) -> bool:
    """Cette identite peut-elle creer et supprimer dans cette racine ?

    Les trois classes de droits ne s'additionnent pas : Unix retient la
    PREMIERE qui correspond. Proprietaire d'un dossier en drwxr-xr-x, on
    n'herite pas des droits des « autres » — c'est exactement ce qui surprend.
    Il faut w ET x : sans x on ne traverse pas le dossier, et un rename() y
    echoue malgre le droit d'ecriture.

    Limite assumee : les groupes SECONDAIRES de l'identite ne sont pas connus
    ici. Le conteneur ne recoit qu'un couple de nombres, aucun /etc/group n'est
    consulte. Une identite membre d'un autre groupe du NAS peut donc ecrire la
    ou ce calcul dit le contraire — jamais l'inverse, ce qui est le bon sens de
    l'erreur : on retient plus large, pas plus permissif.
    """
    if uid == 0:
        return True
    if uid == racine.uid:
        return bool(racine.mode & stat.S_IWUSR and racine.mode & stat.S_IXUSR)
    if gid == racine.gid:
        return bool(racine.mode & stat.S_IWGRP and racine.mode & stat.S_IXGRP)
    return bool(racine.mode & stat.S_IWOTH and racine.mode & stat.S_IXOTH)


def motif_refus(racine: Racine, uid: int, gid: int) -> str:
    """La phrase qui dit pourquoi cette racine resiste a cette identite."""
    if uid == racine.uid:
        return "elle en est propriétaire, mais le mode ne donne pas l'écriture au propriétaire"
    if gid == racine.gid:
        return "elle est dans le groupe, mais le mode ne donne pas l'écriture au groupe"
    return (
        f"le dossier appartient à {racine.proprietaire}, l'identité n'en est "
        "ni propriétaire ni du groupe, et les « autres » n'ont pas l'écriture"
    )


def _ecrit_partout(racines: list[Racine], uid: int, gid: int) -> bool:
    return all(peut_ecrire(r, uid, gid) for r in racines)


def _gid_le_plus_large(racines: list[Racine], gids: list[int]) -> int:
    """Parmi ``gids``, celui qui ouvre vraiment une porte. A defaut, le premier.

    Sert quand plusieurs candidates ne se distinguent que par leur groupe :
    l'uid etant le meme, le groupe retenu ne change rien au droit d'ecrire
    aujourd'hui, mais il decide du groupe que porteront les fichiers deposes.

    Le critere est donc : le groupe d'une racine qui accorde l'ecriture AU
    GROUPE (``g+wx``) vaut mieux qu'un groupe decoratif — il continuera de
    passer si le proprietaire d'une racine change, alors que l'autre ne tenait
    que par le proprietaire. A egalite, l'ordre de declaration tranche, donc la
    bibliotheque d'abord : c'est elle qu'un serveur multimedia relit.
    """
    for racine in racines:
        if racine.gid in gids and racine.mode & stat.S_IWGRP and racine.mode & stat.S_IXGRP:
            return racine.gid
    return gids[0]


def choisir(racines: list[Racine], uid: int, gid: int) -> Choix:
    """Identite retenue pour ``uid:gid`` demande, et pourquoi.

    Quatre issues, dans cet ordre :

    1. l'identite demandee ecrit partout — on n'y touche pas ;
    2. une seule des identites PROPRIETAIRES des racines ecrit partout — on la
       propose, elle existe deja sur le NAS ;
    3. plusieurs conviennent mais toutes portent le MEME uid — c'est cet uid,
       le groupe n'etant plus qu'un detail (voir ``_gid_le_plus_large``) ;
    4. root, parce qu'il ne reste rien d'autre.

    On n'invente pas d'identite composite (l'uid de l'une, le gid de l'autre) :
    elle fonctionnerait parfois, mais les fichiers deposes appartiendraient
    alors a un couple qui n'existe nulle part sur le NAS, et personne ne saurait
    d'ou il sort.

    L'ambiguite, en revanche, ne justifie root que si elle porte sur l'UID :
    c'est lui qui designe le compte qui possedera la bibliotheque, et on ne le
    tire pas au sort. Quand ``/dl`` est a 999:999 et ``/media`` a 999:1000, les
    deux candidates disent la meme chose — l'utilisateur 999 ecrit des deux
    cotes — et basculer tout le conteneur en root pour un desaccord de groupe
    serait une punition sans rapport avec le probleme.
    """
    comptent = [r for r in racines if not r.ecartee]
    if not comptent:
        return Choix(uid, gid, "aucune racine existante à vérifier, identité demandée gardée")

    if _ecrit_partout(comptent, uid, gid):
        return Choix(uid, gid, "elle peut écrire dans toutes les racines")

    candidates: list[tuple[int, int]] = []
    for racine in comptent:
        couple = (racine.uid, racine.gid)
        if couple not in (ROOT, (uid, gid)) and couple not in candidates:
            candidates.append(couple)
    retenues = [c for c in candidates if _ecrit_partout(comptent, *c)]

    if len(retenues) == 1:
        choisie = retenues[0]
        return Choix(
            *choisie,
            f"{uid}:{gid} ne peut pas écrire partout ; {choisie[0]}:{choisie[1]}, "
            "propriétaire sur le NAS, le peut",
        )
    if len(retenues) > 1:
        liste = ", ".join(f"{u}:{g}" for u, g in retenues)
        uids = {u for u, _ in retenues}
        if len(uids) == 1:
            uid_commun = uids.pop()
            gid_retenu = _gid_le_plus_large(comptent, [g for _, g in retenues])
            return Choix(
                uid_commun,
                gid_retenu,
                f"{uid}:{gid} ne peut pas écrire partout ; {uid_commun} le peut dans "
                f"toutes les racines, seul le groupe variait ({liste}) — "
                f"{uid_commun}:{gid_retenu} retenu plutôt que root",
            )
        return Choix(
            *ROOT,
            f"plusieurs identités conviendraient ({liste}) et aucune n'est plus légitime "
            "qu'une autre : fixe PUID/PGID sur celle que tu veux voir posséder tes fichiers",
        )
    return Choix(*ROOT, "aucune identité non-root ne peut écrire dans toutes les racines")


def lignes_diagnostic(
    racines: list[Racine],
    uid: int,
    gid: int,
    raison: str,
    demandee: tuple[int, int] | None = None,
) -> list[str]:
    """Le diagnostic affiche au demarrage : une ligne par racine, rien de plus.

    Chaque ligne porte le chemin, son proprietaire, son mode et le verdict pour
    l'identite RETENUE. C'est ce tableau qui manquait : jusqu'ici, un
    « Permission denied » arrivait des heures plus tard, sur un fichier, sans
    jamais nommer le dossier fautif ni son proprietaire.

    ``demandee`` ajoute, quand elle a ete ecartee, la phrase qui dit pour chaque
    racine ce qui lui resistait. Sans elle, un basculement en root ressemblerait
    a un caprice : toutes les lignes diraient « accessible en écriture », root
    pouvant tout, et rien ne montrerait le dossier a l'origine du choix.
    """
    qui = f"{uid}:{gid}" + (" (root)" if uid == 0 else "")
    lignes = [f"Sortilège — identité retenue : {qui} — {raison}"]
    if not racines:
        lignes.append("  aucune racine déclarée : rien à vérifier.")
        return lignes

    largeur = max(len(str(r.chemin)) for r in racines)
    largeur_id = max((len(r.proprietaire) for r in racines if not r.ecartee), default=0)

    refusee = False
    par_les_autres = False
    for racine in racines:
        chemin = f"{racine.chemin!s:<{largeur}}"
        if racine.ecartee:
            lignes.append(f"  {chemin}  {racine.ecartee}")
            continue
        if peut_ecrire(racine, uid, gid):
            verdict = ECRITURE_OK
        else:
            verdict = f"{ECRITURE_KO} — {motif_refus(racine, uid, gid)}"
            refusee = True
            par_les_autres = par_les_autres or uid not in (0, racine.uid)
        proprietaire = f"{racine.proprietaire:<{largeur_id}}"
        lignes.append(f"  {chemin}  {proprietaire}  {stat.filemode(racine.mode)}  {verdict}")

    if demandee is not None and demandee != (uid, gid):
        bloquantes = [r for r in racines if not r.ecartee and not peut_ecrire(r, *demandee)]
        if bloquantes:
            lignes.append(f"  Pourquoi pas {demandee[0]}:{demandee[1]} :")
            for racine in bloquantes:
                lignes.append(f"    {racine.chemin} : {motif_refus(racine, *demandee)}")
                par_les_autres = par_les_autres or demandee[0] not in (0, racine.uid)

    ecartees = sum(1 for racine in racines if racine.ecartee)
    if ecartees:
        # Une racine ecartee ne compte pas dans la decision : l'identite retenue
        # peut donc etre une identite ordinaire alors que root aurait ete
        # necessaire si la racine manquante avait ete la. Le dire ici evite de
        # le decouvrir en « Permission denied » au premier rangement, une fois
        # le volume enfin monte.
        lignes.append(
            f"  {ecartees} racine(s) ignorée(s) : si elles apparaissent plus tard, "
            "relance le conteneur."
        )
    if refusee and uid != 0:
        # N'arrive qu'avec une identite IMPOSEE : en mode automatique, celle qui
        # est retenue ecrit partout, ou c'est root.
        lignes.append(
            "  Le rangement échouera sur ces dossiers. PUID=auto laisse Sortilège "
            "retenir l'identité qui écrit partout — root si aucune autre ne passe."
        )
    if par_les_autres:
        lignes.append(
            "  Note : les groupes secondaires ne sont pas connus dans un conteneur — "
            "une identité membre d'un autre groupe du NAS peut écrire malgré ce verdict."
        )
    if uid == 0:
        lignes.append(
            "  root traverse tout : c'est la seule identité qui passe quand deux dossiers "
            "appartiennent à deux comptes différents. Compromis assumé — voir le README."
        )
    return lignes


def _entier(valeur: str, defaut: int) -> int:
    """Des chiffres ASCII, et rien d'autre -> l'entier. Tout le reste -> le defaut.

    Le controle est plus strict que ``int()``, qui acceptait bien plus que ce
    que cette fonction promettait : « 1_0 » valait 10, « 1000 » entoure
    d'espaces passait, et les chiffres arabes (U+0660 a U+0669) valaient les
    notres. Un PUID est saisi a la main dans un
    ``.env`` et decide sous quelle identite tourne tout le conteneur : mieux
    vaut retomber sur le defaut — visible dans le diagnostic — que demarrer sous
    un nombre que personne n'a voulu ecrire. Un negatif n'a plus besoin d'etre
    ecarte a part : le signe n'est pas un chiffre.
    """
    if not (valeur.isascii() and valeur.isdigit()):
        return defaut
    return int(valeur)


def identite_demandee(argument: str) -> tuple[int, int]:
    """« 1000:1000 » -> (1000, 1000). Tout ce qui ne se lit pas -> le defaut.

    Pas d'erreur sur une saisie fautive : PUID est rempli a la main dans un
    .env, et refuser de demarrer pour un « 100O » serait une punition sans
    rapport avec ce que fait l'application.
    """
    uid, _, gid = argument.partition(":")
    return _entier(uid, DEFAUT[0]), _entier(gid, DEFAUT[1])


def main(argv: list[str] | None = None) -> int:
    """Point d'entree de ``python -m sortilege.identite``.

    ``--impose`` : rendre l'identite demandee telle quelle, sans jamais en
    changer, mais en publiant quand meme le diagnostic. C'est le mode du
    demarrage ordinaire (PUID fixe) : l'utilisateur garde la main, et lit
    pourtant ce qui coince avant que le premier rangement n'echoue.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    impose = "--impose" in arguments
    positionnels = [a for a in arguments if not a.startswith("--")]

    demande = identite_demandee(
        positionnels[0]
        if positionnels
        else f"{os.environ.get('PUID', '')}:{os.environ.get('PGID', '')}"
    )

    racines = [lire_racine(chemin) for chemin in chemins_declares()]
    if impose:
        choix = Choix(*demande, "identité imposée par PUID/PGID")
    else:
        choix = choisir(racines, *demande)

    for ligne in lignes_diagnostic(racines, choix.uid, choix.gid, choix.raison, demande):
        print(ligne, file=sys.stderr)
    print(choix.texte)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
