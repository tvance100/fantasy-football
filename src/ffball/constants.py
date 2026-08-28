"""Static reference maps.

The ESPN stat/position/pro-team maps are vendored from the `espn_api` package
(espn_api/football/constant.py) so the scoring engine can translate ESPN's
live `scoringItems` (keyed by numeric statId) onto projected stat lines
(keyed by stat name).  We deliberately do NOT hardcode any league's scoring
values here -- those are pulled live per league (see espn.py).
"""

from __future__ import annotations

# statId -> canonical stat-line key. Mirrors espn_api PLAYER_STATS_MAP but only
# the entries the projection/scoring path actually needs.
STAT_ID_TO_KEY: dict[int, str] = {
    # passing
    0: "passingAttempts",
    1: "passingCompletions",
    2: "passingIncompletions",
    3: "passingYards",
    4: "passingTouchdowns",
    19: "passing2PtConversions",
    20: "passingInterceptions",
    # rushing
    23: "rushingAttempts",
    24: "rushingYards",
    25: "rushingTouchdowns",
    26: "rushing2PtConversions",
    # receiving
    41: "receivingReceptions",
    42: "receivingYards",
    43: "receivingTouchdowns",
    44: "receiving2PtConversions",
    53: "receivingReceptions",
    58: "receivingTargets",
    # misc offense
    62: "twoPtConversions",
    63: "fumbleRecoveredForTD",
    64: "passingTimesSacked",
    68: "fumbles",
    72: "lostFumbles",
    73: "turnovers",
    # kicking (aggregate only -- bucket-by-distance handled separately)
    83: "madeFieldGoals",
    84: "attemptedFieldGoals",
    85: "missedFieldGoals",
    86: "madeExtraPoints",
    87: "attemptedExtraPoints",
    88: "missedExtraPoints",
    74: "madeFieldGoalsFrom50Plus",
    77: "madeFieldGoalsFrom40To49",
    80: "madeFieldGoalsFromUnder40",
    198: "madeFieldGoalsFrom50Plus",
    201: "madeFieldGoalsFrom60Plus",
    # per-yardage "every N" buckets (statId -> (source key, N))
}

# statId -> (stat-line key, yards_per_point) for "every N yards" scoring items.
PER_YARDAGE_BUCKETS: dict[int, tuple[str, int]] = {
    5: ("passingYards", 5),
    6: ("passingYards", 10),
    7: ("passingYards", 20),
    8: ("passingYards", 25),
    9: ("passingYards", 50),
    10: ("passingYards", 100),
    27: ("rushingYards", 5),
    28: ("rushingYards", 10),
    29: ("rushingYards", 20),
    30: ("rushingYards", 25),
    31: ("rushingYards", 50),
    32: ("rushingYards", 100),
    47: ("receivingYards", 5),
    48: ("receivingYards", 10),
    49: ("receivingYards", 20),
    50: ("receivingYards", 25),
    51: ("receivingYards", 50),
    52: ("receivingYards", 100),
}

# statId -> (stat-line key, threshold) for single-game milestone bonuses.
GAME_MILESTONE_BONUSES: dict[int, tuple[str, float]] = {
    17: ("passingYards", 300),
    18: ("passingYards", 400),
    37: ("rushingYards", 100),
    38: ("rushingYards", 200),
    56: ("receivingYards", 100),
    57: ("receivingYards", 200),
}

# lineupSlotId -> position label (from espn_api POSITION_MAP).
SLOT_ID_TO_POS: dict[int, str] = {
    0: "QB", 1: "TQB", 2: "RB", 3: "RB/WR", 4: "WR", 5: "WR/TE", 6: "TE",
    7: "OP", 8: "DT", 9: "DE", 10: "LB", 11: "DL", 12: "CB", 13: "S",
    14: "DB", 15: "DP", 16: "D/ST", 17: "K", 18: "P", 19: "HC", 20: "BE",
    21: "IR", 22: "", 23: "RB/WR/TE", 24: "ER", 25: "Rookie",
}

# Which eligible positions can fill each multi-position slot.
FLEX_ELIGIBILITY: dict[str, set[str]] = {
    "RB/WR": {"RB", "WR"},
    "WR/TE": {"WR", "TE"},
    "RB/WR/TE": {"RB", "WR", "TE"},
    "OP": {"QB", "RB", "WR", "TE"},          # superflex
    "DP": {"DT", "DE", "LB", "DL", "CB", "S", "DB"},
    "DL": {"DT", "DE", "DL"},
    "DB": {"CB", "S", "DB"},
}

# Slots that are not "starting" slots for lineup purposes.
NON_STARTING_SLOTS = {"BE", "IR", "", "Rookie", "ER"}

# NFL stadium coordinates + roof type, keyed by team abbreviation
# (matches nfl_data_py / espn_api PRO_TEAM_MAP abbreviations).
# roof: "dome" (always closed), "retractable" (treat as closed unless known open),
#       "outdoor".
STADIUMS: dict[str, dict] = {
    "ARI": {"lat": 33.5277, "lon": -112.2626, "roof": "retractable"},
    "ATL": {"lat": 33.7554, "lon": -84.4009, "roof": "retractable"},
    "BAL": {"lat": 39.2780, "lon": -76.6227, "roof": "outdoor"},
    "BUF": {"lat": 42.7738, "lon": -78.7870, "roof": "outdoor"},
    "CAR": {"lat": 35.2258, "lon": -80.8528, "roof": "outdoor"},
    "CHI": {"lat": 41.8623, "lon": -87.6167, "roof": "outdoor"},
    "CIN": {"lat": 39.0955, "lon": -84.5161, "roof": "outdoor"},
    "CLE": {"lat": 41.5061, "lon": -81.6995, "roof": "outdoor"},
    "DAL": {"lat": 32.7473, "lon": -97.0945, "roof": "retractable"},
    "DEN": {"lat": 39.7439, "lon": -105.0201, "roof": "outdoor"},
    "DET": {"lat": 42.3400, "lon": -83.0456, "roof": "dome"},
    "GB": {"lat": 44.5013, "lon": -88.0622, "roof": "outdoor"},
    "HOU": {"lat": 29.6847, "lon": -95.4107, "roof": "retractable"},
    "IND": {"lat": 39.7601, "lon": -86.1639, "roof": "retractable"},
    "JAX": {"lat": 30.3239, "lon": -81.6373, "roof": "outdoor"},
    "KC": {"lat": 39.0489, "lon": -94.4839, "roof": "outdoor"},
    "LV": {"lat": 36.0909, "lon": -115.1833, "roof": "dome"},
    "LAC": {"lat": 33.9535, "lon": -118.3392, "roof": "dome"},
    "LAR": {"lat": 33.9535, "lon": -118.3392, "roof": "dome"},
    "MIA": {"lat": 25.9580, "lon": -80.2389, "roof": "outdoor"},
    "MIN": {"lat": 44.9736, "lon": -93.2575, "roof": "dome"},
    "NE": {"lat": 42.0909, "lon": -71.2643, "roof": "outdoor"},
    "NO": {"lat": 29.9511, "lon": -90.0812, "roof": "dome"},
    "NYG": {"lat": 40.8135, "lon": -74.0745, "roof": "outdoor"},
    "NYJ": {"lat": 40.8135, "lon": -74.0745, "roof": "outdoor"},
    "PHI": {"lat": 39.9008, "lon": -75.1675, "roof": "outdoor"},
    "PIT": {"lat": 40.4468, "lon": -80.0158, "roof": "outdoor"},
    "SF": {"lat": 37.4030, "lon": -121.9697, "roof": "outdoor"},
    "SEA": {"lat": 47.5952, "lon": -122.3316, "roof": "outdoor"},
    "TB": {"lat": 27.9759, "lon": -82.5033, "roof": "outdoor"},
    "TEN": {"lat": 36.1665, "lon": -86.7713, "roof": "outdoor"},
    "WSH": {"lat": 38.9078, "lon": -76.8645, "roof": "outdoor"},
}
