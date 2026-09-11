"""Tests des preferences modifiables.

L'enjeu principal est le confinement : l'interface devient un moyen d'ecrire
sur le disque, elle ne doit pas pouvoir designer n'importe quelle destination.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from sortilege.core.preferences import (
    DEFAULT_DESTINATIONS,
    AutomationSettings,
    NotificationSettings,
    PreferenceError,
    Preferences,
    PreferenceStore,
    SubtitleSettings,
)


@pytest.fixture
def store(tmp_path: Path) -> PreferenceStore:
    library = tmp_path / "media"
    library.mkdir()
    src_a = tmp_path / "dl-a"
    src_b = tmp_path / "dl-b"
    src_a.mkdir()
    src_b.mkdir()
    return PreferenceStore(
        path=tmp_path / "data" / "preferences.json",
        library_root=library,
        source_roots=[src_a, src_b],
    )


def test_defauts_sans_fichier(store: PreferenceStore) -> None:
    prefs = store.load()
    assert prefs.destinations == DEFAULT_DESTINATIONS
    assert prefs.enabled_sources == []


def test_aller_retour(store: PreferenceStore) -> None:
    """Relu depuis le DISQUE et non depuis le cache : c'est ce que fait un
    redemarrage, et c'est la seule chose qui prouve que l'ecriture a eu lieu."""
    store.save(Preferences(destinations={**DEFAULT_DESTINATIONS, "movie": "Cinema"}))

    relu = PreferenceStore(
        path=store._path, library_root=store._library_root, source_roots=store._source_roots
    )
    assert relu.load().destinations["movie"] == "Cinema"


def test_fichier_corrompu_retombe_sur_les_defauts(store: PreferenceStore) -> None:
    """Un JSON casse ne doit pas empecher l'application de demarrer."""
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._path.write_text("{ ceci n'est pas du json", encoding="utf-8")

    neuf = PreferenceStore(
        path=store._path, library_root=store._library_root, source_roots=store._source_roots
    )
    assert neuf.load().destinations == DEFAULT_DESTINATIONS


def test_un_reglage_disparu_est_ignore_sans_tout_emporter(store: PreferenceStore) -> None:
    """« ai.batch_size » a quitte le code, mais pas les fichiers deja ecrits.

    Le passer au constructeur leve un TypeError qu'aucun bloc ne rattrape :
    l'application repartirait alors avec TOUTES les preferences aux defauts —
    destinations, gabarits, sources — pour un champ qui ne sert plus. Un
    reglage supprime doit s'oublier, pas tout emporter avec lui.
    """
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._path.write_text(
        json.dumps(
            {
                "destinations": {**DEFAULT_DESTINATIONS, "movie": "Cinema"},
                "ai": {"provider": "groq", "batch_size": 12, "reglage_du_futur": True},
            }
        ),
        encoding="utf-8",
    )

    neuf = PreferenceStore(
        path=store._path, library_root=store._library_root, source_roots=store._source_roots
    )
    prefs = neuf.load()

    assert prefs.destinations["movie"] == "Cinema"
    assert prefs.ai.provider == "groq"
    assert not hasattr(prefs.ai, "batch_size")


def test_les_nouveaux_blocs_ont_leurs_defauts_sur_un_ancien_fichier(
    store: PreferenceStore,
) -> None:
    """Un fichier ecrit avant l'arrivee des metadonnees et du parcours."""
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._path.write_text(json.dumps({"templates": {}}), encoding="utf-8")

    prefs = PreferenceStore(
        path=store._path, library_root=store._library_root, source_roots=store._source_roots
    ).load()

    assert prefs.metadata.language == "fr-FR"
    assert prefs.metadata.tmdb_api_key == ""
    assert prefs.scan.min_size_bytes() == 50 * 1024 * 1024
    assert prefs.scan.extra_skip_dirs == []


def test_une_valeur_de_la_mauvaise_nature_retombe_sur_le_defaut(store: PreferenceStore) -> None:
    """« "languages": "fr" » passait la relecture puis faisait echouer TOUS les
    enregistrements ; « "notifications": "oops" » levait au demarrage. Chaque
    valeur fautive est ecartee, le reste du fichier est garde."""
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._path.write_text(
        json.dumps(
            {
                "destinations": {**DEFAULT_DESTINATIONS, "movie": "Cinema"},
                "custom_sources": "/pas/une/liste",
                "subtitles": {"languages": "fr", "enabled": "false", "overwrite": 1},
                "notifications": "oops",
                "automation": {"interval_minutes": True, "quiet_seconds": 30},
                "ai": {"threshold": 1, "api_key": 12345},
                "integration": {"api_key": 42},
            }
        ),
        encoding="utf-8",
    )

    neuf = PreferenceStore(
        path=store._path, library_root=store._library_root, source_roots=store._source_roots
    )
    prefs = neuf.load()

    assert prefs.destinations["movie"] == "Cinema"
    assert prefs.custom_sources == []
    assert prefs.subtitles.languages == SubtitleSettings().languages
    assert prefs.subtitles.enabled is False
    assert prefs.subtitles.overwrite is False
    assert prefs.notifications == NotificationSettings()
    # bool est un int pour Python : il ne doit pas passer pour un intervalle.
    assert prefs.automation.interval_minutes == AutomationSettings().interval_minutes
    assert prefs.automation.quiet_seconds == 30
    # Un entier la ou un decimal est attendu est accepte.
    assert prefs.ai.threshold == 1.0
    assert isinstance(prefs.ai.threshold, float)
    assert prefs.ai.api_key == ""
    assert prefs.integration.api_key == ""
    # Et surtout : l'enregistrement suivant passe.
    neuf.save(prefs)


def test_un_fichier_illisible_n_est_remplace_qu_apres_avoir_ete_mis_de_cote(
    store: PreferenceStore,
) -> None:
    """Une virgule en trop ne doit pas couter les sources, les gabarits et les
    cles : rien n'est ecrit tant que personne n'enregistre, et l'original est
    copie juste avant."""
    illisible = '{"destinations": {"movie": "Cinema"},}'
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._path.write_text(illisible, encoding="utf-8")

    neuf = PreferenceStore(
        path=store._path, library_root=store._library_root, source_roots=store._source_roots
    )
    assert neuf.load().destinations == DEFAULT_DESTINATIONS
    assert neuf.unreadable is True

    # La cle du demarrage : en memoire, stable dans le processus, rien d'ecrit.
    cle = neuf.ensure_api_key()
    assert neuf.ensure_api_key() == cle
    assert store._path.read_text(encoding="utf-8") == illisible
    assert not list(store._path.parent.glob("preferences.json.illisible-*"))

    neuf.save(neuf.load())

    copies = list(store._path.parent.glob("preferences.json.illisible-*"))
    assert [c.read_text(encoding="utf-8") for c in copies] == [illisible]
    assert json.loads(store._path.read_text(encoding="utf-8"))["integration"]["api_key"] == cle
    assert neuf.unreadable is False


def test_sans_copie_de_secours_rien_n_est_ecrit(
    store: PreferenceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    illisible = "{ ceci n'est pas du json"
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._path.write_text(illisible, encoding="utf-8")
    neuf = PreferenceStore(
        path=store._path, library_root=store._library_root, source_roots=store._source_roots
    )
    neuf.load()

    def _panne(*args: object, **kwargs: object) -> None:
        raise OSError("volume plein")

    monkeypatch.setattr(shutil, "copy2", _panne)
    with pytest.raises(PreferenceError, match="copie de secours"):
        neuf.rotate_api_key()
    assert store._path.read_text(encoding="utf-8") == illisible


def test_une_ecriture_interrompue_laisse_l_ancien_fichier_intact(
    store: PreferenceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fichier temporaire puis remplacement : une coupure ne laisse jamais un
    JSON tronque, que la relecture suivante prendrait pour un fichier illisible."""
    store.save(Preferences(destinations={**DEFAULT_DESTINATIONS, "movie": "Cinema"}))
    avant = store._path.read_text(encoding="utf-8")

    def _coupure(*args: object, **kwargs: object) -> None:
        raise OSError("coupure")

    monkeypatch.setattr(os, "replace", _coupure)
    with pytest.raises(OSError, match="coupure"):
        store.save(Preferences(destinations={**DEFAULT_DESTINATIONS, "movie": "Autre"}))
    monkeypatch.undo()

    assert store._path.read_text(encoding="utf-8") == avant
    assert not store._path.with_name(f"{store._path.name}.tmp").exists()


def test_la_cle_s_ecrit_meme_quand_le_reste_ne_valide_plus(store: PreferenceStore) -> None:
    """Une source demontee depuis ne doit pas empecher d'ecrire la cle d'API :
    au demarrage, cela faisait redemarrer le conteneur en boucle."""
    store._path.parent.mkdir(parents=True, exist_ok=True)
    store._path.write_text(json.dumps({"custom_sources": ["/ailleurs/demonte"]}), encoding="utf-8")
    neuf = PreferenceStore(
        path=store._path, library_root=store._library_root, source_roots=store._source_roots
    )
    with pytest.raises(PreferenceError):
        neuf.validate(neuf.load())

    cle = neuf.ensure_api_key()

    relu = json.loads(store._path.read_text(encoding="utf-8"))
    assert relu["integration"]["api_key"] == cle
    assert relu["custom_sources"] == ["/ailleurs/demonte"]


def test_le_jeton_vip_seul_ne_suffit_pas_a_activer_les_sous_titres(
    store: PreferenceStore,
) -> None:
    """Le fournisseur n'emet rien sans cle : accepter le jeton seul enregistrait
    une recherche active qui ne pouvait rien trouver."""
    prefs = Preferences(
        subtitles=SubtitleSettings(enabled=True, opensubtitles_token="jeton-vip-de-test")
    )
    with pytest.raises(PreferenceError, match="jeton VIP seul ne suffit pas"):
        store.save(prefs)


def test_les_refus_sont_accentues(store: PreferenceStore) -> None:
    prefs = Preferences()
    prefs.scan.min_size_mb = -1
    with pytest.raises(PreferenceError, match="ne peut pas être négative"):
        store.save(prefs)


# --- Confinement ------------------------------------------------------------


@pytest.mark.parametrize("hostile", ["../../etc", "/etc/passwd", "..", "  "])
def test_destination_hors_racine_refusee(store: PreferenceStore, hostile: str) -> None:
    with pytest.raises(PreferenceError):
        store.save(Preferences(destinations={**DEFAULT_DESTINATIONS, "movie": hostile}))


def test_destination_resolue_reste_sous_la_racine(store: PreferenceStore) -> None:
    store.save(Preferences(destinations={**DEFAULT_DESTINATIONS, "movie": "Cinema/VF"}))
    resolved = store.destination_root("movie")
    assert store._library_root in resolved.parents


def test_type_de_media_inconnu_refuse(store: PreferenceStore) -> None:
    with pytest.raises(PreferenceError, match="inconnu"):
        store.save(Preferences(destinations={"musique": "Musique"}))


# --- Sources ----------------------------------------------------------------


def test_source_non_declaree_refusee(store: PreferenceStore) -> None:
    """On ne peut pas activer une source qui n'existe dans aucune liste."""
    with pytest.raises(PreferenceError, match="ni une racine montée"):
        store.save(Preferences(enabled_sources=["/ailleurs"]))


# --- Sources ajoutees depuis l'interface ------------------------------------


def test_source_ajoutee_sous_une_racine(store: PreferenceStore) -> None:
    sub = store._source_roots[0] / "films"
    sub.mkdir()
    store.save(Preferences(custom_sources=[str(sub)]))
    assert str(sub) in {str(p) for p in store.all_sources()}


@pytest.mark.parametrize("hostile", ["/etc", "/", "relatif/pas/absolu"])
def test_source_hors_racines_refusee(store: PreferenceStore, hostile: str) -> None:
    """Sans cette regle, l'interface permettrait de parcourir tout le NAS."""
    with pytest.raises(PreferenceError):
        store.save(Preferences(custom_sources=[hostile]))


def test_remontee_dans_une_source_refusee(store: PreferenceStore) -> None:
    evasion = f"{store._source_roots[0]}/../../etc"
    with pytest.raises(PreferenceError, match=r"interdits"):
        store.save(Preferences(custom_sources=[evasion]))


def test_source_ajoutee_puis_activee(store: PreferenceStore) -> None:
    sub = store._source_roots[0] / "animes"
    sub.mkdir()
    store.save(Preferences(custom_sources=[str(sub)], enabled_sources=[str(sub)]))
    resolved = store.resolved_sources()
    assert [str(p) for p in resolved] == [str(sub)]


def test_racine_montee_toujours_disponible(store: PreferenceStore) -> None:
    """Une racine du compose ne disparait pas parce qu'on ajoute des sources."""
    sub = store._source_roots[0] / "x"
    sub.mkdir()
    store.save(Preferences(custom_sources=[str(sub)]))
    paths = {str(p) for p in store.all_sources()}
    assert str(store._source_roots[0]) in paths
    assert str(store._source_roots[1]) in paths


# --- Explorateur ------------------------------------------------------------


def test_browse_sans_argument_liste_les_zones(store: PreferenceStore) -> None:
    """Deux racines sources plus la bibliotheque."""
    node = store.browse(None)
    assert node["path"] is None
    assert len(node["entries"]) == 3
    assert all(e["is_root"] for e in node["entries"])


def test_la_bibliotheque_est_parcourable(store: PreferenceStore) -> None:
    """Scanner sa bibliotheque existante pour la normaliser est un usage a part
    entiere — le cas « rattrapage » pour lequel on sort FileBot d'habitude."""
    node = store.browse(None)
    library = [e for e in node["entries"] if e["is_library"]]
    assert len(library) == 1
    assert library[0]["path"] == str(store._library_root)


def test_dossier_de_bibliotheque_ajoutable_en_source(store: PreferenceStore) -> None:
    films = store._library_root / "Films"
    films.mkdir()
    store.save(Preferences(custom_sources=[str(films)]))
    assert str(films) in {str(p) for p in store.all_sources()}


def test_browse_descend(store: PreferenceStore) -> None:
    root = store._source_roots[0]
    (root / "Films").mkdir()
    (root / ".cache").mkdir()
    node = store.browse(str(root))
    names = [e["name"] for e in node["entries"]]
    assert "Films" in names
    assert ".cache" not in names  # les dossiers caches sont ecartes


def test_browse_ne_remonte_pas_au_dessus_des_racines(store: PreferenceStore) -> None:
    node = store.browse(str(store._source_roots[0]))
    assert node["parent"] is None


def test_browse_hors_perimetre_refuse(store: PreferenceStore) -> None:
    with pytest.raises(PreferenceError):
        store.browse("/etc")


def test_selection_vide_signifie_toutes(store: PreferenceStore) -> None:
    assert len(store.resolved_sources()) == 2


def test_selection_partielle(store: PreferenceStore) -> None:
    first = str(store._source_roots[0])
    store.save(Preferences(enabled_sources=[first]))
    resolved = store.resolved_sources()
    assert len(resolved) == 1
    assert str(resolved[0]) == first


# --- Gabarits ---------------------------------------------------------------


def test_gabarit_invalide_refuse(store: PreferenceStore) -> None:
    with pytest.raises(PreferenceError, match="gabarit invalide"):
        store.save(Preferences(templates={"movie": "Films/{jeton_qui_nexiste_pas}"}))


def test_gabarit_valide_accepte(store: PreferenceStore) -> None:
    store.save(Preferences(templates={"movie": "Cinema/{title}{? year: ($)}"}))
    assert store.load().template_for("movie").startswith("Cinema/")


def test_gabarit_absent_retombe_sur_le_prereglage(store: PreferenceStore) -> None:
    assert "{title}" in store.load().template_for("episode")
