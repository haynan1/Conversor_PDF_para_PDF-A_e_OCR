"""Livro-razão em SQLite: idempotência e trilha de auditoria.

PDF/A existe para acervo — contexto em que "converti isso?", "com qual versão do
Tesseract?" e "o resultado foi validado?" são perguntas que reaparecem anos
depois. O kit original não respondia a nenhuma delas.

O ledger cumpre dois papéis:

* **idempotência** — a chave é ``(sha256 da origem, hash da receita)``. Reexecutar
  sobre a mesma pasta não reprocessa nada; mudar o idioma, o perfil PDF/A ou a
  versão do Tesseract muda a receita e reprocessa o que for afetado.
* **auditoria** — cada tentativa fica registrada, inclusive as que falharam. As
  linhas nunca são apagadas nem sobrescritas; o histórico é acumulativo.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    version      TEXT NOT NULL,
    recipe_hash  TEXT NOT NULL,
    toolchain    TEXT NOT NULL,
    workspace    TEXT NOT NULL,
    settings     TEXT NOT NULL,
    totals       TEXT
);

CREATE TABLE IF NOT EXISTS documents (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id             INTEGER NOT NULL REFERENCES runs(id),
    created_at         TEXT NOT NULL,
    source_path        TEXT NOT NULL,
    source_sha256      TEXT NOT NULL,
    source_bytes       INTEGER NOT NULL,
    output_path        TEXT,
    output_sha256      TEXT,
    output_bytes       INTEGER,
    recipe_hash        TEXT NOT NULL,
    status             TEXT NOT NULL,
    nature             TEXT,
    pages              INTEGER,
    mode               TEXT,
    attempts           INTEGER NOT NULL DEFAULT 0,
    conformance        TEXT,
    conformance_detail TEXT,
    duration_ms        INTEGER,
    detail             TEXT
);

CREATE INDEX IF NOT EXISTS idx_documents_lookup
    ON documents (source_sha256, recipe_hash, status);
CREATE INDEX IF NOT EXISTS idx_documents_run
    ON documents (run_id);
CREATE INDEX IF NOT EXISTS idx_documents_created
    ON documents (created_at DESC);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Record:
    """Uma linha da trilha de auditoria."""

    id: int
    created_at: str
    source_path: str
    source_sha256: str
    output_path: str | None
    output_sha256: str | None
    output_bytes: int | None
    status: str
    nature: str | None
    pages: int | None
    mode: str | None
    conformance: str | None
    duration_ms: int | None
    detail: str | None


class Ledger:
    """Acesso serializado ao livro-razão. Seguro para uso por várias threads."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    # ------------------------------------------------------------- execução --

    def start_run(
        self,
        *,
        recipe_hash: str,
        toolchain: str,
        workspace: Path,
        settings: dict[str, Any],
    ) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO runs (started_at, version, recipe_hash, toolchain, workspace,"
                " settings) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    _now(),
                    __version__,
                    recipe_hash,
                    toolchain,
                    str(workspace),
                    json.dumps(settings, ensure_ascii=False, sort_keys=True),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    def finish_run(self, run_id: int, totals: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET finished_at = ?, totals = ? WHERE id = ?",
                (_now(), json.dumps(totals, ensure_ascii=False, sort_keys=True), run_id),
            )
            self._conn.commit()

    # ----------------------------------------------------------- documentos --

    def lookup(self, source_sha256: str, recipe_hash: str) -> Record | None:
        """Conversão bem-sucedida mais recente para esta origem e esta receita."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM documents WHERE source_sha256 = ? AND recipe_hash = ?"
                " AND status = 'ok' ORDER BY id DESC LIMIT 1",
                (source_sha256, recipe_hash),
            ).fetchone()
        return _to_record(row) if row else None

    #: Colunas gravadas por :meth:`record`, nesta ordem. O SQL é interpolado a
    #: partir desta tupla — constante do módulo, jamais entrada externa — e
    #: todos os valores seguem por parâmetros ligados.
    _DOCUMENT_COLUMNS = (
        "source_path",
        "source_sha256",
        "source_bytes",
        "output_path",
        "output_sha256",
        "output_bytes",
        "recipe_hash",
        "status",
        "nature",
        "pages",
        "mode",
        "attempts",
        "conformance",
        "conformance_detail",
        "duration_ms",
        "detail",
    )

    def record(self, run_id: int, **fields: Any) -> int:
        columns = self._DOCUMENT_COLUMNS
        # `attempts` é NOT NULL: o DEFAULT da tabela não se aplica quando a
        # coluna é informada explicitamente, ainda que como NULL.
        fields.setdefault("attempts", 0)
        values = [fields.get(name) for name in columns]
        placeholders = ", ".join("?" for _ in columns) + ", ?, ?"
        statement = (
            f"INSERT INTO documents ({', '.join(columns)}, run_id, created_at)"  # noqa: S608
            f" VALUES ({placeholders})"
        )
        with self._lock:
            cursor = self._conn.execute(statement, (*values, run_id, _now()))
            self._conn.commit()
            return int(cursor.lastrowid or 0)

    # ---------------------------------------------------------- consultas --

    def recent(self, limit: int = 20, *, status: str | None = None) -> list[Record]:
        query = "SELECT * FROM documents"
        params: list[Any] = []
        if status:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_to_record(row) for row in rows]

    def totals(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM documents GROUP BY status"
            ).fetchall()
        return {row["status"]: row["n"] for row in rows}

    def runs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------ ciclo de vida --

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _to_record(row: sqlite3.Row) -> Record:
    return Record(
        id=row["id"],
        created_at=row["created_at"],
        source_path=row["source_path"],
        source_sha256=row["source_sha256"],
        output_path=row["output_path"],
        output_sha256=row["output_sha256"],
        output_bytes=row["output_bytes"],
        status=row["status"],
        nature=row["nature"],
        pages=row["pages"],
        mode=row["mode"],
        conformance=row["conformance"],
        duration_ms=row["duration_ms"],
        detail=row["detail"],
    )
