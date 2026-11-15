"""Prediction log, backfill-from-HTML, and evaluation (all offline)."""

from datetime import datetime
from types import SimpleNamespace

import pytest

from ffball import evaluate, tracking
from ffball.backfill import backfill, parse_report
from ffball.projections import project_roster
from ffball.report import build_email_html, render_league_section
from ffball.shared_data import SharedData
from ffball.slate import ET, Game, SendWindow
from ffball.startsit import optimize

from test_pipeline_offline import _make_league


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
THU = Game("KC", "DEN", datetime(2025, 11, 6, 20, 15, tzinfo=ET), "Thursday", "REG")
SUN_GAMES = [
    Game(h, a, datetime(2025, 11, 9, 13, 0, tzinfo=ET), "Sunday", "REG")
    for h, a in [("SF", "SEA"), ("GB", "CHI"), ("BUF", "MIA"), ("MIN", "DET"),
                 ("PHI", "DAL"), ("LAR", "ARI"), ("BAL", "CIN"), ("TEN", "HOU")]
]
PRE = SendWindow("pre_weekend", "Thursday games", datetime(2025, 11, 6, 17, 0, tzinfo=ET), [THU])
SUN = SendWindow("sunday", "Sunday games", datetime(2025, 11, 9, 10, 0, tzinfo=ET), SUN_GAMES)


def _sd():
    sd = SharedData(season=2025, stats_season=2024, week=10)
    sd.games = [THU] + SUN_GAMES
    return sd


def _logged_db(conn=None):
    """Log a Thursday email and a Sunday email for the synthetic league."""
    conn = conn or tracking.connect(":memory:")
    ld = _make_league()
    sd = _sd()
    projs = project_roster(ld, sd)
    lineup = optimize(projs, ld.starting_slot_counts, ld.week)
    t_pre = datetime(2025, 11, 6, 16, 55, tzinfo=ET)
    t_sun = datetime(2025, 11, 9, 9, 55, tzinfo=ET)
    tracking.log_lineup(conn, season=2025, week=10, window=PRE, cfg=ld.config,
                        projections=projs, lineup=lineup, sent_at=t_pre)
    tracking.log_lineup(conn, season=2025, week=10, window=SUN, cfg=ld.config,
                        projections=projs, lineup=lineup, sent_at=t_sun)
    return conn, ld, projs


def _box(name, pos, team, pts, slot):
    return SimpleNamespace(name=name, playerId=0, position=pos, proTeam=team,
                           points=pts, slot_position=slot, on_bye_week=False, game_played=100)


# deliberately different from the projections so the grading has something to say
ACTUAL = {
    "Star QB": 30.0, "RB One": 6.0, "RB Two": 4.0, "RB Three": 20.0, "WR One": 9.0,
    "WR Two": 18.0, "WR Three": 2.0, "TE One": 11.0, "Flex WR": 14.0, "Def": 12.0,
    "Kicker": 5.0, "Hurt RB": 0.0,
}
FINAL_SLOT = {  # what the manager actually started
    "Star QB": "QB", "RB One": "RB", "RB Two": "RB", "WR One": "WR", "WR Two": "WR",
    "TE One": "TE", "Flex WR": "FLEX", "Def": "D/ST", "Kicker": "K",
    "RB Three": "BE", "WR Three": "BE", "Hurt RB": "BE",
}
POS = {"Star QB": "QB", "RB One": "RB", "RB Two": "RB", "RB Three": "RB", "WR One": "WR",
       "WR Two": "WR", "WR Three": "WR", "TE One": "TE", "Flex WR": "WR", "Def": "D/ST",
       "Kicker": "K", "Hurt RB": "RB"}


def _store_actuals(conn, ld, projs):
    team = {p.name: p.pro_team for p in projs}
    boxes = [_box(n, POS[n], team[n], pts, FINAL_SLOT[n]) for n, pts in ACTUAL.items()]
    return evaluate.store_actuals(conn, 2025, 10, ld.config.league_id, boxes)


# --------------------------------------------------------------------------- #
# tracking
# --------------------------------------------------------------------------- #
def test_log_writes_lineup_and_every_player_per_window():
    conn, ld, projs = _logged_db()
    assert conn.execute("SELECT COUNT(*) FROM lineups").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 2 * len(projs)
    row = conn.execute("SELECT * FROM predictions WHERE window='sunday' AND name='RB One'").fetchone()
    assert row["espn_points"] == 18.0
    assert row["our_points"] > 0
    assert "form" in row["factors_json"] or "injury" in row["factors_json"]
    assert row["rec_slot"] in ("RB", "FLEX", "BE")
    assert row["provenance"] == "live"


def test_relogging_same_window_replaces_not_duplicates():
    conn, ld, projs = _logged_db()
    lineup = optimize(projs, ld.starting_slot_counts, ld.week)
    tracking.log_lineup(conn, season=2025, week=10, window=SUN, cfg=ld.config,
                        projections=projs, lineup=lineup,
                        sent_at=datetime(2025, 11, 9, 9, 59, tzinfo=ET))
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == 2 * len(projs)


def test_decision_row_is_the_email_the_player_locked_in():
    conn, _, _ = _logged_db()
    rows = {r["name"]: r for r in tracking.decision_rows(conn, 2025, 10)}
    assert len(rows) == 12
    assert rows["Star QB"]["window"] == "pre_weekend"      # KC plays Thursday
    assert rows["TE One"]["window"] == "pre_weekend"
    assert rows["RB One"]["window"] == "sunday"            # SF plays Sunday
    assert rows["Star QB"]["in_window"] == 1


def test_logged_stat_lines_allow_rescoring_later():
    conn, _, _ = _logged_db()
    import json
    r = conn.execute("SELECT * FROM predictions WHERE name='Star QB' AND window='sunday'").fetchone()
    assert json.loads(r["stat_line_json"])["passingYards"] > 0
    assert json.loads(r["espn_stat_line_json"])["passingYards"] == 290


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
def test_graded_rows_and_lineup_comparison():
    conn, ld, projs = _logged_db()
    assert evaluate.weeks_missing_actuals(conn, 2025) == [10]
    assert _store_actuals(conn, ld, projs) == 12
    assert evaluate.weeks_missing_actuals(conn, 2025) == []

    rows = evaluate.graded_rows(conn, 2025, 10)
    assert len(rows) == 12
    qb = next(r for r in rows if r["name"] == "Star QB")
    assert qb["actual"] == 30.0 and qb["espn"] == 22.0 and qb["position"] == "QB"

    (lu,) = evaluate.lineup_comparison(conn, 2025, 10)
    # You started: QB30 + RB6 + RB4 + WR9 + WR18 + TE11 + FLEX14 + DST12 + K5
    assert lu["you"] == pytest.approx(109.0)
    # hindsight best can never lose to any realistic lineup
    assert lu["best"] >= lu["ours"] and lu["best"] >= lu["espn"] and lu["best"] >= lu["you"]
    # hindsight best from this roster: QB30 + RB20,6 + WR18,14 + TE11 + FLEX9 + D/ST12 + K5
    assert lu["best"] == pytest.approx(125.0)


def test_error_metrics_math():
    rows = [
        # name, pos, ours, espn, actual
        dict(position="RB", ours=10.0, espn=14.0, actual=11.0),   # ours closer (1 vs 3)
        dict(position="RB", ours=8.0, espn=6.0, actual=5.0),      # espn closer (3 vs 1)
        dict(position="RB", ours=12.0, espn=9.0, actual=12.0),    # ours closer (0 vs 3)
        dict(position="QB", ours=20.0, espn=20.0, actual=25.0),   # tie
    ]
    for i, r in enumerate(rows):
        r.update(league_id="L", week=1, name=f"p{i}", on_bye=False, complete=True,
                 delta=r["ours"] - r["espn"])
    m = {x["pos"]: x for x in evaluate.error_metrics(rows)}
    assert m["ALL"]["n"] == 4
    assert m["RB"]["mae_ours"] == pytest.approx((1 + 3 + 0) / 3)
    assert m["RB"]["mae_espn"] == pytest.approx((3 + 1 + 3) / 3)
    assert m["RB"]["ours_closer"] == 2 and m["RB"]["espn_closer"] == 1
    assert m["QB"]["decided"] == 0 and m["QB"]["closer_pct"] is None
    assert m["RB"]["bias_ours"] == pytest.approx(((10 - 11) + (8 - 5) + (12 - 12)) / 3)


def test_zero_zero_and_bye_rows_are_not_graded():
    rows = [
        dict(position="WR", ours=0.0, espn=0.0, actual=0.0, on_bye=False, complete=True),
        dict(position="WR", ours=5.0, espn=5.0, actual=3.0, on_bye=True, complete=True),
        dict(position="WR", ours=5.0, espn=5.0, actual=3.0, on_bye=False, complete=False),
        dict(position="WR", ours=5.0, espn=4.0, actual=3.0, on_bye=False, complete=True),
    ]
    assert len(evaluate.gradable(rows)) == 1


def test_ranking_counts_disagreements():
    base = dict(league_id="L", week=1, position="WR", on_bye=False, complete=True)
    rows = [
        # A vs B: we rank A over B, ESPN ranks B over A, A scored more -> we were right
        dict(base, name="A", ours=10, espn=8, actual=15),
        dict(base, name="B", ours=8, espn=10, actual=5),
        # C vs D: we rank C over D, ESPN agrees, D scored more -> both wrong
        dict(base, name="C", ours=9, espn=9, actual=2),
        dict(base, name="D", ours=7, espn=7, actual=20),
    ]
    rk = evaluate.ranking_metrics(rows)
    assert rk["disagree_ours_right"] >= 1
    assert rk["ours_n"] > 0 and rk["espn_n"] > 0
    assert 0 <= rk["ours_acc"] <= 1


def test_delta_buckets_assign_and_count():
    base = dict(league_id="L", week=1, position="RB", on_bye=False, complete=True)
    rows = [
        dict(base, name="a", ours=15, espn=10, actual=14, delta=5.0),
        dict(base, name="b", ours=10, espn=10.5, actual=9, delta=-0.5),
    ]
    b = {x["label"]: x for x in evaluate.delta_buckets(rows)}
    assert b["ours >3 above ESPN"]["n"] == 1 and b["ours >3 above ESPN"]["ours_closer"] == 1
    assert b["within 1 pt"]["n"] == 1


def test_wilson_interval_behaves():
    lo, hi = evaluate.wilson(5, 10)
    assert lo < 0.5 < hi
    lo, hi = evaluate.wilson(0, 0)
    assert (lo, hi) == (0.0, 1.0)
    lo, hi = evaluate.wilson(90, 100)
    assert 0.8 < lo < 0.9 < hi < 1.0


def test_eligibility_derivation():
    slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "SUPERFLEX": 1}
    assert set(evaluate.eligible_from_position("RB", slots)) == {"RB", "RB/WR/TE", "OP"}
    assert set(evaluate.eligible_from_position("QB", slots)) == {"QB", "OP"}
    assert evaluate.eligible_from_position("K", slots) == ["K"]


def test_run_evaluate_end_to_end_offline(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(evaluate, "REPORT_DIR", tmp_path)
    conn, ld, projs = _logged_db()
    _store_actuals(conn, ld, projs)
    text = evaluate.run_evaluate(None, conn, 2025, None, current_week=11, fetch=False, email=False)
    assert "START/SIT SCORECARD" in text and "Week 10" in text
    assert "PROJECTION ACCURACY" in text and "RANKING" in text and "BIGGEST MISSES" in text
    assert "CAUTION" in text                       # tiny sample must carry the warning
    assert (tmp_path / "scorecard_2025_wk10.txt").exists()


def test_run_evaluate_with_nothing_logged_is_graceful(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "REPORT_DIR", tmp_path)
    conn = tracking.connect(":memory:")
    msg = evaluate.run_evaluate(None, conn, 2025, None, current_week=5, fetch=False)
    assert "Nothing to grade" in msg


# --------------------------------------------------------------------------- #
# backfill from rendered HTML
# --------------------------------------------------------------------------- #
def _rendered_report(now, window):
    ld = _make_league()
    sd = _sd()
    projs = project_roster(ld, sd)
    lineup = optimize(projs, ld.starting_slot_counts, ld.week)
    section = render_league_section(ld, lineup, projs, window, now)
    return build_email_html([section], sd, window), ld, projs, lineup


def test_parse_report_round_trips_rendered_email():
    now = datetime(2025, 11, 9, 9, 55, tzinfo=ET)       # Sunday morning; KC/DEN game is over
    doc, ld, projs, lineup = _rendered_report(now, SUN)
    (sec,) = parse_report(doc)
    assert sec["league_name"] == "Test League" and sec["week"] == 10
    assert sec["slot_counts"] == ld.config.roster_slots
    assert sec["projected_total"] == pytest.approx(lineup.projected_total, abs=0.01)
    assert len(sec["players"]) == len(projs)

    by = {p["name"]: p for p in sec["players"]}
    assert by["Star QB"]["locked"] is True                # Thursday game already played
    assert by["RB One"]["locked"] is False and by["RB One"]["in_window"] is True
    assert by["Star QB"]["ours"] == pytest.approx(
        next(p.our_points for p in projs if p.name == "Star QB"), abs=0.05)
    assert by["RB One"]["espn"] == 18.0
    starters = {p.name for _, p in lineup.starters}
    assert {n for n, p in by.items() if p["rec_slot"] != "BE"} == starters


def test_backfill_imports_unlocked_rows_only_and_is_idempotent(tmp_path):
    now = datetime(2025, 11, 9, 9, 55, tzinfo=ET)
    doc, ld, projs, lineup = _rendered_report(now, SUN)
    (tmp_path / "startsit_wk10_sunday_20251109.html").write_text(doc)
    (tmp_path / "demo.html").write_text("<html></html>")        # ignored: not a window report
    conn = tracking.connect(":memory:")

    s = backfill(conn, tmp_path, 2025, [ld.config])
    assert s["files"] == 1 and s["unknown_league"] == []
    assert s["skipped_locked"] == 2                         # Star QB + TE One (KC) are locked
    assert s["rows"] == len(projs) - 2
    r = conn.execute("SELECT * FROM predictions WHERE name='RB One'").fetchone()
    assert r["provenance"] == "backfill" and r["in_window"] == 1
    assert conn.execute("SELECT COUNT(*) FROM predictions WHERE name='Star QB'").fetchone()[0] == 0

    s2 = backfill(conn, tmp_path, 2025, [ld.config])
    assert s2["rows"] == 0 and s2["skipped_files"] == 1
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == len(projs) - 2


def test_backfill_never_overwrites_live_rows(tmp_path):
    now = datetime(2025, 11, 9, 9, 55, tzinfo=ET)
    doc, ld, projs, _ = _rendered_report(now, SUN)
    (tmp_path / "startsit_wk10_sunday_20251109.html").write_text(doc)
    conn, _, _ = _logged_db()
    before = conn.execute("SELECT COUNT(*) FROM predictions WHERE provenance='live'").fetchone()[0]
    s = backfill(conn, tmp_path, 2025, [ld.config], overwrite=True)
    assert s["rows"] == 0
    assert conn.execute("SELECT COUNT(*) FROM predictions WHERE provenance='live'").fetchone()[0] == before


def test_backfilled_flex_starter_gets_position_from_actuals_and_grades(tmp_path):
    # Thursday email (pre_weekend) AND Sunday email so every player has a decision row.
    ld_cfg = _make_league().config
    conn = tracking.connect(":memory:")
    docs = {
        "startsit_wk10_pre_weekend_20251106.html":
            _rendered_report(datetime(2025, 11, 6, 16, 55, tzinfo=ET), PRE)[0],
        "startsit_wk10_sunday_20251109.html":
            _rendered_report(datetime(2025, 11, 9, 9, 55, tzinfo=ET), SUN)[0],
    }
    for n, d in docs.items():
        (tmp_path / n).write_text(d)
    backfill(conn, tmp_path, 2025, [ld_cfg])

    ld, _, projs = _make_league(), None, project_roster(_make_league(), _sd())
    _store_actuals(conn, ld, projs)
    rows = evaluate.graded_rows(conn, 2025, 10)
    assert {r["name"] for r in rows} >= {"Star QB", "RB One", "Flex WR"}
    flex = next(r for r in rows if r["name"] == "Flex WR")
    assert flex["position"] == "WR"                       # filled from the box score
    (lu,) = evaluate.lineup_comparison(conn, 2025, 10)
    assert lu["provenance"] == "backfill" and lu["best"] >= lu["ours"]
    text = evaluate.build_report(2025, 10, rows, rows, [lu], [10])
    assert "reconstructed from old email HTML" in text


# --------------------------------------------------------------------------- #
# wiring: logging hook + scheduled auto-grade
# --------------------------------------------------------------------------- #
def test_main_logging_hook_writes_and_never_raises(tmp_path, monkeypatch, capsys):
    from ffball import main as ffmain

    monkeypatch.setattr(tracking, "DB_PATH", tmp_path / "t.sqlite")
    ld = _make_league()
    projs = project_roster(ld, _sd())
    lineup = optimize(projs, ld.starting_slot_counts, ld.week)
    now = datetime(2025, 11, 9, 9, 55, tzinfo=ET)

    ffmain._log_predictions(2025, 10, SUN, now, [(ld.config, ld, projs, lineup)])
    conn = tracking.connect(tmp_path / "t.sqlite")
    assert conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0] == len(projs)

    # a broken DB path must be swallowed -- logging may never block the email
    monkeypatch.setattr(tracking, "DB_PATH", tmp_path / "no_such_dir" / "x" / "t.sqlite")
    monkeypatch.setattr(tracking, "connect", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    ffmain._log_predictions(2025, 10, SUN, now, [(ld.config, ld, projs, lineup)])
    assert "prediction logging failed" in capsys.readouterr().out


def test_auto_evaluate_waits_for_tuesday_runs_once_and_throttles(tmp_path, monkeypatch):
    from datetime import timedelta

    db = tmp_path / "t.sqlite"
    monkeypatch.setattr(tracking, "DB_PATH", db)
    monkeypatch.setattr(evaluate, "STATE_DIR", tmp_path)
    ready = datetime(2025, 11, 11, 8, 0, tzinfo=ET)            # Tuesday 8am after week 10
    monkeypatch.setattr(evaluate, "week_ready_time", lambda season, week: ready)

    conn, ld, projs = _logged_db(tracking.connect(db))
    calls = []

    def fake_run(settings, conn_, season, week, current_week, fetch, email):
        calls.append((week, current_week, fetch, email))
        _store_actuals(conn_, ld, projs)                        # as if ESPN returned them
        return ""

    monkeypatch.setattr(evaluate, "run_evaluate", fake_run)

    # Monday night: week not finished -> nothing happens
    assert evaluate.maybe_auto_evaluate(None, 2025, ready - timedelta(hours=9), 10) is False
    # current week still 10 (week 10 not over) -> nothing happens even after the ready time
    assert evaluate.maybe_auto_evaluate(None, 2025, ready + timedelta(minutes=1), 10) is False
    assert calls == []

    # Tuesday 8:10, current week is now 11 -> grade week 10, fetch + email on
    assert evaluate.maybe_auto_evaluate(None, 2025, ready + timedelta(minutes=10), 11) is True
    assert calls == [(None, 11, True, True)]

    # already graded -> later ticks are free no-ops
    assert evaluate.maybe_auto_evaluate(None, 2025, ready + timedelta(hours=2), 11) is False
    assert len(calls) == 1


def test_auto_evaluate_throttles_failed_attempts(tmp_path, monkeypatch):
    from datetime import timedelta

    db = tmp_path / "t.sqlite"
    monkeypatch.setattr(tracking, "DB_PATH", db)
    monkeypatch.setattr(evaluate, "STATE_DIR", tmp_path)
    ready = datetime(2025, 11, 11, 8, 0, tzinfo=ET)
    monkeypatch.setattr(evaluate, "week_ready_time", lambda season, week: ready)
    _logged_db(tracking.connect(db))
    calls = []
    monkeypatch.setattr(evaluate, "run_evaluate",
                        lambda *a, **k: calls.append(1) or "")   # ESPN down: no actuals stored

    t0 = ready + timedelta(minutes=5)
    evaluate.maybe_auto_evaluate(None, 2025, t0, 11)
    evaluate.maybe_auto_evaluate(None, 2025, t0 + timedelta(minutes=10), 11)   # throttled
    evaluate.maybe_auto_evaluate(None, 2025, t0 + timedelta(minutes=20), 11)   # throttled
    assert len(calls) == 1
    evaluate.maybe_auto_evaluate(None, 2025, t0 + timedelta(minutes=70), 11)   # retry allowed
    assert len(calls) == 2


def test_auto_evaluate_swallows_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(tracking, "connect", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert evaluate.maybe_auto_evaluate(None, 2025, datetime.now(ET), 5) is False
    assert "auto-evaluate failed" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# ESPN-only vs tool lineup differences
# --------------------------------------------------------------------------- #
def _pp(name, pos, ours, espn):
    from ffball.projections import PlayerProjection
    return PlayerProjection(name=name, position=pos, pro_team="KC", opponent="DEN",
                            current_slot="BE", eligible_slots=[pos], on_bye=False,
                            espn_points=espn, our_points=ours, delta_vs_espn=ours - espn)


def _swap_db(week=3):
    conn = tracking.connect(":memory:")
    cfg = SimpleNamespace(league_id="9", league_name="Swap League", scoring_type="standard",
                          roster_slots={"QB": 1, "RB": 1})
    projs = [_pp("Qb", "QB", 20, 20),
             _pp("Rb A", "RB", 10, 8),      # we like A
             _pp("Rb B", "RB", 8, 10)]      # ESPN likes B
    lineup = optimize(projs, cfg.roster_slots, week)
    tracking.log_lineup(conn, season=2025, week=week, window=None, cfg=cfg, projections=projs,
                        lineup=lineup, sent_at=datetime(2025, 11, 9, 9, 0, tzinfo=ET))
    boxes = [_box("Qb", "QB", "KC", 20.0, "QB"), _box("Rb A", "RB", "KC", 3.0, "RB"),
             _box("Rb B", "RB", "KC", 12.0, "BE")]
    evaluate.store_actuals(conn, 2025, week, "9", boxes)
    return conn


def test_swaps_found_paired_and_scored():
    conn = _swap_db()
    (lu,) = evaluate.lineup_comparison(conn, 2025, 3)
    assert len(lu["swaps"]) == 1
    sw = lu["swaps"][0]
    assert (sw["tool"], sw["espn"]) == ("Rb A", "Rb B")
    assert (sw["tool_actual"], sw["espn_actual"]) == (3.0, 12.0)
    assert lu["ours"] - lu["espn"] == pytest.approx(-9.0)

    ss = evaluate.swap_summary([lu])
    assert ss["changed"] == 1 and ss["swaps"] == 1 and ss["won"] == 0 and ss["lost"] == 1
    assert ss["net"] == pytest.approx(-9.0)


def test_identical_lineups_report_no_swaps():
    conn = tracking.connect(":memory:")
    cfg = SimpleNamespace(league_id="9", league_name="Same League", scoring_type="standard",
                          roster_slots={"QB": 1, "RB": 1})
    projs = [_pp("Qb", "QB", 20, 20), _pp("Rb A", "RB", 10, 10), _pp("Rb B", "RB", 8, 8)]
    lineup = optimize(projs, cfg.roster_slots, 3)
    tracking.log_lineup(conn, season=2025, week=3, window=None, cfg=cfg, projections=projs,
                        lineup=lineup, sent_at=datetime(2025, 11, 9, 9, 0, tzinfo=ET))
    evaluate.store_actuals(conn, 2025, 3, "9", [
        _box("Qb", "QB", "KC", 20.0, "QB"), _box("Rb A", "RB", "KC", 5.0, "RB"),
        _box("Rb B", "RB", "KC", 9.0, "BE")])
    (lu,) = evaluate.lineup_comparison(conn, 2025, 3)
    assert lu["swaps"] == []
    assert evaluate.swap_summary([lu])["changed"] == 0
    text = evaluate.build_report(2025, 3, [], [], [lu], [3])
    assert "same lineup as ESPN-only" in text


def test_report_shows_swap_section(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluate, "REPORT_DIR", tmp_path)
    conn = _swap_db()
    text = evaluate.run_evaluate(None, conn, 2025, None, current_week=4, fetch=False)
    assert "WHERE THE TOOL DIFFERED FROM ESPN-ONLY" in text
    assert "started Rb A (3.0) over Rb B (12.0)" in text and "ESPN right" in text
    assert "Season to date: lineup differed in 1 of 1 league-weeks" in text
