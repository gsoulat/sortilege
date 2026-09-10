"""Isolation des tests vis-a-vis de l'environnement local.

Sans ceci, un ``.env`` de developpement present a la racine ferait passer ou
echouer les tests selon la machine — et la CI divergerait du poste du
developpeur.
"""

import os

import pytest

from sortilege.config import get_settings

_TEST_ENV = {
    "SORTILEGE_SOURCE_ROOTS": "/tmp/sortilege-test/downloads",
    "SORTILEGE_LIBRARY_ROOT": "/tmp/sortilege-test/media",
    "SORTILEGE_SECRET_KEY": "k" * 48,
    "SORTILEGE_ADMIN_PASSWORD": "mot-de-passe-de-test",
}


@pytest.fixture(autouse=True, scope="session")
def _environnement_de_test() -> None:
    for key, value in _TEST_ENV.items():
        os.environ[key] = value
    # get_settings est mis en cache : il faut la vider apres avoir pose l'env.
    get_settings.cache_clear()
