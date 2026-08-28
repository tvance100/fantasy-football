"""Scoring engine: applies arbitrary live ESPN scoring formats correctly."""

from ffball.scoring import infer_scoring_type, score_stat_line

# Minimal realistic ESPN scoring_format fragments (id = statId, points = value).
PPR = [
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
HALF = [dict(x, points=0.5) if x["id"] == 53 else x for x in PPR]
STD = [dict(x, points=0) if x["id"] == 53 else x for x in PPR]


def test_ppr_wr_line():
    line = {"receivingReceptions": 8, "receivingYards": 110, "receivingTouchdowns": 1}
    total, items, warns = score_stat_line(line, PPR)
    # 8 rec + 11.0 yds + 6 td = 25.0
    assert total == 25.0
    assert not warns


def test_scoring_type_changes_total():
    line = {"receivingReceptions": 8, "receivingYards": 110, "receivingTouchdowns": 1}
    ppr = score_stat_line(line, PPR)[0]
    half = score_stat_line(line, HALF)[0]
    std = score_stat_line(line, STD)[0]
    assert ppr == 25.0
    assert half == 21.0
    assert std == 17.0


def test_qb_line_with_interception_penalty():
    line = {"passingYards": 300, "passingTouchdowns": 2, "passingInterceptions": 1,
            "rushingYards": 20}
    total = score_stat_line(line, PPR)[0]
    # 12 + 8 - 2 + 2 = 20.0
    assert total == 20.0


def test_per_yardage_bucket():
    fmt = [{"id": 6, "abbr": "PY10", "points": 1}]  # 1 pt / 10 passing yards
    total = score_stat_line({"passingYards": 345}, fmt)[0]
    assert total == 34.0


def test_milestone_bonus_is_soft():
    fmt = [{"id": 56, "abbr": "REY100", "points": 3}]  # 100-yard game bonus
    below = score_stat_line({"receivingYards": 40}, fmt)[0]
    near = score_stat_line({"receivingYards": 100}, fmt)[0]
    above = score_stat_line({"receivingYards": 160}, fmt)[0]
    assert below == 0.0
    assert 1.0 < near < 2.0          # ~half of 3
    assert above == 3.0


def test_infer_scoring_type():
    assert infer_scoring_type(PPR) == "ppr"
    assert infer_scoring_type(HALF) == "half_ppr"
    assert infer_scoring_type(STD) == "standard"


def test_unsupported_item_warns_not_crash():
    fmt = [{"id": 9999, "abbr": "???", "points": 5}]
    total, items, warns = score_stat_line({"passingYards": 100}, fmt)
    assert total == 0.0
    assert warns
