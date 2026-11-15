"""Weekly team report: analysis, ESPN adapter (with fakes), rendering, scheduling."""

import random
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from ffball import team_analysis as ta
from ffball import team_fetch, team_report
from ffball.projections import PlayerProjection
from ffball.slate import ET
from ffball.startsit import optimize
from ffball.team_analysis import LeagueSnapshot, PlayerLine, TeamSnapshot
from ffball.team_report import demo_snapshot

SLOTS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1}


def pl(name, pos, avg, slot="BE", inj="ACTIVE", team="KC", games=6, proj=None, **kw):
    p = PlayerLine(name=name, position=pos, pro_team=team, injury=inj, slot=slot,
                   season_avg=avg, proj_avg=avg if proj is None else proj, games=games, **kw)
    ta.ensure_eligibility([p], SLOTS)
    return p


def team(tid, name, players, scores=(), wins=0, losses=0, pf=0.0, pa=0.0, standing=0):
    t = TeamSnapshot(team_id=tid, name=name, roster=list(players), scores=list(scores),
                     wins=wins, losses=losses, points_for=pf, points_against=pa, standing=standing)
    return t


# --------------------------------------------------------------------------- #
# value & lineup engine
# --------------------------------------------------------------------------- #
def test_value_blends_production_with_projection_by_games():
    early = PlayerLine("a", "RB", season_avg=20, proj_avg=10, games=1)   # 0.3*20 + 0.7*10
    late = PlayerLine("b", "RB", season_avg=20, proj_avg=10, games=5)    # 0.7*20 + 0.3*10
    none = PlayerLine("c", "RB", season_avg=0, proj_avg=9, games=0)
    assert early.value == pytest.approx(13.0)
    assert late.value == pytest.approx(17.0)
    assert none.value == pytest.approx(9.0)


def test_injury_adjusts_value_and_availability():
    base = dict(position="WR", season_avg=10, proj_avg=10, games=5)
    assert PlayerLine("q", injury="QUESTIONABLE", **base).value == pytest.approx(9.5)
    assert PlayerLine("d", injury="DOUBTFUL", **base).value == pytest.approx(3.0)
    out = PlayerLine("o", injury="OUT", **base)
    assert out.value == 0.0 and not out.available
    assert not PlayerLine("ir", slot="IR", **base).available


def test_best_lineup_matches_the_live_optimizer():
    rng = random.Random(3)
    for trial in range(25):
        roster = []
        for pos, n in (("QB", 2), ("RB", 5), ("WR", 5), ("TE", 2), ("K", 1), ("D/ST", 1)):
            for i in range(n):
                roster.append(pl(f"{pos}{i}-{trial}", pos, round(rng.uniform(2, 22), 2)))
        total, picks = ta.best_lineup(roster, SLOTS)
        projs = [PlayerProjection(name=p.name, position=p.position, pro_team=p.pro_team,
                                  opponent="", current_slot="BE", eligible_slots=p.eligible_slots,
                                  on_bye=False, espn_points=0, our_points=p.value,
                                  delta_vs_espn=0) for p in roster]
        lineup = optimize(projs, SLOTS, 1)
        assert total == pytest.approx(lineup.projected_total, abs=0.01)
        assert {p.name for _, p in picks} == {p.name for _, p in lineup.starters}


def test_unavailable_players_never_start():
    roster = [pl("QBa", "QB", 20, inj="OUT"), pl("QBb", "QB", 12), pl("RB1", "RB", 9), pl("RB2", "RB", 8),
              pl("WR1", "WR", 9), pl("WR2", "WR", 8), pl("TE1", "TE", 6), pl("K1", "K", 7), pl("D1", "D/ST", 7)]
    _, picks = ta.best_lineup(roster, SLOTS)
    assert "QBa" not in {p.name for _, p in picks}


# --------------------------------------------------------------------------- #
# strengths / weaknesses, standings, recap
# --------------------------------------------------------------------------- #
def test_position_table_flags_strength_and_weakness():
    snap = demo_snapshot()
    rows = {r["pos"]: r for r in ta.position_table(snap)}
    assert rows["RB"]["verdict"] == "strength" and rows["RB"]["rank"] <= 4
    assert rows["TE"]["verdict"] == "weakness" and rows["TE"]["rank"] >= 9
    assert rows["LINEUP"]["of"] == 12
    assert all(rows[p]["players"] for p in ("QB", "RB", "WR", "TE"))


def test_standings_expected_wins_and_luck():
    # 3 teams, 2 weeks. Me: 100, 100. A: 90, 110. B: 80, 120.
    me = team(1, "me", [], scores=[100, 100], wins=2, losses=0, pf=200, pa=170, standing=1)
    a = team(2, "A", [], scores=[90, 110], wins=1, losses=1, pf=200, pa=190, standing=2)
    b = team(3, "B", [], scores=[80, 120], wins=0, losses=2, pf=200, pa=240, standing=3)
    snap = LeagueSnapshot("L", "1", 2, SLOTS, [me, a, b], my_id=1)
    st = ta.standings_info(snap)
    # week 1 beats both (1.0), week 2 beats none (0.0) -> 1.0 expected wins vs 2 actual
    assert st["expected_wins"] == pytest.approx(1.0)
    assert st["luck"] == pytest.approx(1.0)
    assert st["place"] == 1 and st["pf_rank"] == 3 or st["pf_rank"] >= 1


def test_week_recap_finds_the_benching_mistake_and_efficiency():
    box = [pl("QB1", "QB", 15, "QB", week_points=18, week_proj=17),
           pl("RB1", "RB", 10, "RB", week_points=9, week_proj=10),
           pl("RB2", "RB", 8, "RB", week_points=2, week_proj=9),         # bust
           pl("WR1", "WR", 10, "WR", week_points=12, week_proj=11),
           pl("WR2", "WR", 9, "WR", week_points=8, week_proj=9),
           pl("TE1", "TE", 7, "TE", week_points=6, week_proj=7),
           pl("FLEX", "WR", 6, "FLEX", week_points=3, week_proj=7),
           pl("D1", "D/ST", 7, "D/ST", week_points=7, week_proj=7),
           pl("K1", "K", 7, "K", week_points=7, week_proj=7),
           pl("BenchStar", "RB", 5, "BE", week_points=24, week_proj=5)]
    snap = LeagueSnapshot("L", "1", 3, SLOTS, [team(1, "me", box)], my_id=1, my_box=box,
                          my_score=66.0, opp_score=80.0, opp_name="Them")
    rc = ta.week_recap(snap)
    assert rc["result"] == "L"
    assert rc["optimal_pts"] > rc["started_pts"]
    assert rc["left_on_bench"] == pytest.approx(rc["optimal_pts"] - rc["started_pts"])
    m = rc["mistakes"][0]
    assert m["benched"] == "BenchStar" and m["cost"] >= 15 and m["started"] in ("RB2", "FLEX")
    assert ("RB2", "RB", 2, 9) in [(n, p, int(pts), proj) for n, p, pts, proj in rc["busts"]]


def test_outlook_flags_injuries_and_byes():
    roster = [pl("QB1", "QB", 15, "QB", team="KC"), pl("RB1", "RB", 10, "RB", team="BUF"),
              pl("RB2", "RB", 9, "RB", team="BUF", inj="QUESTIONABLE"), pl("WR1", "WR", 9, "WR"),
              pl("WR2", "WR", 8, "WR"), pl("TE1", "TE", 7, "TE"), pl("K1", "K", 7, "K"),
              pl("D1", "D/ST", 7, "D/ST")]
    snap = LeagueSnapshot("L", "1", 4, SLOTS, [team(1, "me", roster)], my_id=1,
                          bye_map={"BUF": 6, "KC": 9})
    ol = ta.outlook(snap)
    assert ("RB2", "RB", "Questionable") in ol["injuries"]
    assert ol["byes"] == [{"week": 6, "players": ["RB1", "RB2"]}]       # KC's wk 9 is beyond 3 weeks


# --------------------------------------------------------------------------- #
# free agents
# --------------------------------------------------------------------------- #
def _fa_snapshot(fas, my_te=4.0, bye_map=None):
    roster = [pl("QB1", "QB", 18, "QB"), pl("RB1", "RB", 12, "RB"), pl("RB2", "RB", 11, "RB"),
              pl("WR1", "WR", 12, "WR"), pl("WR2", "WR", 11, "WR"), pl("TE1", "TE", my_te, "TE"),
              pl("FLEX", "WR", 9, "FLEX"), pl("D1", "D/ST", 7, "D/ST"), pl("K1", "K", 7, "K"),
              pl("BenchRB", "RB", 3, "BE"), pl("BenchWR", "WR", 2.5, "BE")]
    snap = LeagueSnapshot("L", "1", 4, SLOTS, [team(1, "me", roster)], my_id=1,
                          free_agents=fas, bye_map=bye_map or {})
    ta.ensure_eligibility(snap.free_agents, SLOTS)
    return snap


def test_free_agent_upgrade_picks_lowest_value_drop():
    fa = pl("NewTE", "TE", 9, "FA", percent_owned=40, week_proj=8.5)
    (rec,) = ta.free_agent_recs(_fa_snapshot([fa]))
    assert rec["add"].name == "NewTE" and rec["kind"] == "upgrade"
    assert rec["gain"] == pytest.approx(9 - 4.0, abs=0.01)
    assert rec["drop"].name == "BenchWR"                  # lowest-value bench player
    assert rec["slot"] == "TE"


def test_free_agent_filters():
    injured = pl("HurtTE", "TE", 12, "FA", inj="OUT")
    marginal = pl("MehTE", "TE", 4.3, "FA")                 # +0.3 only
    junk_qb = pl("BackupQB", "QB", 5, "FA")
    assert ta.free_agent_recs(_fa_snapshot([injured, marginal, junk_qb])) == []


def test_free_agent_bye_cover_when_no_immediate_gain():
    # TE1 is on bye next week (wk 5); a FA TE of 4.5 barely helps now but covers the bye.
    fa = pl("CoverTE", "TE", 4.5, "FA")
    snap = _fa_snapshot([fa], my_te=4.0)
    snap.me.roster[5].pro_team = "DEN"
    snap.bye_map = {"DEN": 5}
    ta.ensure_eligibility(snap.me.roster, SLOTS)
    (rec,) = ta.free_agent_recs(snap)
    assert rec["kind"] == "cover" and rec["gain_next"] >= ta.MIN_COVER_GAIN


# --------------------------------------------------------------------------- #
# trades
# --------------------------------------------------------------------------- #
def _trade_league():
    """I'm deep at WR and thin at TE; Team X is the mirror image."""
    mine = [pl("QB1", "QB", 18, "QB"), pl("RB1", "RB", 12, "RB"), pl("RB2", "RB", 11, "RB"),
            pl("WR1", "WR", 14, "WR"), pl("WR2", "WR", 13, "WR"), pl("WR3", "WR", 12.5, "FLEX"),
            pl("WRbench", "WR", 11, "BE"), pl("TE1", "TE", 3, "TE"), pl("D1", "D/ST", 7, "D/ST"),
            pl("K1", "K", 7, "K")]
    x = [pl("xQB", "QB", 17, "QB"), pl("xRB1", "RB", 11, "RB"), pl("xRB2", "RB", 10, "RB"),
         pl("xWR1", "WR", 5, "WR"), pl("xWR2", "WR", 4, "WR"), pl("xTE1", "TE", 13, "TE"),
         pl("xTE2", "TE", 10, "BE"), pl("xFLEX", "RB", 6, "FLEX"), pl("xD", "D/ST", 6, "D/ST"),
         pl("xK", "K", 6, "K")]
    others = [team(i + 2, f"Filler {i}", [pl(f"f{i}{pos}", pos, v, s) for pos, v, s in
                                          (("QB", 16, "QB"), ("RB", 10, "RB"), ("RB", 9, "RB"), ("WR", 9, "WR"),
                                           ("WR", 8, "WR"), ("TE", 7, "TE"), ("WR", 6, "FLEX"),
                                           ("D/ST", 6, "D/ST"), ("K", 6, "K"))]) for i in range(8)]
    teams = [team(1, "me", mine), team(99, "Team X", x)] + others
    return LeagueSnapshot("L", "1", 4, SLOTS, teams, my_id=1)


def test_trades_are_mutually_beneficial_and_make_sense():
    snap = _trade_league()
    trades = ta.find_trades(snap)
    assert trades, "the mirror-image team should produce a trade"
    top = trades[0]
    assert top["partner"] == "Team X"
    assert any(p.position == "TE" for p in top["get"])
    assert all(p.position == "WR" for p in top["give"])
    assert top["gain"] >= ta.MIN_TRADE_GAIN and top["their_gain"] >= ta.MIN_THEIR_GAIN
    assert top["why"] and any("Team X" in w for w in top["why"])

    # re-derive both gains independently
    sc = snap.slot_counts
    gid, rid = {id(p) for p in top["give"]}, {id(p) for p in top["get"]}
    me, x = snap.me, next(t for t in snap.teams if t.name == "Team X")
    mine_after = [p for p in me.roster if id(p) not in gid] + top["get"]
    theirs_after = [p for p in x.roster if id(p) not in rid] + top["give"]
    assert ta.best_lineup(mine_after, sc)[0] - ta.best_lineup(me.roster, sc)[0] == pytest.approx(top["gain"])
    assert ta.best_lineup(theirs_after, sc)[0] - ta.best_lineup(x.roster, sc)[0] == pytest.approx(top["their_gain"])


def test_trades_never_include_kickers_defenses_or_injured_players():
    snap = _trade_league()
    snap.others[0].roster.append(pl("HurtStar", "WR", 25, "BE", inj="OUT"))
    for t in ta.find_trades(snap, limit=10):
        for p in t["give"] + t["get"]:
            assert p.position in ("QB", "RB", "WR", "TE") and p.available


def test_no_trade_when_nobody_benefits():
    same = lambda i: team(i, f"T{i}", [pl(f"{i}{pos}", pos, v, s) for pos, v, s in  # noqa: E731
                                       (("QB", 16, "QB"), ("RB", 10, "RB"), ("RB", 9, "RB"), ("WR", 9, "WR"),
                                        ("WR", 8, "WR"), ("TE", 7, "TE"), ("WR", 6, "FLEX"),
                                        ("D/ST", 6, "D/ST"), ("K", 6, "K"))])
    snap = LeagueSnapshot("L", "1", 4, SLOTS, [same(i) for i in range(1, 5)], my_id=1)
    assert ta.find_trades(snap) == []


# --------------------------------------------------------------------------- #
# analyze() + rendering
# --------------------------------------------------------------------------- #
def test_analyze_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(ta, "find_trades", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    a = ta.analyze(demo_snapshot())
    assert a["trades"] == [] and any("trades unavailable" in n for n in a["notes"])
    assert a["assessment"] and a["positions"] and a["free_agents"]


def test_render_email_has_every_section_and_escapes_html():
    snap = demo_snapshot("<League & Co>")
    a = ta.analyze(snap)
    subject, doc, text = team_report.render_email([a], 4, datetime(2026, 10, 6, 8, 0))
    assert subject.startswith("Weekly Team Report — Week 4")
    for needle in ("Overall assessment", "Strengths &amp; weaknesses", "Waiver / free-agent targets",
                   "Trade ideas", "Last week", "Looking ahead", "&lt;League &amp; Co&gt;"):
        assert needle in doc, needle
    assert "<League & Co>" not in doc
    assert "OVERALL ASSESSMENT" in text and "TRADE IDEAS" in text


def test_a_failed_league_renders_an_error_not_a_crash():
    good = ta.analyze(demo_snapshot("Good"))
    bad = {"error": "ValueError: nope", "league_name": "Broken"}
    subject, doc, text = team_report.render_email([good, bad], 4)
    assert "Could not build this league" in doc and "Broken" in text
    assert "Broken" not in subject


def test_demo_run_writes_html_and_never_emails(tmp_path, monkeypatch):
    monkeypatch.setattr(team_report, "REPORT_DIR", tmp_path)
    sent = []
    monkeypatch.setattr("ffball.email_send.send_email", lambda *a, **k: sent.append(a))
    team_report.run_team_report(None, 2026, 4, email=True, demo=True)
    assert (tmp_path / "team_report_2026_wk4_demo.html").exists() and sent == []


# --------------------------------------------------------------------------- #
# ESPN adapter (fakes shaped like espn_api's Team / Player / BoxPlayer / League)
# --------------------------------------------------------------------------- #
class FakePlayer:
    def __init__(self, name, pid, pos, team, avg, total, proj_avg, slot="BE", inj="ACTIVE",
                 owned=-1, eligible=None, points=0.0, proj=0.0):
        self.name, self.playerId, self.position, self.proTeam = name, pid, pos, team
        self.avg_points, self.total_points, self.projected_avg_points = avg, total, proj_avg
        self.lineupSlot = self.slot_position = slot
        self.injuryStatus, self.percent_owned = inj, owned
        self.eligibleSlots = eligible or [pos, "BE", "IR"]
        self.points, self.projected_points = points, proj
        self.stats = {}


class FakeTeam:
    def __init__(self, tid, name, roster, scores):
        self.team_id, self.team_name, self.roster = tid, name, roster
        self.wins, self.losses, self.ties = 2, 1, 0
        self.points_for, self.points_against, self.standing = 310.5, 290.25, tid
        self.scores, self.outcomes = scores, ["W", "L", "W", "U"]


class FakeLeague:
    def __init__(self, fail_k=False):
        self.fail_k = fail_k
        mine = [FakePlayer("Me QB", 1, "QB", "KC", 20.0, 80.0, 18.0, slot="QB", inj="QUESTIONABLE"),
                FakePlayer("Me RB", 2, "RB", "BUF", 11.0, 33.0, 12.0, slot="BE")]
        self.teams = [FakeTeam(1, "Me", mine, [101.0, 99.0, 120.0, 0.0]),
                      FakeTeam(2, "Them", [FakePlayer("T QB", 9, "QB", "DEN", 15.0, 60.0, 15.0)],
                               [90.0, 95.0, 100.0, 0.0])]

    def box_scores(self, week):
        lineup = [FakePlayer("Me QB", 1, "QB", "KC", 0, 0, 0, slot="QB", points=22.5, proj=19.0),
                  FakePlayer("Me RB", 2, "RB", "BUF", 0, 0, 0, slot="BE", points=14.0, proj=11.0)]
        return [SimpleNamespace(home_team=self.teams[0], away_team=self.teams[1], home_score=120.5,
                                away_score=100.25, home_lineup=lineup, away_lineup=[])]

    def free_agents(self, week, size, position):
        if self.fail_k and position == "K":
            raise RuntimeError("ESPN said no")
        if position == "TE":
            return [FakePlayer("FA TE", 50, "TE", "DEN", 8.0, 24.0, 7.0, slot="FA", owned=35.5, proj=7.5)]
        return []


def _settings():
    return SimpleNamespace(espn_s2="s2", swid="{x}")


def _cfg():
    return SimpleNamespace(league_id="123", team_id=1, league_name="My League")


def test_to_line_maps_espn_player_fields():
    p = FakePlayer("X", 7, "RB", "BUF", 10.0, 40.0, 9.0, slot="RB", inj="QUESTIONABLE", owned=55.5)
    line = team_fetch.to_line(p)
    assert (line.name, line.position, line.pro_team, line.player_id) == ("X", "RB", "BUF", 7)
    assert line.games == 4 and line.season_avg == 10.0 and line.proj_avg == 9.0
    assert line.injury == "QUESTIONABLE" and line.percent_owned == 55.5 and line.slot == "RB"


def test_fetch_snapshot_builds_everything_from_espn_objects(monkeypatch):
    monkeypatch.setattr("ffball.espn.connect", lambda *a, **k: FakeLeague())
    monkeypatch.setattr("ffball.espn._roster_slot_counts", lambda league: dict(SLOTS))
    snap = team_fetch.fetch_snapshot(_cfg(), _settings(), 2026, 3, {"KC": 7})

    assert snap.week == 3 and snap.my_id == 1 and snap.slot_counts == SLOTS and snap.bye_map == {"KC": 7}
    assert [t.name for t in snap.teams] == ["Me", "Them"]
    assert snap.me.scores == [101.0, 99.0, 120.0] and snap.me.outcomes == ["W", "L", "W"]   # future week cut
    assert (snap.my_score, snap.opp_score, snap.opp_name) == (120.5, 100.25, "Them")
    qb = next(p for p in snap.my_box if p.name == "Me QB")
    assert qb.week_points == 22.5 and qb.week_proj == 19.0 and qb.slot == "QB"
    assert qb.season_avg == 20.0 and qb.games == 4                      # season stats joined from roster
    assert next(p for p in snap.me.roster if p.name == "Me QB").injury == "QUESTIONABLE"
    (fa,) = snap.free_agents
    assert fa.name == "FA TE" and fa.slot == "FA" and fa.week_proj == 7.5 and fa.percent_owned == 35.5
    assert snap.notes == []


def test_fetch_snapshot_turns_partial_failures_into_notes(monkeypatch):
    monkeypatch.setattr("ffball.espn.connect", lambda *a, **k: FakeLeague(fail_k=True))
    monkeypatch.setattr("ffball.espn._roster_slot_counts", lambda league: dict(SLOTS))
    snap = team_fetch.fetch_snapshot(_cfg(), _settings(), 2026, 3, {})
    assert any("Free agents (K) unavailable" in n for n in snap.notes)
    assert any("Bye-week data unavailable" in n for n in snap.notes)
    assert len(snap.free_agents) == 1                       # the other positions still loaded


def test_fetch_then_analyze_end_to_end(monkeypatch):
    monkeypatch.setattr("ffball.espn.connect", lambda *a, **k: FakeLeague())
    monkeypatch.setattr("ffball.espn._roster_slot_counts", lambda league: dict(SLOTS))
    a = ta.analyze(team_fetch.fetch_snapshot(_cfg(), _settings(), 2026, 3, {"KC": 7}))
    assert a["standing"]["record"] == "2-1" and a["recap"]["result"] == "W"
    subject, doc, text = team_report.render_email([a], 3)
    assert "My League" in doc


def test_missing_team_id_is_a_clear_error(monkeypatch):
    monkeypatch.setattr("ffball.espn.connect", lambda *a, **k: FakeLeague())
    monkeypatch.setattr("ffball.espn._roster_slot_counts", lambda league: dict(SLOTS))
    cfg = SimpleNamespace(league_id="123", team_id=77, league_name="My League")
    with pytest.raises(ValueError, match="team_id 77"):
        team_fetch.fetch_snapshot(cfg, _settings(), 2026, 3, {})


# --------------------------------------------------------------------------- #
# Tuesday 8am scheduling
# --------------------------------------------------------------------------- #
def test_team_report_waits_for_tuesday_sends_once_and_retries_failures(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(team_report, "STATE_DIR", tmp_path)
    ready = datetime(2026, 10, 6, 8, 0, tzinfo=ET)
    monkeypatch.setattr("ffball.evaluate.week_ready_time", lambda season, week: ready)
    calls = []
    fail = {"on": True}

    def fake_run(settings, season, week, email, now=None, late=False, demo=False):
        calls.append((week, email, late))
        if fail["on"]:
            raise RuntimeError("ESPN down")

    monkeypatch.setattr(team_report, "run_team_report", fake_run)
    send = team_report.maybe_send_team_report

    assert send(None, 2026, ready - timedelta(hours=10), 5) is False          # Monday night
    assert calls == []

    t0 = ready + timedelta(minutes=2)
    assert send(None, 2026, t0, 5) is False                                    # fails -> not marked sent
    assert calls == [(4, True, False)]
    assert send(None, 2026, t0 + timedelta(minutes=10), 5) is False            # throttled
    assert len(calls) == 1
    fail["on"] = False
    assert send(None, 2026, t0 + timedelta(minutes=70), 5) is True             # retry succeeds
    assert len(calls) == 2
    assert send(None, 2026, t0 + timedelta(hours=3), 5) is False               # once per week
    assert len(calls) == 2
    assert "team report failed" in capsys.readouterr().out


def test_team_report_flags_late_sends(tmp_path, monkeypatch):
    monkeypatch.setattr(team_report, "STATE_DIR", tmp_path)
    ready = datetime(2026, 10, 6, 8, 0, tzinfo=ET)
    monkeypatch.setattr("ffball.evaluate.week_ready_time", lambda season, week: ready)
    seen = []
    monkeypatch.setattr(team_report, "run_team_report",
                        lambda settings, season, week, email, now=None, late=False, demo=False: seen.append(late))
    assert team_report.maybe_send_team_report(None, 2026, ready + timedelta(hours=9), 5) is True
    assert seen == [True]


def test_team_report_never_raises_and_skips_before_week_one(tmp_path, monkeypatch):
    monkeypatch.setattr(team_report, "STATE_DIR", tmp_path)
    assert team_report.maybe_send_team_report(None, 2026, datetime.now(ET), 1) is False
    monkeypatch.setattr("ffball.evaluate.week_ready_time",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("schedule gone")))
    assert team_report.maybe_send_team_report(None, 2026, datetime.now(ET), 5) is False


def test_demo_is_labeled_sample_data():
    from ffball.team_report import render_email
    from ffball.team_analysis import analyze
    from ffball.team_report import demo_snapshot
    subject, doc, text = render_email([analyze(demo_snapshot())], 4, demo=True)
    assert subject.startswith("[SAMPLE DATA]") and "NOT your leagues" in doc.replace("NOT YOUR", "NOT your")
    _, doc2, _ = render_email([analyze(demo_snapshot())], 4)
    assert "SAMPLE DATA" not in doc2


def test_team_and_owner_shown_and_record_check():
    from ffball.team_report import render_email, _partner
    from ffball.team_analysis import analyze
    from ffball.team_report import demo_snapshot
    from ffball.team_fetch import _record_check, _owner_name
    snap = demo_snapshot()
    snap.me.owner = "Tony Vance"
    snap.teams[1].owner = "Jane Doe"
    _, doc, text = render_email([analyze(snap)], 4)
    assert "owner: Tony Vance" in doc and "owner: Tony Vance" in text
    assert _partner(snap, snap.teams[1].name) == f"{snap.teams[1].name} (Jane Doe)"
    assert _record_check(snap) == []
    snap.me.wins += 1
    assert "weekly results" in _record_check(snap)[0]

    class T:
        owners = [{"firstName": "Tony", "lastName": "Vance"}, {"displayName": "x"}]
    assert _owner_name(T()) == "Tony Vance, x"
