"""Les adresses des blocages doivent nommer des ecrans qui existent.

Deux des cinq envoyaient chercher au mauvais endroit : « Réglages →
Métadonnées » (l'onglet s'appelle « Identification ») et « Médiathèque →
Analyser les sources » (le bouton est dans « Ranger », et « Ma médiathèque »
est l'autre onglet).

Le defaut vient de la distance : ces textes sont ecrits cote serveur, loin des
libelles de l'interface, et rien ne les reliait. Ce test tient la laisse — une
adresse fausse coute plus cher qu'une adresse absente, parce qu'elle fait
chercher avant de faire douter.
"""

from __future__ import annotations

import re

import pytest

from sortilege.api.review import blockers

# Les libelles de la barre de navigation, tels que `ui/src/App.vue` les ecrit.
ECRANS = ("Ranger", "Ma médiathèque", "Réencodage", "Journal", "Réglages")

# Les onglets de Reglages, tels que `SettingsView.vue` les ecrit.
ONGLETS_REGLAGES = ("Bibliothèque", "Automatisation", "Identification", "Système")


@pytest.fixture
def adresses() -> list[str]:
    """Toutes les adresses possibles, y compris celles qu'un etat sain masque.

    ``blockers()`` ne rend que les blocages du moment ; le test doit couvrir
    les cinq, pas les deux qui se trouvent actifs ici.
    """
    return [entree["where"] for entree in blockers()]


def test_chaque_blocage_actif_nomme_un_ecran(adresses: list[str]) -> None:
    for adresse in adresses:
        assert adresse.startswith(ECRANS), f"« {adresse} » ne commence par aucun écran"


def test_aucune_adresse_du_code_ne_nomme_un_ecran_inexistant() -> None:
    """Lit le module plutot que son execution : les cinq adresses sont ecrites
    en clair, et trois ne sortent que dans des etats qu'un test ne monte pas."""
    import inspect

    import sortilege.api.review as module

    source = inspect.getsource(module.blockers)
    # Les VALEURS d'adresse, pas tout ce qui contient une fleche : un
    # commentaire du corps en contenait une, et le test se trompait de cible.
    adresses = re.findall(r'"where": "([^"]+)"', source)
    adresses += re.findall(r'^\s*ou = "([^"]+)"', source, re.M)
    assert adresses, "aucune adresse lue : la regex ne correspond plus au code"
    for adresse in adresses:
        assert adresse.startswith(ECRANS), f"« {adresse} » ne commence par aucun écran"
        if adresse.startswith("Réglages → "):
            onglet = adresse.removeprefix("Réglages → ").split(" ")[0].split("(")[0]
            assert onglet.startswith(ONGLETS_REGLAGES), (
                f"« {adresse} » nomme un onglet de Réglages qui n'existe pas"
            )
