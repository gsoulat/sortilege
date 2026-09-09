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
