"""Isolation des tests vis-a-vis de l'environnement local.

Sans ceci, un ``.env`` de developpement present a la racine ferait passer ou
echouer les tests selon la machine — et la CI divergerait du poste du
developpeur.

Le dossier de donnees est detourne pour la meme raison, et pour une seconde
plus grave : ``api/deps.py`` le designe par un chemin RELATIF (« data/ »), donc
une suite lancee depuis la racine du depot lisait et ECRIVAIT le journal, la
base et les preferences de l'utilisateur. Les deux effets etaient mauvais — des
entrees de test dans un vrai journal d'annulation, et des tests dont le
resultat dependait des reglages du poste. Depuis que la cle TheMovieDB vit dans
les preferences, le second est devenu franchement piegeux : renseigner sa cle
depuis l'interface faisait echouer des tests sans aucun rapport.
"""

import os
from pathlib import Path

import pytest

from sortilege.api import deps
from sortilege.config import get_settings

_TEST_ENV = {
    "SORTILEGE_SOURCE_ROOTS": "/tmp/sortilege-test/downloads",
    "SORTILEGE_LIBRARY_ROOT": "/tmp/sortilege-test/media",
    "SORTILEGE_SECRET_KEY": "k" * 48,
    "SORTILEGE_ADMIN_PASSWORD": "mot-de-passe-de-test",
}


@pytest.fixture(autouse=True, scope="session")
def _environnement_de_test(tmp_path_factory: pytest.TempPathFactory) -> None:
    for key, value in _TEST_ENV.items():
        os.environ[key] = value
    # get_settings est mis en cache : il faut la vider apres avoir pose l'env.
    get_settings.cache_clear()

    donnees: Path = tmp_path_factory.mktemp("data")
    deps.DATA_DIR = donnees
    # Les trois accesseurs sont memoises et capturent le chemin a la premiere
    # construction : vider les caches AVANT que quoi que ce soit ne les appelle
    # est ce qui rend le detournement effectif.
    deps.get_journal.cache_clear()
    deps.get_memory.cache_clear()
    deps.get_store.cache_clear()
