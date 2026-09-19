"""Outils de diagnostic lances a la main ou par la CI, jamais importes par l'application.

Ils vivent dans le paquet, et non dans ``tests/``, parce qu'ils doivent tourner
DANS l'image Docker publiee : ``tests/`` n'y est pas copie (voir .dockerignore),
``sortilege/`` l'est.
"""
