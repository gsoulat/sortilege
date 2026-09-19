"""Les fichiers « ._ » de macOS ne sont pas des films.

Sur un volume qui n'est pas au format Apple — disque exFAT, partage SMB —,
macOS depose a cote de chaque fichier un « ._Nom.mkv » de quelques
kilo-octets. L'extension est celle d'une video : chacun passait pour un second
exemplaire, donc un doublon fantome par film, dans la mediatheque comme sur un
disque externe.
"""

from __future__ import annotations

from pathlib import Path

from sortilege.core.scanner import collect


def test_un_fichier_appledouble_n_est_pas_recense(tmp_path: Path) -> None:
    dossier = tmp_path / "Films" / "Dune (2021)"
    dossier.mkdir(parents=True)
    (dossier / "Dune (2021).mkv").write_bytes(b"x" * 1000)
    (dossier / "._Dune (2021).mkv").write_bytes(b"\x00\x05\x16\x07" + b"\x00" * 4092)

    trouves, sautes, erreurs = collect([tmp_path])

    assert [p.name for _, p in trouves] == ["Dune (2021).mkv"]
    assert sautes == 0, "ce n'est pas un fichier que l'utilisateur a choisi d'ecarter"
    assert erreurs == []


def test_un_vrai_film_dont_le_nom_commence_par_un_tiret_bas_reste(tmp_path: Path) -> None:
    """Seul le prefixe exact « ._ » est ecarte."""
    (tmp_path / "_Film.mkv").write_bytes(b"x")

    trouves, _, _ = collect([tmp_path])

    assert [p.name for _, p in trouves] == ["_Film.mkv"]
