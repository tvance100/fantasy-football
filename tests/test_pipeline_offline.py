"""End-to-end pipeline test with a synthetic roster (no ESPN credentials)."""

from ffball.config import LeagueConfig
from ffball.espn import LeagueData, RosteredPlayer
from ffball.projections import project_roster
from ffball.report import build_email_html, render_league_section
from ffball.shared_data import SharedData
from ffball.startsit import optimize

PPR_FORMAT = [
    {"id": 3, "abbr": "PY", "points": 0.04},
    {"id": 4, "abbr": "PTD", "points": 4},
    {"id": 20, "abbr": "INT", "points": -2},
    {"id": 24, "abbr": "RY", "points": 0.1},
    {"id": 25, "abbr": "RTD", "points": 6},
    {"id": 42, "abbr": "REY", "points": 0.1},
    {"id": 43, "abbr": "RETD", "points": 6},
    {"id": 53, "abbr": "REC", "points": 1},
    {"id": 72, "abbr": "FUML", "points": -2},
]


def _p(name, pos, team, opp, slot, proj, breakdown, inj="ACTIVE", bye=False):
    elig = {
        "QB": ["QB", "OP"],
        "RB": ["RB", "RB/WR", "RB/WR/TE", "OP"],
        "WR": ["WR", "RB/WR", "RB/WR/TE", "OP"],
        "TE": ["TE", "RB/WR/TE", "OP"],
        "D/ST": ["D/ST"],
        "K": ["K"],
    }[pos]
    return RosteredPlayer(
        name=name, player_id=0, position=pos, pro_team=team, eligible_slots=elig,
        current_slot=slot, injury_status=inj, pro_opponent=opp, on_bye=bye,
        espn_proj_points=proj, proj_breakdown=breakdown,
    )


def _make_league():
    cfg = LeagueConfig(league_id="1", team_id=1, league_name="Test League")
    cfg.scoring_format = PPR_FORMAT
    cfg.scoring_type = "ppr"
    cfg.roster_slots = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1}
    roster = [
        _p("Star QB", "QB", "KC", "DEN", "QB", 22, {"passingYards": 290, "passingTouchdowns": 2, "passingInterceptions": 0.6, "rushingYards": 18}),
        _p("RB One", "RB", "SF", "SEA", "RB", 18, {"rushingYards": 88, "rushingTouchdowns": 0.7, "receivingReceptions": 3, "receivingYards": 22}),
        _p("RB Two", "RB", "GB", "CHI", "RB", 12, {"rushingYards": 61, "rushingTouchdowns": 0.4, "receivingReceptions": 2, "receivingYards": 14}),
        _p("RB Three", "RB", "BUF", "MIA", "BE", 14, {"rushingYards": 70, "rushingTouchdowns": 0.5, "receivingReceptions": 3, "receivingYards": 20}),
        _p("WR One", "WR", "MIN", "DET", "WR", 16, {"receivingReceptions": 7, "receivingYards": 92, "receivingTouchdowns": 0.6}),
        _p("WR Two", "WR", "PHI", "DAL", "WR", 13, {"receivingReceptions": 5, "receivingYards": 68, "receivingTouchdowns": 0.4}),
        _p("WR Three", "WR", "MIA", "BUF", "BE", 11, {"receivingReceptions": 5, "receivingYards": 60, "receivingTouchdowns": 0.3}),
        _p("TE One", "TE", "KC", "DEN", "TE", 10, {"receivingReceptions": 5, "receivingYards": 55, "receivingTouchdowns": 0.4}),
        _p("Flex WR", "WR", "LAR", "ARI", "FLEX", 12, {"receivingReceptions": 6, "receivingYards": 70, "receivingTouchdowns": 0.3}),
        _p("Def", "D/ST", "BAL", "CIN", "D/ST", 8, {}),
        _p("Kicker", "K", "DAL", "PHI", "K", 8, {}),
        _p("Hurt RB", "RB", "TEN", "HOU", "BE", 9, {"rushingYards": 45}, inj="QUESTIONABLE"),
    ]
    return LeagueData(config=cfg, week=10, roster=roster, starting_slot_counts=cfg.roster_slots)


def test_full_offline_pipeline():
    sd = SharedData(season=2025, stats_season=2024, week=10)  # empty shared data
    ld = _make_league()
    projs = project_roster(ld, sd)
    assert all(p.our_points >= 0 for p in projs)

    lineup = optimize(projs, ld.starting_slot_counts, ld.week)
    starters = [p.name for _, p in lineup.starters]
    assert len(lineup.starters) == 9
    assert "Star QB" in starters
    # RB Three (14) should be started over RB Two (12)
    assert "RB Three" in starters
    assert lineup.projected_total > 0

    section = render_league_section(ld, lineup, projs)
    doc = build_email_html([section], sd)
    assert "Test League" in doc
    assert "Recommended lineup" in doc
    assert "<table" in doc


def test_windowed_report_scopes_and_locks():
    from datetime import datetime

    from ffball.slate import ET, Game, SendWindow

    sd = SharedData(season=2025, stats_season=2024, week=10)
    sd.games = [
        Game("KC", "DEN", datetime(2025, 11, 6, 20, 15, tzinfo=ET), "Thursday", "REG"),
        Game("SF", "SEA", datetime(2025, 11, 9, 13, 0, tzinfo=ET), "Sunday", "REG"),
    ]
    ld = _make_league()
    projs = project_roster(ld, sd)
    star_qb = next(p for p in projs if p.name == "Star QB")  # KC -> Thursday
    assert star_qb.kickoff is not None

    lineup = optimize(projs, ld.starting_slot_counts, ld.week)
    window = SendWindow("pre_weekend", "Thursday games",
                        datetime(2025, 11, 6, 17, 0, tzinfo=ET),
                        [sd.games[0]])
    section = render_league_section(ld, lineup, projs, window)
    assert "Thursday games" in section
    assert "locks" in section.lower() or "LOCKED" in section
    doc = build_email_html([section], sd, window)
    assert "Send by" in doc


def test_dst_uses_espn_projection_not_zero():
    # ESPN's D/ST projected_breakdown uses defensive stat keys our scoring
    # engine doesn't score -- we must fall back to ESPN's number, not 0.
    sd = SharedData(season=2025, stats_season=2024, week=10)
    cfg = LeagueConfig(league_id="1", team_id=1, league_name="L")
    cfg.scoring_format = PPR_FORMAT
    dst = RosteredPlayer(
        name="Ravens D/ST", player_id=0, position="D/ST", pro_team="BAL",
        eligible_slots=["D/ST"], current_slot="D/ST", injury_status="ACTIVE",
        pro_opponent="None", on_bye=False, espn_proj_points=9.0,
        proj_breakdown={"defensiveInterceptions": 1.0, "defensiveSacks": 2.5},
    )
    ld = LeagueData(config=cfg, week=10, roster=[dst], starting_slot_counts={"D/ST": 1})
    proj = project_roster(ld, sd)[0]
    assert proj.our_points == 9.0
    assert proj.opponent == "—"  # "None" string is cleaned to em dash


def test_opponent_resolved_from_schedule_not_stale_espn_field():
    from datetime import datetime

    from ffball.slate import ET, Game

    sd = SharedData(season=2025, stats_season=2024, week=10)
    sd.matchups = {"KC": {"opp": "BUF", "home": True}, "BUF": {"opp": "KC", "home": False}}
    sd.games = [Game("KC", "BUF", datetime(2025, 11, 9, 20, 20, tzinfo=ET), "Sunday", "REG")]
    ld = _make_league()
    ld.roster[0].pro_team = "KC"
    ld.roster[0].pro_opponent = "None"  # stale/missing on ESPN's side
    proj = next(p for p in project_roster(ld, sd) if p.pro_team == "KC")
    assert proj.opponent == "BUF"


def test_bye_player_not_started():
    sd = SharedData(season=2025, stats_season=2024, week=10)
    ld = _make_league()
    ld.roster[0].on_bye = True  # Star QB on bye
    ld.roster.append(_p("Backup QB", "QB", "NYJ", "NE", "BE", 15,
                        {"passingYards": 230, "passingTouchdowns": 1.4, "passingInterceptions": 0.8}))
    projs = project_roster(ld, sd)
    lineup = optimize(projs, ld.starting_slot_counts, ld.week)
    qb_starter = next(p for s, p in lineup.starters if s == "QB")
    assert qb_starter.name == "Backup QB"


def test_v2_never_projects_above_espn_but_keeps_downgrades():
    # A soft matchup and a hot streak would push us above ESPN; v2 caps at ESPN.
    sd = SharedData(season=2025, stats_season=2024, week=10)
    sd.matchups = {"MIN": {"opp": "DET", "home": True}, "TEN": {"opp": "HOU", "home": True}}
    sd.dvp = {("WR", "DET"): {"mult": 1.25, "rank": 1, "of": 32, "ppg_allowed": 30.0}}
    sd.form = {"wr one": {"games": 4, "delta_pct": 0.4, "recent_ppg": 20, "season_ppg": 14}}
    ld = _make_league()
    projs = {p.name: p for p in project_roster(ld, sd)}
    wr = projs["WR One"]
    assert wr.factors["dvp"] == 1.25 and wr.factors["form"] > 1
    assert wr.our_points == wr.espn_points  # boost removed
    hurt = projs["Hurt RB"]
    assert hurt.our_points < hurt.espn_points  # questionable downgrade kept
    assert all(p.our_points <= p.espn_points + 1e-9 for p in projs.values())
