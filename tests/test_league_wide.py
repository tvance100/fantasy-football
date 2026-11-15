"""League-wide logging and grading."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from ffball import evaluate, league_wide
from ffball.projections import PlayerProjection
from ffball.slate import ET, Game, SendWindow

NOW = datetime(2025, 11, 9, 10, 0, tzinfo=ET)
SUN_1PM = datetime(2025, 11, 9, 13, 0, tzinfo=ET)
THU = datetime(2025, 11, 6, 20, 15, tzinfo=ET)
SUN = SendWindow("sunday", "Sunday games", datetime(2025, 11, 9, 10, 0, tzinfo=ET),
                 [Game("SF", "LAR", SUN_1PM, "Sunday", "REG")])


def _p(name, pos, team, ours, espn, kickoff):
    return PlayerProjection(name=name, position=pos, pro_team=team, opponent="X",
                            current_slot="BE", eligible_slots=[pos], on_bye=False,
                            espn_points=espn, our_points=ours, delta_vs_espn=ours - espn,
                            kickoff=kickoff, player_id=hash(name) % 10000)


def _box(name, pos, pts):
    return SimpleNamespace(name=name, position=pos, points=pts, playerId=1,
                           on_bye_week=False, game_played=100)


def test_log_skips_players_already_kicked_off_and_grades():
    conn = league_wide.connect(":memory:")
    projs = [
        _p("Sun RB", "RB", "SF", 12.0, 10.0, SUN_1PM),
        _p("Sun WR", "WR", "LAR", 8.0, 9.0, SUN_1PM),
        _p("Thu QB", "QB", "KC", 20.0, 18.0, THU),       # already played: must be skipped
    ]
    n = league_wide.log_team(conn, season=2025, week=10, window=SUN, league_id="L1",
                             team_id=3, projections=projs, now=NOW)
    assert n == 2
    names = {r["name"] for r in conn.execute("SELECT name FROM lw_predictions")}
    assert names == {"Sun RB", "Sun WR"}
    assert all(r["in_window"] == 1 for r in conn.execute("SELECT in_window FROM lw_predictions"))

    league_wide.store_actuals(conn, 2025, 10, "L1", 3,
                              [_box("Sun RB", "RB", 15.0), _box("Sun WR", "WR", 2.0)])
    rows = league_wide.graded_rows(conn, 2025, 10)
    by = {r["name"]: r for r in rows}
    assert by["Sun RB"]["ours"] == 12.0 and by["Sun RB"]["actual"] == 15.0
    assert by["Sun WR"]["delta"] == -1.0
    assert league_wide.weeks_missing_actuals(conn, 2025, 11) == []


def test_latest_in_window_snapshot_is_decision_of_record():
    conn = league_wide.connect(":memory:")
    p_early = [_p("Mon TE", "TE", "DAL", 6.0, 7.0, NOW + timedelta(days=1, hours=10))]
    p_late = [_p("Mon TE", "TE", "DAL", 9.0, 7.5, NOW + timedelta(days=1, hours=10))]
    mon = SendWindow("monday", "Monday games", NOW + timedelta(days=1, hours=7),
                     [Game("DAL", "NYG", NOW + timedelta(days=1, hours=10), "Monday", "REG")])
    league_wide.log_team(conn, season=2025, week=10, window=SUN, league_id="L1", team_id=1,
                         projections=p_early, now=NOW)
    league_wide.log_team(conn, season=2025, week=10, window=mon, league_id="L1", team_id=1,
                         projections=p_late, now=NOW + timedelta(days=1))
    d = league_wide.decision_rows(conn, 2025, 10)
    assert len(d) == 1 and d[0]["window"] == "monday" and d[0]["our_points"] == 9.0


def test_scorecard_section_renders():
    conn = league_wide.connect(":memory:")
    projs = [_p(f"Player {c}", "WR", "SF", 10 + i, 10.0, SUN_1PM) for i, c in enumerate("ABCDEF")]
    league_wide.log_team(conn, season=2025, week=10, window=SUN, league_id="L1", team_id=1,
                         projections=projs, now=NOW)
    league_wide.store_actuals(conn, 2025, 10, "L1", 1, [_box(f"Player {c}", "WR", 9 + 2 * i) for i, c in enumerate("ABCDEF")])
    text = "\n".join(evaluate.league_wide_section(league_wide.season_rows(conn, 2025, 10), 10))
    assert "LEAGUE-WIDE CHECK" in text and "WR" in text and "6 graded" in text
