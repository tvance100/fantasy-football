"""Prediction log: a SQLite record of every recommendation the tool makes.

Why this exists: the tool can only be evaluated (and later improved) if what it
*said* at send time is kept. Everything is a snapshot taken when the email goes
out -- never recomputed later -- so grading never benefits from hindsight.

Tables
------
lineups      one row per (season, week, window, league): slot counts, projected total
predictions  one row per (season, week, window, league, player): ESPN vs our
             projection, every multiplier applied, the projected stat line, slots
actuals      one row per (season, week, league, player): what actually happened
             (filled by evaluate.py from ESPN box scores)

`provenance` is "live" for rows logged at send time and "backfill" for rows
reconstructed from old report HTML (see backfill.py). Backfilled rows have no
factor / stat-line detail.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from .projections import MODEL_VERSION

ROOT = Path(__file__).resolve().parents[2]
# FFBALL_TRACKING_DB overrides the location (e.g. a path on a filesystem SQLite likes).
DB_PATH = Path(os.getenv("FFBALL_TRACKING_DB") or ROOT / ".state" / "tracking.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lineups (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    window TEXT NOT NULL,
    league_id TEXT NOT NULL,
    league_name TEXT,
    scoring_type TEXT,
    slot_counts_json TEXT,
    projected_total REAL,
    sent_at TEXT,
    model_version TEXT,
    provenance TEXT NOT NULL DEFAULT 'live',
    PRIMARY KEY (season, week, window, league_id)
);

CREATE TABLE IF NOT EXISTS predictions (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    window TEXT NOT NULL,
    league_id TEXT NOT NULL,
    name_key TEXT NOT NULL,
    player_id INTEGER,
    name TEXT,
    position TEXT,
    pro_team TEXT,
    opponent TEXT,
    on_bye INTEGER NOT NULL DEFAULT 0,
    espn_points REAL,
    our_points REAL,
    factors_json TEXT,
    flags_json TEXT,
    stat_line_json TEXT,
    espn_stat_line_json TEXT,
    eligible_json TEXT,
    user_slot TEXT,
    rec_slot TEXT,
    in_window INTEGER NOT NULL DEFAULT 0,
    kickoff_ts TEXT,
    sent_at TEXT,
    model_version TEXT,
    provenance TEXT NOT NULL DEFAULT 'live',
    PRIMARY KEY (season, week, window, league_id, name_key)
);

CREATE TABLE IF NOT EXISTS actuals (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    league_id TEXT NOT NULL,
    name_key TEXT NOT NULL,
    player_id INTEGER,
    name TEXT,
    position TEXT,
    pro_team TEXT,
    actual_points REAL,
    final_slot TEXT,
    on_bye INTEGER NOT NULL DEFAULT 0,
    game_complete INTEGER NOT NULL DEFAULT 1,
    fetched_at TEXT,
    PRIMARY KEY (season, week, league_id, name_key)
);

CREATE INDEX IF NOT EXISTS idx_pred_week ON predictions (season, week, league_id);
"""

START_EXCLUDED = ("BE", "IR", "", None)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    if str(p) != ":memory:":
        p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _iso(dt) -> str | None:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _norm_key(name: str) -> str:
    from .shared_data import norm_name

    return norm_name(name)


def log_lineup(
    conn: sqlite3.Connection,
    *,
    season: int,
    week: int,
    window,                      # slate.SendWindow | None
    cfg,                         # config.LeagueConfig
    projections: list,           # list[PlayerProjection]
    lineup,                      # startsit.Lineup
    sent_at: datetime,
    model_version: str = MODEL_VERSION,
) -> int:
    """Snapshot one league's recommendation. Returns rows written."""
    window_key = window.key if window else "full"
    rec_slot = {id(p): slot for slot, p in lineup.starters}
    sent = _iso(sent_at)

    conn.execute(
        "INSERT OR REPLACE INTO lineups VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            season, week, window_key, str(cfg.league_id), cfg.league_name,
            cfg.scoring_type, json.dumps(cfg.roster_slots),
            lineup.projected_total, sent, model_version, "live",
        ),
    )

    n = 0
    for p in projections:
        in_window = 1 if (window is None or p.pro_team in window.teams) else 0
        conn.execute(
            "INSERT OR REPLACE INTO predictions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                season, week, window_key, str(cfg.league_id), _norm_key(p.name),
                int(p.player_id or 0), p.name, p.position, p.pro_team, p.opponent,
                1 if p.on_bye else 0, p.espn_points, p.our_points,
                json.dumps(p.factors), json.dumps(p.flags),
                json.dumps(p.stat_line), json.dumps(p.espn_stat_line),
                json.dumps(list(p.eligible_slots)),
                p.current_slot, rec_slot.get(id(p), "BE"),
                in_window, _iso(p.kickoff), sent, model_version, "live",
            ),
        )
        n += 1
    conn.commit()
    return n


def log_run(conn, *, season, week, window, now, results, model_version=MODEL_VERSION) -> int:
    """Log every league from one email. `results` = [(cfg, ld, projections, lineup), ...]."""
    total = 0
    for cfg, _ld, projections, lineup in results:
        total += log_lineup(
            conn, season=season, week=week, window=window, cfg=cfg,
            projections=projections, lineup=lineup, sent_at=now,
            model_version=model_version,
        )
    return total


# --------------------------------------------------------------------------- #
# reading snapshots back
# --------------------------------------------------------------------------- #
def decision_rows(conn, season: int, week: int) -> list[sqlite3.Row]:
    """The prediction that governed each player's decision, one per (league, player).

    That is the row from the email whose window the player locked in
    (`in_window=1`) -- the last word before kickoff. If a player never appears
    as in-window (e.g. kickoff unknown), fall back to the latest snapshot.
    """
    rows = conn.execute(
        "SELECT * FROM predictions WHERE season=? AND week=? ORDER BY sent_at, window",
        (season, week),
    ).fetchall()
    best: dict[tuple, sqlite3.Row] = {}
    for r in rows:
        key = (r["league_id"], r["name_key"])
        cur = best.get(key)
        if cur is None:
            best[key] = r
            continue
        # prefer in-window rows; among equals prefer the later snapshot
        if (r["in_window"], r["sent_at"] or "") >= (cur["in_window"], cur["sent_at"] or ""):
            best[key] = r
    return list(best.values())


def latest_lineup_meta(conn, season: int, week: int) -> dict[str, sqlite3.Row]:
    """league_id -> most recent lineups row for that league-week."""
    out: dict[str, sqlite3.Row] = {}
    for r in conn.execute(
        "SELECT * FROM lineups WHERE season=? AND week=? ORDER BY sent_at", (season, week)
    ):
        out[r["league_id"]] = r
    return out


def logged_weeks(conn, season: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT week FROM predictions WHERE season=? ORDER BY week", (season,))]
