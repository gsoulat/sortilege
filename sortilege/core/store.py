"""Persistance sur disque : decisions retenues et etat de travail.

SQLite par le module standard, sans ORM. La base tient en deux tables et les
requetes sont triviales : une couche d'abstraction couterait une dependance et
des migrations pour ne rien simplifier.

Deux natures de donnees, deliberement separees :

- **Les decisions** sont durables et precieuses. Quand tu tranches entre deux
  « Dark Matter », ce choix doit valoir pour toujours — c'est ce qui fait qu'une
  file de revue converge vers le vide au lieu de reposer les memes questions a
  chaque scan.
- **L'etat de travail** (scan, plans) est reconstructible mais couteux : sur un
  millier de fichiers, le reperdre a chaque redemarrage du conteneur rend
  l'outil inutilisable au quotidien.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    kind        TEXT NOT NULL,
    title_key   TEXT NOT NULL,
    provider    TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title       TEXT NOT NULL,
    year        INTEGER,
    poster_url  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kind, title_key)
);

CREATE TABLE IF NOT EXISTS blobs (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass(slots=True)
class Decision:
    """Une identification tranchee par un humain, valable pour toujours."""

    kind: str
    title_key: str
    provider: str
    external_id: str
    title: str
    year: int | None = None
    poster_url: str = ""
    hits: int = 0
    created_at: str = ""


def title_key(title: str) -> str:
    """Cle de recherche insensible a la casse et aux accents.

    Le titre LU par le parseur sert de cle, pas le titre officiel : c'est lui
    qu'on reverra au prochain scan, et c'est sur lui qu'on doit reconnaitre la
    situation deja tranchee.
    """
    decomposed = unicodedata.normalize("NFKD", title.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip()


class Store:
    """Acces a la base. Une instance pour toute l'application."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _connect(self):
        # Une connexion par operation : SQLite n'aime pas qu'on partage un
        # objet entre threads, et l'application en utilise plusieurs (scan,
        # cycle automatique, requetes HTTP).
        conn = sqlite3.connect(self._path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # --- Decisions ----------------------------------------------------------

    def remember(self, decision: Decision) -> None:
        """Retient un choix humain. Un nouveau choix remplace l'ancien.

        Changer d'avis doit etre possible : sans remplacement, une erreur de
        clic resterait gravee et il n'y aurait aucun moyen de la corriger.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO decisions
                    (kind, title_key, provider, external_id, title, year, poster_url, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, title_key) DO UPDATE SET
                    provider = excluded.provider,
                    external_id = excluded.external_id,
                    title = excluded.title,
                    year = excluded.year,
                    poster_url = excluded.poster_url,
                    created_at = excluded.created_at
                """,
                (
                    decision.kind,
                    decision.title_key,
                    decision.provider,
                    decision.external_id,
                    decision.title,
                    decision.year,
                    decision.poster_url,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def recall(self, kind: str, title: str) -> Decision | None:
        """Retrouve un choix deja fait pour ce titre, s'il existe."""
        key = title_key(title)
        if not key:
            return None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM decisions WHERE kind = ? AND title_key = ?", (kind, key)
            ).fetchone()
        if row is None:
            return None
        return Decision(**{k: row[k] for k in row.keys()})

    def note_hit(self, kind: str, title_key_value: str) -> None:
        """Compte les reutilisations — de quoi montrer ce que la memoire evite."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE decisions SET hits = hits + 1 WHERE kind = ? AND title_key = ?",
                (kind, title_key_value),
            )

    def decisions(self) -> list[Decision]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM decisions ORDER BY created_at DESC").fetchall()
        return [Decision(**{k: r[k] for k in r.keys()}) for r in rows]

    def forget(self, kind: str, title_key_value: str) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM decisions WHERE kind = ? AND title_key = ?",
                (kind, title_key_value),
            )
        return cursor.rowcount > 0

    # --- Etat de travail ----------------------------------------------------

    def save_blob(self, key: str, payload: object) -> None:
        """Enregistre un etat serialisable.

        Une panne d'ecriture est journalisee et avalee : perdre la reprise
        apres redemarrage est genant, interrompre un scan en cours pour cette
        raison le serait bien plus.
        """
        try:
            encoded = json.dumps(payload, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            logger.warning("etat « %s » non serialisable : %s", key, exc)
            return

        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO blobs (key, payload, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, "
                    "updated_at = excluded.updated_at",
                    (key, encoded, datetime.now(UTC).isoformat()),
                )
        except sqlite3.Error as exc:
            logger.warning("ecriture de l'etat « %s » impossible : %s", key, exc)

    def load_blob(self, key: str) -> object | None:
        try:
            with self._connect() as conn:
                row = conn.execute("SELECT payload FROM blobs WHERE key = ?", (key,)).fetchone()
        except sqlite3.Error as exc:
            logger.warning("lecture de l'etat « %s » impossible : %s", key, exc)
            return None

        if row is None:
            return None
        try:
            return json.loads(row["payload"])
        except ValueError:
            # Etat ecrit par une version anterieure au format different : on
            # repart de zero plutot que d'echouer au demarrage.
            logger.warning("etat « %s » illisible, ignore", key)
            return None

    def drop_blob(self, key: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM blobs WHERE key = ?", (key,))
