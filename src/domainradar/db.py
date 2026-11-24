"""SQLite-Zugriff für DomainRadar.

Eine Datei, ein Writer, WAL-Mode. Kein Serverdienst – Backup ist `vzdump` bzw.
eine Datei-Kopie.

Das Schema ist absichtlich flach: alles, was ein Filter oder eine Aggregation
braucht, steht als eigene Spalte bereit. So bleibt die API ein einfacher
SQL-Query statt eines Joins über halb normalisierte Tabellen.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logs (
  id         INTEGER PRIMARY KEY,
  name       TEXT UNIQUE NOT NULL,
  url        TEXT NOT NULL,
  tree_size  INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT,
  state      TEXT NOT NULL DEFAULT 'active',
  last_error TEXT
);

CREATE TABLE IF NOT EXISTS certs (
  id          INTEGER PRIMARY KEY,
  log_id      INTEGER NOT NULL REFERENCES logs(id) ON DELETE CASCADE,
  cert_index  INTEGER NOT NULL,
  not_before  TEXT,
  not_after   TEXT,
  sans        TEXT NOT NULL,
  seen_at     TEXT NOT NULL,
  UNIQUE(log_id, cert_index)
);
CREATE INDEX IF NOT EXISTS idx_certs_seen ON certs(seen_at);

CREATE TABLE IF NOT EXISTS domains (
  domain            TEXT PRIMARY KEY,
  first_seen        TEXT NOT NULL,
  first_cert_index  INTEGER,
  first_log         TEXT,
  country           TEXT,
  last_cert_at      TEXT,
  -- Abrieb
  is_parked         INTEGER NOT NULL DEFAULT 0,
  http_status       INTEGER,
  final_url         TEXT,
  title             TEXT,
  meta_description  TEXT,
  lang              TEXT,
  page_hash         TEXT,
  text_len          INTEGER DEFAULT 0,
  fetched_at        TEXT,
  fetch_error       TEXT,
  -- Klassifikation
  category          TEXT,
  segment           TEXT,
  business_type     TEXT,
  sells_own_product INTEGER,
  confidence        REAL,
  summary_de        TEXT,
  classified_at     TEXT,
  classify_provider TEXT,
  classify_model    TEXT,
  -- Betreiber (DSGVO-bewusst minimal)
  company_name      TEXT,
  legal_form        TEXT,
  contact_url       TEXT,
  -- Änderungen
  last_change_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_domains_seen    ON domains(first_seen);
CREATE INDEX IF NOT EXISTS idx_domains_class   ON domains(category, segment);
CREATE INDEX IF NOT EXISTS idx_domains_country ON domains(country);

CREATE TABLE IF NOT EXISTS changes (
  id          INTEGER PRIMARY KEY,
  domain      TEXT NOT NULL REFERENCES domains(domain) ON DELETE CASCADE,
  kind        TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_domain ON changes(domain, detected_at);

CREATE TABLE IF NOT EXISTS page_cache (
  domain     TEXT PRIMARY KEY,
  html       TEXT,
  fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS settings (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS notes (
  id         INTEGER PRIMARY KEY,
  domain     TEXT NOT NULL,
  note       TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    """UTC-Zeitstempel in ISO 8601, sekundengenau."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    """Dünner SQLite-Wrapper mit WAL und Thread-localen Verbindungen."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._connect()
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self.transaction() as con:
            con.executescript(SCHEMA)
            con.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Transaktion mit Commit, bei Fehler Rollback."""
        conn = self.conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.cancel()
            raise

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ---------- Logs ----------

    def upsert_log(self, name: str, url: str) -> int:
        with self.transaction() as con:
            con.execute(
                """INSERT INTO logs(name, url) VALUES(?, ?)
                   ON CONFLICT(name) DO UPDATE SET url=excluded.url""",
                (name, url),
            )
            row = con.execute("SELECT id FROM logs WHERE name=?", (name,)).fetchone()
        return int(row["id"])

    def get_log_progress(self, log_name: str) -> tuple[int, str]:
        """Liefert (tree_size, state) eines Logs."""
        row = self.conn.execute(
            "SELECT tree_size, state FROM logs WHERE name=?", (log_name,)
        ).fetchone()
        if row is None:
            return 0, "active"
        return int(row["tree_size"]), str(row["state"])

    def set_log_progress(
        self, log_name: str, tree_size: int, error: str | None = None
    ) -> None:
        state = "error" if error else "active"
        with self.transaction() as con:
            con.execute(
                """UPDATE logs
                      SET tree_size=?, state=?, last_error=?, updated_at=?
                    WHERE name=?""",
                (tree_size, state, error, now_iso(), log_name),
            )

    # ---------- Zertifikate ----------

    def record_certs(self, log_id: int, rows: list[tuple[int, str, list[str], str, str]]) -> int:
        """Mehrere Zertifikate einfügen. Rückgabe: Anzahl neuer Zeilen."""
        with self.transaction() as con:
            before = con.total_changes
            con.executemany(
                """INSERT OR IGNORE INTO certs
                   (log_id, cert_index, not_before, not_after, sans, seen_at)
                   VALUES(?,?,?,?,?,?)""",
                rows,
            )
            return con.total_changes - before

    def first_cert_index(self, domain: str) -> int | None:
        row = self.conn.execute(
            """SELECT c.cert_index, c.seen_at, l.name
                 FROM certs c JOIN logs l ON l.id = c.log_id
                 JOIN json_each(c.sans) j ON j.value = ?
                ORDER BY c.seen_at ASC LIMIT 1""",
            (domain,),
        ).fetchone()
        if row is None:
            return None
        return int(row["cert_index"])

    # ---------- Domains ----------

    def insert_domains(self, entries: list[dict[str, Any]]) -> int:
        """Neue Basis-Domains einfügen, bestehende unverändert lassen.

        Rückgabe: Anzahl tatsächlich neuer Zeilen – damit kann der Collector
        melden, wie viel er gefunden hat.
        """
        if not entries:
            return 0
        stamp = now_iso()
        with self.transaction() as con:
            before = con.total_changes
            con.executemany(
                """INSERT OR IGNORE INTO domains
                   (domain, first_seen, first_cert_index, first_log)
                   VALUES(?,?,?,?)""",
                [
                    (
                        e["domain"],
                        e.get("first_seen", stamp),
                        e.get("cert_index"),
                        e.get("log_name"),
                    )
                    for e in entries
                ],
            )
            return con.total_changes - before

    def domains_without_fetch(self, limit: int = 200) -> list[str]:
        rows = self.conn.execute(
            """SELECT domain FROM domains
                WHERE http_status IS NULL AND is_parked = 0
                ORDER BY first_seen ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [r["domain"] for r in rows]

    def domains_without_classification(self, limit: int = 100) -> list[str]:
        rows = self.conn.execute(
            """SELECT domain FROM domains
                WHERE classified_at IS NULL
                  AND is_parked = 0
                  AND text_len > 400
                ORDER BY first_seen ASC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [r["domain"] for r in rows]

    def get_domain(self, domain: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM domains WHERE domain=?", (domain,)
        ).fetchone()

    def update_fetch(self, domain: str, **fields: Any) -> None:
        allowed = {
            "is_parked",
            "http_status",
            "final_url",
            "title",
            "meta_description",
            "lang",
            "page_hash",
            "text_len",
            "fetched_at",
            "fetch_error",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return
        clean["fetched_at"] = now_iso()
        assignments = ", ".join(f"{k}=?" for k in clean)
        with self.transaction() as con:
            con.execute(
                f"UPDATE domains SET {assignments} WHERE domain=?",
                (*clean.values(), domain),
            )

    def update_classification(self, domain: str, **fields: Any) -> None:
        allowed = {
            "category",
            "segment",
            "business_type",
            "sells_own_product",
            "confidence",
            "summary_de",
            "company_name",
            "legal_form",
            "contact_url",
            "classify_provider",
            "classify_model",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if not clean:
            return
        clean["classified_at"] = now_iso()
        assignments = ", ".join(f"{k}=?" for k in clean)
        with self.transaction() as con:
            con.execute(
                f"UPDATE domains SET {assignments} WHERE domain=?",
                (*clean.values(), domain),
            )

    def add_change(self, domain: str, kind: str, detail: str = "") -> None:
        with self.transaction() as con:
            con.execute(
                """INSERT INTO changes(domain, kind, detected_at, detail)
                   VALUES(?,?,?,?)""",
                (domain, kind, now_iso(), detail),
            )
            con.execute(
                "UPDATE domains SET last_change_at=? WHERE domain=?",
                (now_iso(), domain),
            )

    def cache_page(self, domain: str, html: str) -> None:
        with self.transaction() as con:
            con.execute(
                """INSERT INTO page_cache(domain, html, fetched_at) VALUES(?,?,?)
                   ON CONFLICT(domain) DO UPDATE SET
                     html=excluded.html, fetched_at=excluded.fetched_at""",
                (domain, html, now_iso()),
            )

    def get_cached_page(self, domain: str) -> str | None:
        row = self.conn.execute(
            "SELECT html FROM page_cache WHERE domain=?", (domain,)
        ).fetchone()
        return row["html"] if row else None

    # ---------- Lesen für API/Report ----------

    def search(
        self,
        *,
        view: str = "market",
        country: str | None = None,
        category: str | None = None,
        segment: str | None = None,
        q: str | None = None,
        days: int | None = None,
        limit: int = 50,
        offset: int = 0,
        sort: str = "newest",
    ) -> dict[str, Any]:
        """Gefilterte Liste für die API.

        `view="leads"` blendet geparkte Seiten aus, verlangt ein Produkt und
        sortiert nach einem erklärbaren Score.
        """
        where = ["1=1"]
        params: list[Any] = []

        if view == "leads":
            where.append("is_parked = 0")
            where.append("sells_own_product = 1")
            if segment:
                where.append("segment = ?")
                params.append(segment)
        else:
            if segment:
                where.append("segment = ?")
                params.append(segment)

        if country:
            where.append("country = ?")
            params.append(country)
        if category:
            where.append("category = ?")
            params.append(category)
        if q:
            where.append("(domain LIKE ? OR COALESCE(title,'') LIKE ?)")
            params.extend([f"%{q}%", f"%{q}%"])
        if days:
            where.append(f"first_seen >= datetime('now', '-{int(days)} days')")

        where_sql = " AND ".join(where)

        if sort == "oldest":
            order = "first_seen ASC"
        elif sort == "confidence" and view != "leads":
            order = "confidence DESC, first_seen DESC"
        else:
            order = "first_seen DESC"

        total = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM domains WHERE {where_sql}", params
        ).fetchone()["n"]

        rows = self.conn.execute(
            f"SELECT * FROM domains WHERE {where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()

        return {"total": total, "hits": [dict(r) for r in rows]}

    def stats(self) -> dict[str, Any]:
        """Kennzahlen für /stats."""

        def scalar(sql: str, params: tuple = ()) -> Any:
            row = self.conn.execute(sql, params).fetchone()
            if row is None:
                return None
            return list(row)[0]

        return {
            "total_domains": scalar("SELECT COUNT(*) FROM domains") or 0,
            "new_24h": scalar(
                "SELECT COUNT(*) FROM domains WHERE first_seen >= datetime('now','-1 day')"
            )
            or 0,
            "new_7d": scalar(
                "SELECT COUNT(*) FROM domains WHERE first_seen >= datetime('now','-7 day')"
            )
            or 0,
            "classified": scalar("SELECT COUNT(*) FROM domains WHERE classified_at IS NOT NULL")
            or 0,
            "parked": scalar("SELECT COUNT(*) FROM domains WHERE is_parked = 1") or 0,
            "with_product": scalar(
                "SELECT COUNT(*) FROM domains WHERE sells_own_product = 1"
            )
            or 0,
            "by_category": [
                dict(r)
                for r in self.conn.execute(
                    """SELECT category, COUNT(*) AS count FROM domains
                        WHERE category IS NOT NULL
                        GROUP BY category ORDER BY count DESC LIMIT 30"""
                ).fetchall()
            ],
            "by_country": [
                dict(r)
                for r in self.conn.execute(
                    """SELECT country, COUNT(*) AS count FROM domains
                        WHERE country IS NOT NULL
                        GROUP BY country ORDER BY count DESC LIMIT 20"""
                ).fetchall()
            ],
            "by_day": [
                dict(r)
                for r in self.conn.execute(
                    """SELECT substr(first_seen,1,10) AS day, COUNT(*) AS count
                         FROM domains
                        WHERE first_seen >= datetime('now','-30 days')
                        GROUP BY day ORDER BY day"""
                ).fetchall()
            ],
        }