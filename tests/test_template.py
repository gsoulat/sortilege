"""Tests du moteur de gabarit.

L'enjeu principal n'est pas le rendu nominal mais ce qui arrive quand une
valeur manque ou quand une metadonnee est hostile.
"""

import pytest

from sortilege.core.template import (
    PRESETS,
    TOKEN_NAMES,
    TemplateError,
    render,
    validate,
)


def test_rendu_nominal() -> None:
    out = render(
        "Series/{title}/Season {season:02}/{title} - S{season:02}E{episode:02}",
        {"title": "Severance", "season": 2, "episode": 7},
    )
    assert out == "Series/Severance/Season 02/Severance - S02E07"


def test_conditionnel_ecrit_si_renseigne() -> None:
    tpl = "Films/{title}{? year: ($)}"
    assert render(tpl, {"title": "Dune", "year": 2024}) == "Films/Dune (2024)"


def test_conditionnel_muet_si_absent() -> None:
    """Le piege corrige apres l'apercu live : pas de « () » orphelines."""
    tpl = "Films/{title}{? year: ($)}"
    assert render(tpl, {"title": "Sans Annee"}) == "Films/Sans Annee"


def test_jeton_absent_ne_laisse_pas_de_segment_vide() -> None:
    out = render("Films/{title}/{year}/x", {"title": "Dune"})
    assert "//" not in out
    assert out == "Films/Dune/x"


def test_espaces_toleres_dans_les_jetons() -> None:
    assert render("{ title }", {"title": "Dune"}) == "Dune"
    assert render("{? year: ($)}", {"year": 2024}) == "(2024)"


def test_zero_padding() -> None:
    assert render("{season:02}-{episode:03}", {"season": 2, "episode": 7}) == "02-007"


# --- Securite ---------------------------------------------------------------


def test_une_metadonnee_ne_peut_pas_creer_de_segment() -> None:
    """Un titre de provider ou de LLM ne doit jamais introduire de separateur."""
    out = render("Films/{title}/x", {"title": "../../etc/passwd"})
    assert out == "Films/etcpasswd/x"
    assert ".." not in out


def test_valeur_avec_caracteres_interdits() -> None:
    out = render("{title}", {"title": 'A<b>:"c|d?e*f'})
    for bad in '<>:"|?*':
        assert bad not in out


def test_octet_nul_retire() -> None:
    assert "\x00" not in render("{title}", {"title": "a\x00b"})


def test_pas_d_evaluation_de_code() -> None:
    """Le moteur ne doit rien interpreter : ni Groovy, ni f-string, ni format."""
    tpl = "{title}"
    valeur = "{__import__('os').system('id')}"
    assert render(tpl, {"title": valeur}) == valeur.replace("*", "")


# --- Validation -------------------------------------------------------------


def test_jeton_inconnu_rejete() -> None:
    with pytest.raises(TemplateError, match="jeton inconnu"):
        validate("Films/{inconnu}")


def test_accolade_orpheline_rejetee() -> None:
    with pytest.raises(TemplateError, match="accolade"):
        validate("Films/{title")


def test_chemin_absolu_rejete() -> None:
    with pytest.raises(TemplateError, match="relatif"):
        validate("/etc/{title}")


def test_gabarit_vide_rejete() -> None:
    with pytest.raises(TemplateError, match="vide"):
        validate("   ")


def test_gabarit_trop_long_rejete() -> None:
    with pytest.raises(TemplateError, match="trop long"):
        validate("{title}" * 200)


@pytest.mark.parametrize("family", sorted(PRESETS))
@pytest.mark.parametrize("kind", ["movie", "episode", "anime"])
def test_tous_les_prereglages_sont_valides(family: str, kind: str) -> None:
    validate(PRESETS[family][kind])


def test_prereglages_ne_produisent_pas_de_parentheses_vides() -> None:
    """Regression : le preset film Jellyfin codait « ({year}) » en dur."""
    for family, kinds in PRESETS.items():
        out = render(kinds["movie"], {"title": "Orphelin"})
        assert "()" not in out, f"{family} laisse des parentheses vides"


def test_catalogue_coherent() -> None:
    assert "title" in TOKEN_NAMES
    assert "absolute_episode" in TOKEN_NAMES
