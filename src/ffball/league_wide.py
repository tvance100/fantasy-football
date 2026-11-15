"""League-wide prediction log: grade the model on every roster, not just yours.

Your own two rosters give ~25 graded projections a week, too few to tell a real
edge from noise. Each time a game-day email goes out, this module also projects
every rostered player in both leagues (every team, starters and bench) and logs
our number next to ESPN's. After the week, it stores what each player actually
scored, and the Tuesday scorecard grades the whole sample.

Rules that keep the grading honest:
  * Only players whose game has NOT kicked off are logged, so no snapshot ever
    sees a result. The decision-of-record for a player is the latest pre-kickoff
    snapshot taken in his own game day's window.
  * Nothing here sends email or changes recommendations; failures are logged
    and swallowed so they can never block the start/sit emails.
  * Stored in separate tables, so the "did the tool's lineup beat ESPN's"
    numbers for your own teams are unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime

from . import tracking
from .projections import MODEL_VERSION

LW_SCHEMA = """
CREATE TABLE IF NOT EXISTS lw_predictions (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    window TEXT NOT NULL,
    league_id TEXT NOT NULL,
    team_id INTEGER,
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
    slot TEXT,
    in_window INTEGER NOT NULL DEFAULT 0,
    kickoff_ts TEXT,
    sent_at TEXT,
    model_version TEXT,
    PRIMARY KEY (season, week, window, league_id, name_key)
);

CREATE TABLE IF NOT EXISTS lw_actuals (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    league_id TEXT NOT NULL,
    name_key TEXT NOT NULL,
    team_id INTEGER,
    name TEXT,
    position TEXT,
    actual_points REAL,
    on_bye INTEGER NOT NULL DEFAULT 0,
    game_complete INTEGER NOT NULL DEFAULT 1,
    fetched_at TEXT,
    PRIMARY KEY (season, week, league_id, name_key)
);

CREATE INDEX IF NOT EXISTS idx_lw_pred_week ON lw_predictions (season, week, league_id);
"""


def connect(path=None):
    conn = tracking.connect(path)
    conn.executescript(LW_SCHEMA)
    return conn


# --------------------------------------------------------------------------- #
# logging (at send time)
# --------------------------------------------------------------------------- #
def log_team(conn, *, season, week, window, league_id, team_id, projections, now,
             model_version=MODEL_VERSION) -> int:
    """Log one team's projections. Players already kicked off are skipped."""
    sent = now.isoformat()
    n = 0
    for p in projections:
        ko = p.kickoff
        if ko is not None and ko <= now:
            continue
        in_window = 1 if (window is not None and p.pro_team in window.teams) else 0
        conn.execute(
            "INSERT OR REPLACE INTO lw_predictions VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                season, week, window.key if window else "full", str(league_id), int(team_id),
                tracking._norm_key(p.name), int(p.player_id or 0), p.name, p.position,
                p.pro_team, p.opponent, 1 if p.on_bye else 0,
                p.espn_points, p.our_points, json.dumps(p.factors), p.current_slot,
                in_window, ko.isoformat() if ko is not None else None, sent, model_version,
            ),
        )
        n += 1
    return n


def snapshot(settings, sd, season, week, window, now, conn=None) -> int:
    """Project and log every rostered player in every configured league."""
    from .espn import load_all_teams
    from .projections import project_roster

    conn = conn or connect()
    total = 0
    for cfg in settings.leagues:
        try:
            teams = load_all_teams(cfg, season, week, settings.espn_s2, settings.swid)
            n = 0
            for team_id, ld in teams.items():
                n += log_team(conn, season=season, week=week, window=window,
                              league_id=cfg.league_id, team_id=team_id,
                              projections=project_roster(ld, sd), now=now)
            conn.commit()
            print(f"  [{cfg.league_name}] league-wide log: {n} players across {len(teams)} teams")
            total += n
        except Exception as e:  # noqa: BLE001 -- never block the emails
            print(f"  ! [{cfg.league_name}] league-wide log failed: {e.__class__.__name__}: {e}")
    return total


# --------------------------------------------------------------------------- #
# actuals (after the week)
# --------------------------------------------------------------------------- #
def store_actuals(conn, season, week, league_id, team_id, players, fetched_at=None) -> int:
    fetched = (fetched_at or datetime.now()).isoformat()
    n = 0
    for bp in players:
        on_bye = bool(getattr(bp, "on_bye_week", False))
        complete = on_bye or float(getattr(bp, "game_played", 100) or 0) >= 100
        conn.execute(
            "INSERT OR REPLACE INTO lw_actuals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                season, week, str(league_id), tracking._norm_key(bp.name), int(team_id),
                bp.name, getattr(bp, "position", "") or "",
                float(getattr(bp, "points", 0) or 0),
                1 if on_bye else 0, 1 if complete else 0, fetched,
            ),
        )
        n += 1
    return n


def weeks_missing_actuals(conn, season: int, before_week: int) -> list[int]:
    rows = conn.execute(
        """
        SELECT DISTINCT p.week FROM lw_predictions p
        WHERE p.season=? AND p.week<? AND NOT EXISTS (
            SELECT 1 FROM lw_actuals a
            WHERE a.season=p.season AND a.week=p.week AND a.league_id=p.league_id)
        ORDER BY p.week
        """,
        (season, before_week),
    ).fetchall()
    return [r[0] for r in rows]


def fetch_actuals(settings, conn, season: int, week: int) -> int:
    from .espn import connect as espn_connect

    total = 0
    for cfg in settings.leagues:
        try:
            league = espn_connect(cfg, season, settings.espn_s2, settings.swid)
            n = 0
            for box in league.box_scores(week=week):
                for side in ("home", "away"):
                    team = getattr(box, f"{side}_team", None)
                    tid = getattr(team, "team_id", None)
                    if tid is None:
                        continue
                    n += store_actuals(conn, season, week, cfg.league_id, tid,
                                       getattr(box, f"{side}_lineup", None) or [])
            conn.commit()
            print(f"  [{cfg.league_name}] week {week}: league-wide actuals for {n} players")
            total += n
        except Exception as e:  # noqa: BLE001
            print(f"  ! [{cfg.league_name}] week {week}: league-wide actuals failed: {e}")
    return total


def fetch_missing(settings, conn, season: int, current_week: int) -> None:
    for w in weeks_missing_actuals(conn, season, current_week):
        fetch_actuals(settings, conn, season, w)


# --------------------------------------------------------------------------- #
# grading
# --------------------------------------------------------------------------- #
REAL_POSITIONS = {"QB", "RB", "WR", "TE", "K", "D/ST"}


def decision_rows(conn, season: int, week: int) -> list:
    """Latest pre-kickoff snapshot per (league, player), preferring in-window rows."""
    best: dict = {}
    for r in conn.execute(
        "SELECT * FROM lw_predictions WHERE season=? AND week=? ORDER BY sent_at, window",
        (season, week),
    ):
        key = (r["league_id"], r["name_key"])
        cur = best.get(key)
        if cur is None or (r["in_window"], r["sent_at"] or "") >= (cur["in_window"], cur["sent_at"] or ""):
            best[key] = r
    return list(best.values())


def graded_rows(conn, season: int, week: int) -> list[dict]:
    """Rows shaped like evaluate.graded_rows so its metric functions can be reused."""
    actuals = {
        (r["league_id"], r["name_key"]): r
        for r in conn.execute("SELECT * FROM lw_actuals WHERE season=? AND week=?", (season, week))
    }
    out = []
    for d in decision_rows(conn, season, week):
        a = actuals.get((d["league_id"], d["name_key"]))
        if a is None:
            continue
        pos = d["position"] if d["position"] in REAL_POSITIONS else (a["position"] or d["position"])
        ours, espn = float(d["our_points"] or 0), float(d["espn_points"] or 0)
        out.append({
            "season": season, "week": week, "league_id": d["league_id"],
            "league": d["league_id"], "name": d["name"], "name_key": d["name_key"],
            "position": pos, "ours": ours, "espn": espn,
            "actual": float(a["actual_points"] or 0),
            "on_bye": bool(d["on_bye"]) or bool(a["on_bye"]),
            "complete": bool(a["game_complete"]),
            "provenance": "live", "delta": round(ours - espn, 2),
        })
    return out


def logged_weeks(conn, season: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT week FROM lw_predictions WHERE season=? ORDER BY week", (season,))]


def season_rows(conn, season: int, through_week: int) -> list[dict]:
    rows: list[dict] = []
    for w in logged_weeks(conn, season):
        if w <= through_week:
            rows += graded_rows(conn, season, w)
    return rows
