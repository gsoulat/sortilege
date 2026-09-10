"""Empreinte OpenSubtitles : identifier un fichier par son CONTENU.

Tout le reste de l'application DEVINE — le parseur lit un nom de release, la
sonde lit des tags. L'empreinte, elle, ne devine pas : deux fichiers qui la
partagent sont le meme encodage. C'est ce qui separe une identification
probable d'une identification certaine.

L'algorithme est fige par un standard vieux de vingt ans. Ces tests verifient
donc surtout qu'on le respecte : une empreinte juste seulement chez nous ne
servirait a rien, puisque tout son interet est d'etre reconnue par une base
exterieure.
"""

from __future__ import annotations

from pathlib import Path

from sortilege.core.filehash import CHUNK, MIN_SIZE, compute


def video(path: Path, taille: int, motif: bytes = b"\x00") -> Path:
    path.write_bytes(motif * (taille // len(motif)))
    return path


def test_une_empreinte_fait_seize_caracteres(tmp_path: Path) -> None:
    """Format attendu par la base : entier 64 bits en hexadecimal."""
    empreinte = compute(video(tmp_path / "a.mkv", MIN_SIZE))

    assert empreinte is not None
    assert len(empreinte) == 16
    int(empreinte, 16)


def test_le_meme_fichier_donne_la_meme_empreinte(tmp_path: Path) -> None:
    f = video(tmp_path / "a.mkv", MIN_SIZE * 3)
    assert compute(f) == compute(f)


def test_deux_tailles_differentes_donnent_deux_empreintes(tmp_path: Path) -> None:
    """La taille entre dans le calcul : c'est ce qui distingue deux encodages
    dont le debut et la fin se ressemblent."""
    a = video(tmp_path / "a.mkv", MIN_SIZE)
    b = video(tmp_path / "b.mkv", MIN_SIZE + 8)

    assert compute(a) != compute(b)


def test_un_contenu_different_donne_une_empreinte_differente(tmp_path: Path) -> None:
    a = video(tmp_path / "a.mkv", MIN_SIZE, b"\x01")
    b = video(tmp_path / "b.mkv", MIN_SIZE, b"\x02")

    assert compute(a) != compute(b)


def test_seuls_128_kio_sont_lus(tmp_path: Path) -> None:
    """C'est ce qui rend l'empreinte utilisable sur une bibliotheque entiere :
    un vrai condensat imposerait de relire des teraoctets."""
    gros = video(tmp_path / "gros.mkv", CHUNK * 40)
    lus = 0
    ouverture = Path.open

    class Compteur:
        def __init__(self, inner):
            self._inner = inner

        def read(self, n=-1):
            nonlocal lus
            data = self._inner.read(n)
            lus += len(data)
            return data

        def __getattr__(self, nom):
            return getattr(self._inner, nom)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return self._inner.__exit__(*a)

    Path.open = lambda self, *a, **k: Compteur(ouverture(self, *a, **k))  # type: ignore[method-assign]
    try:
        compute(gros)
    finally:
        Path.open = ouverture  # type: ignore[method-assign]

    assert lus <= CHUNK * 2, f"{lus} octets lus sur un fichier de {CHUNK * 40}"


def test_un_fichier_trop_court_n_a_pas_d_empreinte(tmp_path: Path) -> None:
    """Les deux morceaux se chevaucheraient, et un fichier de cette taille
    n'est de toute facon pas une video."""
    assert compute(video(tmp_path / "court.mkv", 1024)) is None


def test_un_fichier_absent_ne_leve_pas(tmp_path: Path) -> None:
    """Une empreinte est un signal EN PLUS : son absence ne doit jamais
    interrompre un scan."""
    assert compute(tmp_path / "jamais-cree.mkv") is None
