#!/bin/sh
# Ajuste l'identite AU DEMARRAGE, puis abandonne les droits root.
#
# Pourquoi ce detour plutot qu'un simple USER dans le Dockerfile : l'identite
# doit correspondre au proprietaire des fichiers a deplacer, et ce proprietaire
# n'est connu que de l'utilisateur, sur SA machine. Un UID fige a la
# construction ne vaut que pour qui reconstruit l'image lui-meme ; quiconque
# tire l'image publiee heritait de 1000:1000 et se heurtait a « Permission
# denied » sur des fichiers ecrits par son client de telechargement.
#
# C'est ainsi que procedent les images de Radarr, Sonarr et consorts, et c'est
# la seule facon de faire fonctionner PUID/PGID sur une image partagee.
#
# Le conteneur demarre donc root et le reste le temps de deux commandes. Le
# compromis est assume : sans cela, PUID/PGID sont un reglage decoratif.

set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

# --- Diagnostic et choix de l'identite ---------------------------------------
#
# Deplacer ou supprimer un fichier exige le droit d'ecrire sur le DOSSIER qui le
# contient, pas sur le fichier. Quand les telechargements arrivent en 999:999
# drwxr-xr-x et que la bibliotheque appartient a 1000:1000, aucune identite
# non-root n'ecrit dans les deux : c'est une regle du noyau, aucune astuce
# logicielle ne la contourne.
#
# PUID=auto delegue le choix a sortilege.identite, qui regarde les dossiers
# reellement montes et retient l'identite qui passe partout — quitte a conclure
# root, en le disant. Sans PUID, rien ne change : 1000:1000 comme avant.
#
# Le diagnostic est imprime DANS TOUS LES CAS. Il tient en quelques lignes et
# remplace le « Permission denied » qui arrivait des heures plus tard, sur un
# fichier, sans jamais nommer le dossier fautif ni son proprietaire.

AUTO=0
if [ "$PUID" = "auto" ] || [ "$PGID" = "auto" ]; then
    AUTO=1
fi
# L'identite historique sert de point de depart au mode automatique : quand elle
# convient, elle est gardee, et une mise a jour d'image ne se met pas a ecrire
# sous une autre identite dans le dos de l'utilisateur.
if [ "$PUID" = "auto" ]; then
    PUID=1000
fi
if [ "$PGID" = "auto" ]; then
    PGID=1000
fi

# Le module ne rend QUE « uid:gid » sur sa sortie standard ; le diagnostic part
# sur l'erreur standard, d'ou ce filtrage. Une panne du module ne doit rien
# empecher : on garde alors PUID/PGID tels quels plutot que de refuser de
# demarrer pour un renseignement de confort.
#
# En mode IMPOSE, sa sortie standard n'est meme pas lue : PUID/PGID explicites
# sont la loi, et les remplacer par ce que rend le module serait inoffensif
# aujourd'hui (il rend ce qu'on lui donne) mais silencieux le jour ou il aurait
# un bug. Le module n'est alors appele que pour PUBLIER son diagnostic.
CHOIX=""
if command -v python >/dev/null 2>&1; then
    if [ "$AUTO" -eq 1 ]; then
        CHOIX="$(python -m sortilege.identite "${PUID}:${PGID}" || true)"
    else
        python -m sortilege.identite --impose "${PUID}:${PGID}" >/dev/null || true
    fi
fi

# Validation ANCREE de la sortie, et seulement en mode automatique. Le motif
# « [0-9]*:[0-9]* » d'avant n'etait ancre a aucun bout : il acceptait
# « 1:2 du texte en trop », et une deuxieme ligne de bruit passait avec la
# premiere. gosu recevait alors une identite invalide et le conteneur mourait au
# demarrage — exactement ce que ce garde-fou devait eviter.
#
# On n'applique jamais une identite qu'on n'a pas lue : une sortie vide ou
# inattendue (module absent, interpreteur casse) laisse PUID/PGID intacts.
if [ "$AUTO" -eq 1 ]; then
    case "$CHOIX" in
        # Vide ; un caractere qui n'est ni chiffre ni « : » — un saut de ligne
        # ou une espace en sont — ; deux « : » ; ou un cote vide.
        '' | *[!0-9:]* | *:*:* | :* | *:)
            echo "Sortilège : diagnostic d'identité illisible, on garde ${PUID}:${PGID}." >&2
            ;;
        *:*)
            PUID="${CHOIX%%:*}"
            PGID="${CHOIX##*:}"
            ;;
        *)
            # Des chiffres, mais pas de « : » : ce n'est pas une identite.
            echo "Sortilège : diagnostic d'identité illisible, on garde ${PUID}:${PGID}." >&2
            ;;
    esac
fi

# Le repertoire de donnees appartient au conteneur : journal d'annulation, base
# des decisions, preferences. Il est monte depuis l'hote et peut arriver avec
# n'importe quel proprietaire.
if [ -d /app/data ]; then
    chown -R "${PUID}:${PGID}" /app/data 2>/dev/null || true
fi

# gosu et non su : il n'ouvre pas de session, ne cree pas de processus
# intermediaire, et transmet correctement les signaux — un conteneur doit
# pouvoir s'arreter proprement.
#
# L'identite est donnee en NUMERIQUE : aucun compte n'a besoin d'exister dans
# /etc/passwd, ce qui evite d'avoir a le modifier et permet de garder le
# systeme de fichiers en lecture seule.
exec gosu "${PUID}:${PGID}" "$@"
