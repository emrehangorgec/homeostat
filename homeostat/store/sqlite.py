"""SQLite persistence for incidents, actions and evaluation runs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from homeostat.policy.engine import PastAction

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id                TEXT PRIMARY KEY,
    run_id            TEXT,
    detected_at       REAL NOT NULL,
    closed_at         REAL,
    symptoms          TEXT NOT NULL,
    incident_type     TEXT,
    rule_id           TEXT,
    outcome           TEXT,
    verify_strength   TEXT,
    attempts          INTEGER NOT NULL DEFAULT 0,
    llm_calls         INTEGER NOT NULL DEFAULT 0,
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd          REAL NOT NULL DEFAULT 0,
    schema_version    TEXT NOT NULL,
    first_state       TEXT NOT NULL,
    final_state       TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    at          REAL NOT NULL,
    proposed    TEXT NOT NULL,
    action      TEXT NOT NULL,
    source      TEXT NOT NULL,
    verdict     TEXT NOT NULL,
    reason      TEXT NOT NULL,
    ok          INTEGER,
    duration_s  REAL,
    verify      TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id                  TEXT PRIMARY KEY,
    experiment_id       TEXT NOT NULL,
    scenario_id         TEXT NOT NULL,
    category            TEXT NOT NULL,
    arm                 TEXT NOT NULL,
    injected_at         REAL NOT NULL,
    incident_id         TEXT,
    outcome             TEXT NOT NULL,
    detection_latency_s REAL,
    time_to_recovery_s  REAL,
    notes               TEXT,
    max_impact          INTEGER,
    excess_actions      INTEGER NOT NULL DEFAULT 0,
    correct             INTEGER,
    llm_calls           INTEGER NOT NULL DEFAULT 0,
    cost_usd            REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS diagnoses (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id        TEXT NOT NULL,
    at                 REAL NOT NULL,
    model              TEXT NOT NULL,
    served_by          TEXT,
    diagnosis          TEXT,
    confidence         REAL,
    abstain            INTEGER,
    proposed_action    TEXT,
    explanation        TEXT,
    error              TEXT,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd           REAL NOT NULL DEFAULT 0,
    latency_s          REAL,
    executed_action    TEXT,
    recovered_after    INTEGER
);
CREATE INDEX IF NOT EXISTS actions_at ON actions(at);
CREATE INDEX IF NOT EXISTS runs_experiment ON runs(experiment_id);
"""


class Store:
    def __init__(self, path: str | Path = "homeostat.sqlite"):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a store was created (M2: disruption metrics)."""
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(runs)")}
        with self.conn:
            if "max_impact" not in columns:
                self.conn.execute("ALTER TABLE runs ADD COLUMN max_impact INTEGER")
            if "excess_actions" not in columns:
                self.conn.execute("ALTER TABLE runs ADD COLUMN excess_actions INTEGER NOT NULL DEFAULT 0")
            if "correct" not in columns:  # M3: correctness replaces "recovered" as the headline
                self.conn.execute("ALTER TABLE runs ADD COLUMN correct INTEGER")
            if "llm_calls" not in columns:
                self.conn.execute("ALTER TABLE runs ADD COLUMN llm_calls INTEGER NOT NULL DEFAULT 0")
            if "cost_usd" not in columns:
                self.conn.execute("ALTER TABLE runs ADD COLUMN cost_usd REAL NOT NULL DEFAULT 0")

    def close(self) -> None:
        self.conn.close()

    def _insert(self, table: str, row: dict[str, Any]) -> None:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        values = [json.dumps(v) if isinstance(v, (list, dict)) else v for v in row.values()]
        with self.conn:
            self.conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", values)

    def save_incident(self, row: dict[str, Any]) -> None:
        self._insert("incidents", row)

    def save_action(self, row: dict[str, Any]) -> None:
        self._insert("actions", row)

    def save_run(self, row: dict[str, Any]) -> None:
        self._insert("runs", row)

    def save_diagnosis(self, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.conn:
            cursor = self.conn.execute(f"INSERT INTO diagnoses ({cols}) VALUES ({marks})", list(row.values()))
        return int(cursor.lastrowid)

    def settle_diagnosis(self, diagnosis_id: int, executed_action: str, recovered_after: bool) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE diagnoses SET executed_action = ?, recovered_after = ? WHERE id = ?",
                (executed_action, int(recovered_after), diagnosis_id),
            )

    def diagnoses_for(self, incident_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM diagnoses WHERE incident_id = ? ORDER BY id", (incident_id,))
        return [dict(r) for r in rows]

    def recent_incidents(self, before: float, limit: int = 5) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT incident_type, outcome, closed_at FROM incidents WHERE closed_at < ? "
            "ORDER BY closed_at DESC LIMIT ?",
            (before, limit),
        )
        return [dict(r) for r in rows]

    def action_history(self, since: float) -> list[PastAction]:
        rows = self.conn.execute(
            "SELECT action, at, incident_id FROM actions WHERE at >= ? AND verdict IN ('allow', 'substitute')",
            (since,),
        ).fetchall()
        return [PastAction(r["action"], r["at"], r["incident_id"]) for r in rows]

    def incident(self, incident_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return dict(row) if row else None

    def actions_for(self, incident_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM actions WHERE incident_id = ? ORDER BY id", (incident_id,))
        return [dict(r) for r in rows]

    def runs(self, experiment_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runs WHERE experiment_id = ? ORDER BY injected_at", (experiment_id,))
        return [dict(r) for r in rows]
