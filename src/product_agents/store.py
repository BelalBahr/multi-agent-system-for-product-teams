"""The Product Context store, backed by SQLite.

Rules enforced here, not left to agents:
  * Evidence is immutable (database triggers reject UPDATE and DELETE).
  * Every write goes through a method that can be audited.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Evidence, Theme

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id TEXT PRIMARY KEY,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    segment TEXT NOT NULL,
    text TEXT NOT NULL,
    metric TEXT,
    kind TEXT NOT NULL DEFAULT 'text'
);
CREATE TRIGGER IF NOT EXISTS evidence_no_update BEFORE UPDATE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is immutable'); END;
CREATE TRIGGER IF NOT EXISTS evidence_no_delete BEFORE DELETE ON evidence
BEGIN SELECT RAISE(ABORT, 'evidence is immutable'); END;

CREATE TABLE IF NOT EXISTS themes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    confidence TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS theme_evidence (
    evidence_id TEXT PRIMARY KEY REFERENCES evidence(id),
    theme_id TEXT NOT NULL REFERENCES themes(id)
);
CREATE TABLE IF NOT EXISTS theme_quotes (
    theme_id TEXT NOT NULL REFERENCES themes(id),
    evidence_id TEXT NOT NULL REFERENCES evidence(id),
    quote TEXT NOT NULL,
    PRIMARY KEY (theme_id, evidence_id)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    agent TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS audit_log (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    run_id TEXT,
    action TEXT NOT NULL,
    record_type TEXT NOT NULL,
    record_id TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id TEXT PRIMARY KEY,
    theme_id TEXT NOT NULL REFERENCES themes(id),
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','rejected')),
    options TEXT NOT NULL,
    recommended TEXT NOT NULL,
    rationale TEXT NOT NULL,
    strategy_fit TEXT NOT NULL DEFAULT '',
    evidence_ids TEXT NOT NULL,
    assumption INTEGER NOT NULL DEFAULT 0,
    hypothesis TEXT NOT NULL DEFAULT '',
    metric_event TEXT,
    metric_direction TEXT,
    threshold_pct REAL,
    window_days INTEGER NOT NULL DEFAULT 14,
    review_days INTEGER NOT NULL DEFAULT 30,
    review_date TEXT,
    created_at TEXT NOT NULL,
    approved_by TEXT,
    approved_at TEXT,
    rejected_reason TEXT,
    redteam_waived INTEGER NOT NULL DEFAULT 0
);
CREATE TRIGGER IF NOT EXISTS decision_needs_evidence_upd
BEFORE UPDATE OF status ON decisions
WHEN NEW.status = 'approved' AND json_array_length(NEW.evidence_ids) = 0 AND NEW.assumption = 0
BEGIN SELECT RAISE(ABORT, 'a decision needs evidence or an explicit assumption flag to be approved'); END;
CREATE TRIGGER IF NOT EXISTS decision_needs_evidence_ins
BEFORE INSERT ON decisions
WHEN NEW.status = 'approved' AND json_array_length(NEW.evidence_ids) = 0 AND NEW.assumption = 0
BEGIN SELECT RAISE(ABORT, 'a decision needs evidence or an explicit assumption flag to be approved'); END;
CREATE TABLE IF NOT EXISTS dissents (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decisions(id),
    run_id TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS specs (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decisions(id),
    title TEXT NOT NULL,
    body_json TEXT NOT NULL,
    body_md TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','exported')),
    approved_by TEXT,
    approved_at TEXT,
    exported_url TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS updates (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('status','audience')),
    audience TEXT,
    source_id TEXT,
    title TEXT NOT NULL,
    body_md TEXT NOT NULL,
    facts TEXT NOT NULL,
    method TEXT NOT NULL CHECK (method IN ('model','template')),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','approved','exported')),
    approved_by TEXT,
    approved_at TEXT,
    exported_url TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS research_notes (
    id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL UNIQUE REFERENCES evidence(id),
    kind TEXT NOT NULL CHECK (kind IN ('interview','competitor')),
    summary TEXT NOT NULL,
    findings TEXT NOT NULL,
    dropped INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (
    id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL REFERENCES decisions(id),
    baseline REAL,
    current REAL,
    change_pct REAL,
    verdict TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ---- evidence -------------------------------------------------------

    def add_evidence(self, ev: Evidence) -> bool:
        """Insert evidence. Returns False if it already exists (idempotent)."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO evidence"
            "(id, source_type, source_id, source_url, timestamp, segment, text, metric, kind)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                ev.id,
                ev.source_type,
                ev.source_id,
                ev.source_url,
                ev.timestamp,
                ev.segment,
                ev.text,
                json.dumps(ev.metric) if ev.metric is not None else None,
                ev.kind,
            ),
        )
        self.conn.commit()
        return cur.rowcount == 1

    @staticmethod
    def _ev(row: sqlite3.Row) -> Evidence:
        return Evidence(
            id=row["id"],
            source_type=row["source_type"],
            source_id=row["source_id"],
            source_url=row["source_url"],
            timestamp=row["timestamp"],
            segment=row["segment"],
            text=row["text"],
            metric=json.loads(row["metric"]) if row["metric"] else None,
            kind=row["kind"],
        )

    def get_evidence(self, ids: list[str]) -> list[Evidence]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self.conn.execute(
            f"SELECT * FROM evidence WHERE id IN ({marks}) ORDER BY timestamp", ids
        ).fetchall()
        return [self._ev(r) for r in rows]

    def unassigned_evidence(self, limit: int) -> list[Evidence]:
        rows = self.conn.execute(
            "SELECT e.* FROM evidence e LEFT JOIN theme_evidence te ON te.evidence_id = e.id"
            " WHERE te.evidence_id IS NULL AND e.kind = 'text' ORDER BY e.timestamp, e.id LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._ev(r) for r in rows]

    def count_evidence(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]

    def count_unassigned(self) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM evidence e LEFT JOIN theme_evidence te"
            " ON te.evidence_id = e.id WHERE te.evidence_id IS NULL AND e.kind = 'text'"
        ).fetchone()[0]

    def evidence_sources(self) -> dict[str, int]:
        rows = self.conn.execute(
            "SELECT source_type, COUNT(*) AS n FROM evidence GROUP BY source_type"
        ).fetchall()
        return {r["source_type"]: r["n"] for r in rows}

    # ---- themes ---------------------------------------------------------

    @staticmethod
    def _theme(row: sqlite3.Row) -> Theme:
        return Theme(
            id=row["id"],
            title=row["title"],
            summary=row["summary"],
            confidence=row["confidence"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def list_themes(self) -> list[Theme]:
        rows = self.conn.execute("SELECT * FROM themes ORDER BY created_at, id").fetchall()
        return [self._theme(r) for r in rows]

    def get_theme(self, theme_id: str) -> Theme | None:
        row = self.conn.execute("SELECT * FROM themes WHERE id = ?", (theme_id,)).fetchone()
        return self._theme(row) if row else None

    def find_theme_by_title(self, title: str) -> Theme | None:
        row = self.conn.execute(
            "SELECT * FROM themes WHERE lower(title) = lower(?)", (title.strip(),)
        ).fetchone()
        return self._theme(row) if row else None

    def create_theme(self, theme_id: str, title: str, summary: str) -> Theme:
        now = utcnow()
        self.conn.execute(
            "INSERT INTO themes(id, title, summary, confidence, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?)",
            (theme_id, title.strip(), summary.strip(), "low", now, now),
        )
        self.conn.commit()
        return self.get_theme(theme_id)  # type: ignore[return-value]

    def assign_evidence(self, evidence_id: str, theme_id: str) -> bool:
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO theme_evidence(evidence_id, theme_id) VALUES (?,?)",
            (evidence_id, theme_id),
        )
        self.conn.execute("UPDATE themes SET updated_at = ? WHERE id = ?", (utcnow(), theme_id))
        self.conn.commit()
        return cur.rowcount == 1

    def add_quote(self, theme_id: str, evidence_id: str, quote: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO theme_quotes(theme_id, evidence_id, quote) VALUES (?,?,?)",
            (theme_id, evidence_id, quote),
        )
        self.conn.commit()

    def assignments(self) -> dict[str, str]:
        """evidence id -> theme id. Unscoped: for the trusted evaluation harness only."""
        rows = self.conn.execute("SELECT evidence_id, theme_id FROM theme_evidence").fetchall()
        return {r["evidence_id"]: r["theme_id"] for r in rows}

    def set_confidence(self, theme_id: str, confidence: str) -> None:
        self.conn.execute(
            "UPDATE themes SET confidence = ?, updated_at = ? WHERE id = ?",
            (confidence, utcnow(), theme_id),
        )
        self.conn.commit()

    def theme_evidence(self, theme_id: str) -> list[Evidence]:
        rows = self.conn.execute(
            "SELECT e.* FROM evidence e JOIN theme_evidence te ON te.evidence_id = e.id"
            " WHERE te.theme_id = ? ORDER BY e.timestamp, e.id",
            (theme_id,),
        ).fetchall()
        return [self._ev(r) for r in rows]

    def theme_quotes(self, theme_id: str) -> list[tuple[str, Evidence]]:
        rows = self.conn.execute(
            "SELECT q.quote AS quote, e.* FROM theme_quotes q JOIN evidence e"
            " ON e.id = q.evidence_id WHERE q.theme_id = ? ORDER BY e.timestamp, e.id",
            (theme_id,),
        ).fetchall()
        return [(r["quote"], self._ev(r)) for r in rows]

    def evidence_of_kind(self, theme_id: str, kind: str) -> list[Evidence]:
        return [e for e in self.theme_evidence(theme_id) if e.kind == kind]

    # ---- strategy -------------------------------------------------------

    def set_strategy(self, body: str, by: str) -> None:
        self.conn.execute(
            "INSERT INTO strategy(body, updated_at, updated_by) VALUES (?,?,?)",
            (body, utcnow(), by),
        )
        self.conn.commit()

    def get_strategy(self) -> str:
        row = self.conn.execute("SELECT body FROM strategy ORDER BY id DESC LIMIT 1").fetchone()
        return row["body"] if row else ""

    # ---- decisions ------------------------------------------------------

    def create_decision(self, d: dict) -> None:
        self.conn.execute(
            "INSERT INTO decisions(id, theme_id, title, status, options, recommended, rationale,"
            " strategy_fit, evidence_ids, assumption, hypothesis, metric_event, metric_direction,"
            " threshold_pct, window_days, review_days, created_at)"
            " VALUES (?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                d["id"], d["theme_id"], d["title"], json.dumps(d["options"]), d["recommended"],
                d["rationale"], d.get("strategy_fit", ""), json.dumps(d["evidence_ids"]),
                1 if d.get("assumption") else 0, d.get("hypothesis", ""), d.get("metric_event"),
                d.get("metric_direction"), d.get("threshold_pct"), d.get("window_days", 14),
                d.get("review_days", 30), utcnow(),
            ),
        )
        self.conn.commit()

    @staticmethod
    def _decision(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["options"] = json.loads(d["options"])
        d["evidence_ids"] = json.loads(d["evidence_ids"])
        return d

    def get_decision(self, decision_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM decisions WHERE id = ?", (decision_id,)).fetchone()
        return self._decision(row) if row else None

    def list_decisions(self, status: str | None = None) -> list[dict]:
        if status:
            rows = self.conn.execute(
                "SELECT * FROM decisions WHERE status = ? ORDER BY created_at, id", (status,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM decisions ORDER BY created_at, id").fetchall()
        return [self._decision(r) for r in rows]

    def theme_has_open_decision(self, theme_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM decisions WHERE theme_id = ? AND status IN ('draft','approved') LIMIT 1",
            (theme_id,),
        ).fetchone()
        return row is not None

    def add_dissent(self, dissent_id: str, decision_id: str, run_id: str | None, body: dict) -> None:
        self.conn.execute(
            "INSERT INTO dissents(id, decision_id, run_id, body, created_at) VALUES (?,?,?,?,?)",
            (dissent_id, decision_id, run_id, json.dumps(body), utcnow()),
        )
        self.conn.commit()

    def get_dissents(self, decision_id: str) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM dissents WHERE decision_id = ? ORDER BY created_at, id", (decision_id,)
        ).fetchall()
        return [{**dict(r), "body": json.loads(r["body"])} for r in rows]

    def decision_set_approved(
        self, decision_id: str, by: str, review_date: str, assumption: bool, waived: bool
    ) -> None:
        self.conn.execute(
            "UPDATE decisions SET status = 'approved', approved_by = ?, approved_at = ?,"
            " review_date = ?, assumption = MAX(assumption, ?), redteam_waived = ? WHERE id = ?",
            (by, utcnow(), review_date, 1 if assumption else 0, 1 if waived else 0, decision_id),
        )
        self.conn.commit()

    def decision_set_rejected(self, decision_id: str, by: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE decisions SET status = 'rejected', approved_by = ?, approved_at = ?,"
            " rejected_reason = ? WHERE id = ?",
            (by, utcnow(), reason, decision_id),
        )
        self.conn.commit()

    # ---- specs ----------------------------------------------------------

    def create_spec(self, spec_id: str, decision_id: str, title: str, body: dict, md: str) -> None:
        self.conn.execute(
            "INSERT INTO specs(id, decision_id, title, body_json, body_md, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (spec_id, decision_id, title, json.dumps(body), md, utcnow()),
        )
        self.conn.commit()

    def get_spec(self, spec_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM specs WHERE id = ?", (spec_id,)).fetchone()
        return dict(row) if row else None

    def list_specs(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM specs ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    def spec_for_decision(self, decision_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM specs WHERE decision_id = ? ORDER BY created_at DESC LIMIT 1",
            (decision_id,),
        ).fetchone()
        return dict(row) if row else None

    def spec_set_approved(self, spec_id: str, by: str) -> None:
        self.conn.execute(
            "UPDATE specs SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
            (by, utcnow(), spec_id),
        )
        self.conn.commit()

    def spec_set_exported(self, spec_id: str, url: str) -> None:
        self.conn.execute(
            "UPDATE specs SET status = 'exported', exported_url = ? WHERE id = ?", (url, spec_id)
        )
        self.conn.commit()

    # ---- updates (status and audience messages) -------------------------

    def create_update(self, u: dict) -> None:
        self.conn.execute(
            "INSERT INTO updates(id, kind, audience, source_id, title, body_md, facts, method, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (u["id"], u["kind"], u.get("audience"), u.get("source_id"), u["title"], u["body_md"],
             json.dumps(u["facts"]), u["method"], utcnow()),
        )
        self.conn.commit()

    @staticmethod
    def _update(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["facts"] = json.loads(d["facts"])
        return d

    def get_update(self, update_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM updates WHERE id = ?", (update_id,)).fetchone()
        return self._update(row) if row else None

    def list_updates(self, kind: str | None = None) -> list[dict]:
        if kind:
            rows = self.conn.execute(
                "SELECT * FROM updates WHERE kind = ? ORDER BY created_at, id", (kind,)
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM updates ORDER BY created_at, id").fetchall()
        return [self._update(r) for r in rows]

    def update_set_approved(self, update_id: str, by: str) -> None:
        self.conn.execute(
            "UPDATE updates SET status = 'approved', approved_by = ?, approved_at = ? WHERE id = ?",
            (by, utcnow(), update_id),
        )
        self.conn.commit()

    def update_set_exported(self, update_id: str, url: str) -> None:
        self.conn.execute(
            "UPDATE updates SET status = 'exported', exported_url = ? WHERE id = ?", (url, update_id)
        )
        self.conn.commit()

    # ---- research notes -------------------------------------------------

    def unresearched_evidence(self, source_types: tuple[str, ...], limit: int = 50) -> list[Evidence]:
        marks = ",".join("?" for _ in source_types)
        rows = self.conn.execute(
            f"SELECT e.* FROM evidence e LEFT JOIN research_notes r ON r.evidence_id = e.id"
            f" WHERE r.id IS NULL AND e.kind = 'text' AND e.source_type IN ({marks})"
            f" ORDER BY e.timestamp, e.id LIMIT ?",
            (*source_types, limit),
        ).fetchall()
        return [self._ev(r) for r in rows]

    def create_research_note(self, n: dict) -> None:
        self.conn.execute(
            "INSERT INTO research_notes(id, evidence_id, kind, summary, findings, dropped, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (n["id"], n["evidence_id"], n["kind"], n["summary"], json.dumps(n["findings"]),
             n.get("dropped", 0), utcnow()),
        )
        self.conn.commit()

    def list_research_notes(self, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM research_notes" + (" WHERE kind = ?" if kind else "") + " ORDER BY created_at, id"
        rows = self.conn.execute(sql, (kind,) if kind else ()).fetchall()
        return [{**dict(r), "findings": json.loads(r["findings"])} for r in rows]

    def get_research_note(self, note_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM research_notes WHERE id = ?", (note_id,)).fetchone()
        return {**dict(row), "findings": json.loads(row["findings"])} if row else None

    # ---- outcomes -------------------------------------------------------

    def add_outcome(self, o: dict) -> None:
        self.conn.execute(
            "INSERT INTO outcomes(id, decision_id, baseline, current, change_pct, verdict, detail,"
            " created_at) VALUES (?,?,?,?,?,?,?,?)",
            (o["id"], o["decision_id"], o.get("baseline"), o.get("current"), o.get("change_pct"),
             o["verdict"], o["detail"], utcnow()),
        )
        self.conn.commit()

    def list_outcomes(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM outcomes ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    def outcome_for_decision(self, decision_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM outcomes WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        return dict(row) if row else None

    # ---- runs, audit, meta ---------------------------------------------

    def start_run(self, run_id: str, agent: str) -> None:
        self.conn.execute(
            "INSERT INTO runs(id, agent, started_at) VALUES (?,?,?)", (run_id, agent, utcnow())
        )
        self.conn.commit()

    def finish_run(self, run_id: str, tokens: int, status: str) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, tokens = ?, status = ? WHERE id = ?",
            (utcnow(), tokens, status, run_id),
        )
        self.conn.commit()

    def tokens_used_since(self, iso_ts: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM runs WHERE started_at >= ?", (iso_ts,)
        ).fetchone()
        return int(row[0])

    def log(
        self,
        actor: str,
        action: str,
        record_type: str,
        record_id: str | None = None,
        detail: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO audit_log(at, actor, run_id, action, record_type, record_id, detail)"
            " VALUES (?,?,?,?,?,?,?)",
            (utcnow(), actor, run_id, action, record_type, record_id, detail),
        )
        self.conn.commit()

    def audit(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM audit_log ORDER BY seq DESC LIMIT ?", (limit,)
        ).fetchall()

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, value))
        self.conn.commit()
