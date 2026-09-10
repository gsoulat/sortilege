"""Le journal entree par entree, pour une page dediee.

``GET /journal`` regroupe par oeuvre : c'est la maille a laquelle on decide
d'annuler une serie mal identifiee. Le besoin est ici l'inverse — la liste des
changements, ligne a ligne, pour en defaire un seul — et la reponse existante
ne portait ni titre, ni nature d'operation, ni de quoi annuler.

Trois proprietes decident si la page est branchable :

1. **Le plus recent d'abord**, sur la totalite du journal et pas sur la page
   courante : sinon le tri depend de la page qu'on regarde.
2. **Chaque entree porte de quoi l'annuler.** Une liste ou l'on ne peut agir
   n'est qu'un journal de plus.
3. **Le format sur disque ne change pas.** Des fichiers ecrits par les versions
   precedentes doivent rester lisibles.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sortilege.api import deps
from sortilege.core.journal import Journal, MoveRecord
from sortilege.main import app


@pytest.fixture
def journal(tmp_path: Path, monkeypatch) -> Journal:
    """Un journal a nous, substitue a celui de l'application."""
    j = Journal(tmp_path / "journal.jsonl")
    monkeypatch.setattr(deps, "get_journal", lambda: j)
    from sortilege.api import review

    monkeypatch.setattr(review, "get_journal", lambda: j)
    return j


@pytest.fixture
def client(journal: Journal) -> TestClient:
    with TestClient(app) as c:
        assert (
            c.post("/api/auth/login", json={"password": "mot-de-passe-de-test"}).status_code == 200
        )
        yield c


def entree(
    journal: Journal,
    *,
    plan_id: str = "p1",
    title: str = "Severance",
    at: str = "2026-09-01T10:00:00",
    kind: str = "video",
    work_kind: str = "episode",
    destination: str = "/lib/Severance/S01E01.mkv",
) -> None:
    journal.append(
        MoveRecord(
            timestamp=at,
            plan_id=plan_id,
            source=f"/dl/{Path(destination).name}",
            destination=destination,
            method="rename",
            kind=kind,
            title=title,
            work_kind=work_kind,
        )
    )


def page(client: TestClient, **params):
    r = client.get("/api/review/journal/entries", params=params)
    assert r.status_code == 200, r.text
    return r.json()


# --- L'ordre et la pagination ------------------------------------------------


def test_le_plus_recent_vient_en_tete(client: TestClient, journal: Journal) -> None:
    entree(journal, title="Ancien", at="2026-09-01T10:00:00")
    entree(journal, title="Recent", at="2026-09-03T10:00:00")
    entree(journal, title="Milieu", at="2026-09-02T10:00:00")

    titres = [e["title"] for e in page(client)["entries"]]

    assert titres == ["Recent", "Milieu", "Ancien"]


def test_le_tri_porte_sur_tout_le_journal_pas_sur_la_page(
    client: TestClient, journal: Journal
) -> None:
    """Sinon l'ordre dependrait de la page qu'on regarde — le defaut classique
    d'une pagination qui trie apres avoir decoupe."""
    for jour in range(1, 6):
        entree(journal, title=f"J{jour}", at=f"2026-09-0{jour}T10:00:00")

    premiere = page(client, per_page=2)
    seconde = page(client, per_page=2, page=2)

    assert [e["title"] for e in premiere["entries"]] == ["J5", "J4"]
    assert [e["title"] for e in seconde["entries"]] == ["J3", "J2"]


def test_le_compte_de_pages_est_annonce(client: TestClient, journal: Journal) -> None:
    for jour in range(1, 6):
        entree(journal, title=f"J{jour}", at=f"2026-09-0{jour}T10:00:00")

    reponse = page(client, per_page=2)

    assert reponse["total"] == 5
    assert reponse["pages"] == 3
    assert reponse["page"] == 1


def test_une_page_au_dela_de_la_fin_est_vide_sans_erreur(
    client: TestClient, journal: Journal
) -> None:
    """L'interface peut demander une page qui vient de disparaitre sous elle,
    apres une annulation : un 500 la casserait pour un cas normal."""
    entree(journal)

    assert page(client, page=99)["entries"] == []


def test_un_journal_vide_ne_leve_pas(client: TestClient) -> None:
    reponse = page(client)
    assert reponse["total"] == 0
    assert reponse["pages"] == 1


def test_la_taille_de_page_est_bornee(client: TestClient, journal: Journal) -> None:
    """Au-dela, la reponse pese plus que ce qu'un ecran peut montrer."""
    entree(journal)

    assert page(client, per_page=100_000)["per_page"] == 200
    assert page(client, per_page=0)["per_page"] == 1


# --- De quoi afficher, et de quoi annuler ------------------------------------


def test_une_entree_porte_tout_ce_qu_il_faut(client: TestClient, journal: Journal) -> None:
    entree(journal, plan_id="abc123", title="Dune", work_kind="movie", destination="/lib/Dune.mkv")

    e = page(client)["entries"][0]

    assert e["title"] == "Dune"
    assert e["work_kind"] == "movie"
    assert e["source"] == "/dl/Dune.mkv"
    assert e["destination"] == "/lib/Dune.mkv"
    assert e["operation"] == "video"
    assert e["operation_label"] == "Rangement"
    assert e["timestamp"]


def test_l_identifiant_sert_a_l_annulation_existante(
    client: TestClient, journal: Journal, tmp_path: Path
) -> None:
    """Une liste ou l'on ne peut rien defaire n'est qu'un journal de plus."""
    destination = tmp_path / "lib" / "Dune.mkv"
    destination.parent.mkdir(parents=True)
    destination.write_text("video", encoding="utf-8")
    journal.append(
        MoveRecord(
            timestamp="2026-09-01T10:00:00",
            plan_id="abc123",
            source=str(tmp_path / "dl" / "Dune.mkv"),
            destination=str(destination),
            method="rename",
            title="Dune",
            work_kind="movie",
        )
    )
    (tmp_path / "dl").mkdir()

    identifiant = page(client)["entries"][0]["plan_id"]
    r = client.post("/api/review/undo", json={"plan_ids": [identifiant]})

    assert r.status_code == 200
    assert r.json()["undone"] == 1
    assert (tmp_path / "dl" / "Dune.mkv").is_file()
    assert page(client)["total"] == 0


def test_une_entree_sans_titre_retombe_sur_son_dossier(
    client: TestClient, journal: Journal
) -> None:
    """Les deplacements anterieurs a l'inscription du titre doivent rester
    lisibles : le journal sur disque n'a pas change de format."""
    journal.append(
        MoveRecord(
            timestamp="2026-09-01T10:00:00",
            plan_id="vieux",
            source="/dl/x.mkv",
            destination="/lib/Severance (2022)/Season 01/S01E01.mkv",
            method="rename",
        )
    )

    e = page(client)["entries"][0]

    assert e["title"] == "Severance (2022)"
    assert e["operation"] == "video"


# --- Les filtres -------------------------------------------------------------


def test_le_filtre_par_nature_d_operation(client: TestClient, journal: Journal) -> None:
    entree(journal, title="Dune", kind="video")
    entree(journal, title="Dune", kind="companion", destination="/lib/Dune.srt")
    entree(journal, title="Dune", kind="trash", destination="/lib/.corbeille/reste.nfo")

    assert page(client, operation="trash")["total"] == 1
    assert page(client, operation="companion")["entries"][0]["operation_label"] == "Compagnon"


def test_les_natures_sont_comptees_sur_tout_le_journal(
    client: TestClient, journal: Journal
) -> None:
    """Le compte doit survivre au filtre : sinon les onglets tombent a zero des
    qu'on en choisit un, et on ne peut plus revenir."""
    entree(journal, kind="video")
    entree(journal, kind="companion", destination="/lib/Severance/S01E01.srt")

    natures = {n["code"]: n["count"] for n in page(client, operation="video")["operations"]}

    assert natures == {"video": 1, "companion": 1, "trash": 0}


def test_une_nature_inconnue_est_refusee(client: TestClient) -> None:
    """Une liste vide laisserait croire que le journal l'est aussi."""
    r = client.get("/api/review/journal/entries", params={"operation": "nawak"})

    assert r.status_code == 400
    assert "nawak" in r.json()["detail"]


def test_la_recherche_par_titre(client: TestClient, journal: Journal) -> None:
    entree(journal, title="Severance")
    entree(journal, title="Dark Matter", destination="/lib/Dark Matter/S01E01.mkv")

    reponse = page(client, q="dark")

    assert [e["title"] for e in reponse["entries"]] == ["Dark Matter"]
    assert reponse["journal_size"] == 2, "le total du journal reste annonce malgre le filtre"


def test_la_recherche_trouve_aussi_les_entrees_sans_titre(
    client: TestClient, journal: Journal
) -> None:
    """Elles s'affichent sous le nom de leur dossier : les rendre introuvables
    sous ce nom-la serait incomprehensible."""
    journal.append(
        MoveRecord(
            timestamp="2026-09-01T10:00:00",
            plan_id="vieux",
            source="/dl/x.mkv",
            destination="/lib/Severance (2022)/Season 01/S01E01.mkv",
            method="rename",
        )
    )

    assert page(client, q="severance")["total"] == 1


def test_les_filtres_se_combinent(client: TestClient, journal: Journal) -> None:
    entree(journal, title="Dune", kind="video", destination="/lib/Dune.mkv")
    entree(journal, title="Dune", kind="companion", destination="/lib/Dune.srt")
    entree(journal, title="Severance", kind="companion")

    reponse = page(client, q="dune", operation="companion")

    assert reponse["total"] == 1
    assert reponse["entries"][0]["destination"] == "/lib/Dune.srt"


# --- Valider definitivement -------------------------------------------------
#
# Le journal grossit a chaque rangement et ne diminue jamais. Passe quelques
# milliers d'entrees il ne se consulte plus, et personne ne va annuler un
# deplacement d'il y a six mois. Le vider quand on est satisfait, c'est
# refermer un chantier.


def test_la_purge_vide_le_journal(client, journal) -> None:
    entree(journal, plan_id="p1", title="Severance")
    entree(journal, plan_id="p2", title="Dune")
    avant = client.get("/api/review/journal/entries").json()["total"]
    assert avant == 2, "le test n'a plus d'objet si le journal est vide"

    sortie = client.post("/api/review/journal/purge", json={"confirm": True})

    assert sortie.status_code == 200
    assert sortie.json()["purged"] == avant
    assert client.get("/api/review/journal/entries").json()["total"] == 0


def test_la_purge_doit_etre_confirmee(client, journal) -> None:
    """Vider le journal supprime la seule facon de revenir en arriere : cela ne
    doit pas pouvoir arriver par un champ oublie."""
    entree(journal, plan_id="p1", title="Severance")
    avant = client.get("/api/review/journal/entries").json()["total"]

    refus = client.post("/api/review/journal/purge", json={})

    assert refus.status_code == 400
    assert client.get("/api/review/journal/entries").json()["total"] == avant
