"""SQLite persistence: routing decisions, delegated-run outcomes, learned weights."""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    session     TEXT,
    project     TEXT,
    prompt      TEXT,
    tokens      TEXT NOT NULL,          -- JSON list of content words
    heuristic   TEXT,                   -- tier suggested by the rules alone
    tier        TEXT NOT NULL,          -- tier actually chosen
    reason      TEXT,
    final_tier  TEXT,                   -- highest tier that ran for this task
    escalations INTEGER NOT NULL DEFAULT 0,
    overkill    INTEGER NOT NULL DEFAULT 0,
    cost        REAL,                   -- weighted tokens, set when closed
    closed      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_decisions_session ON decisions(session, ts);
CREATE INDEX IF NOT EXISTS idx_decisions_closed ON decisions(closed, ts);

CREATE TABLE IF NOT EXISTS outcomes (
    id          INTEGER PRIMARY KEY,
    decision_id INTEGER REFERENCES decisions(id),
    ts          REAL NOT NULL,
    agent       TEXT NOT NULL,
    tokens      INTEGER,
    tool_uses   INTEGER,
    duration_ms INTEGER,
    escalated   INTEGER NOT NULL DEFAULT 0,
    cost        REAL
);

CREATE TABLE IF NOT EXISTS running (
    id         TEXT PRIMARY KEY,        -- tool_use_id when available
    session    TEXT,
    agent      TEXT NOT NULL,
    ts         REAL NOT NULL,
    background INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS weights (
    token    TEXT PRIMARY KEY,
    w        REAL NOT NULL,
    evidence INTEGER NOT NULL DEFAULT 0,
    updated  REAL NOT NULL
);
"""

# A decision older than this is not linked to new delegated runs.
LINK_WINDOW_S = 45 * 60

# A decision is settled (usable for learning) once superseded or old enough.
SETTLED = "(closed=1 OR ts<?)"


def db_path() -> Path:
    return config.home() / "router.db"


@contextmanager
def connect(path: Path | None = None):
    path = path or db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_decision(conn, *, session, project, prompt, tokens, heuristic, tier, reason) -> int:
    cur = conn.execute(
        "INSERT INTO decisions (ts, session, project, prompt, tokens, heuristic, tier, reason, final_tier)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (time.time(), session, project, prompt, json.dumps(tokens), heuristic, tier, reason, tier))
    return cur.lastrowid


def open_decision(conn, session):
    """Latest still-open decision of a session, if recent enough."""
    return conn.execute(
        "SELECT * FROM decisions WHERE session=? AND closed=0 AND ts>? ORDER BY ts DESC LIMIT 1",
        (session, time.time() - LINK_WINDOW_S)).fetchone()


def close_open_decisions(conn, session, before_id=None):
    """Close decisions a new prompt has superseded (the user moved on)."""
    q = "UPDATE decisions SET closed=1 WHERE session=? AND closed=0"
    args = [session]
    if before_id is not None:
        q += " AND id<?"
        args.append(before_id)
    conn.execute(q, args)


def history(conn, limit):
    return conn.execute(
        "SELECT id, project, tokens, tier, final_tier, escalations, overkill, cost FROM decisions"
        f" WHERE {SETTLED} ORDER BY ts DESC LIMIT ?", (time.time() - LINK_WINDOW_S, limit)).fetchall()


def agent_cost_means(conn):
    """Mean cost of successful (non-escalated) delegated runs, per agent tier."""
    rows = conn.execute(
        "SELECT agent, AVG(cost) AS c, COUNT(*) AS n FROM outcomes"
        " WHERE escalated=0 AND cost IS NOT NULL GROUP BY agent").fetchall()
    return {r["agent"]: (r["c"], r["n"]) for r in rows}


def today_tier_share(conn, tier):
    start = time.time() - 24 * 3600
    row = conn.execute(
        "SELECT SUM(tier=?) AS t, COUNT(*) AS n FROM decisions WHERE ts>?", (tier, start)).fetchone()
    n = row["n"] or 0
    return ((row["t"] or 0) / n if n else 0.0), n


def get_weights(conn, tokens):
    if not tokens:
        return {}
    marks = ",".join("?" * len(tokens))
    rows = conn.execute(f"SELECT * FROM weights WHERE token IN ({marks})", list(tokens)).fetchall()
    return {r["token"]: r for r in rows}


def upsert_weight(conn, token, w, evidence):
    conn.execute(
        "INSERT INTO weights (token, w, evidence, updated) VALUES (?,?,?,?)"
        " ON CONFLICT(token) DO UPDATE SET w=excluded.w, evidence=excluded.evidence,"
        " updated=excluded.updated", (token, w, evidence, time.time()))


# A subagent still "running" after this long is assumed lost (crash, cancel).
RUNNING_STALE_S = 30 * 60


def start_run(conn, run_id, session, agent, background):
    conn.execute("INSERT OR REPLACE INTO running (id, session, agent, ts, background) VALUES (?,?,?,?,?)",
                 (run_id, session, agent, time.time(), int(background)))


def end_run(conn, run_id=None, session=None, agent=None, background_only=False):
    """Remove a running entry by id, else the oldest matching one for the session."""
    if run_id and conn.execute("DELETE FROM running WHERE id=?", (run_id,)).rowcount:
        return
    q = "SELECT id FROM running WHERE session IS ?"
    args = [session]
    if agent:
        q += " AND agent=?"
        args.append(agent)
    if background_only:
        q += " AND background=1"
    row = conn.execute(q + " ORDER BY ts LIMIT 1", args).fetchone()
    if row:
        conn.execute("DELETE FROM running WHERE id=?", (row["id"],))


def running_agents(conn):
    conn.execute("DELETE FROM running WHERE ts<?", (time.time() - RUNNING_STALE_S,))
    return [r["agent"] for r in conn.execute("SELECT agent FROM running ORDER BY ts").fetchall()]


def tier_shares(conn, since):
    rows = conn.execute("SELECT tier, COUNT(*) AS n FROM decisions WHERE ts>? GROUP BY tier",
                        (since,)).fetchall()
    return {r["tier"]: r["n"] for r in rows}


def last_decision(conn, session=None):
    if session:
        row = conn.execute("SELECT * FROM decisions WHERE session=? ORDER BY ts DESC LIMIT 1",
                           (session,)).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM decisions ORDER BY ts DESC LIMIT 1").fetchone()
