"""Apply a league's live scoring rules to a projected stat line.

`scoring_format` is exactly what ESPN returns for the league today
(list of {"id": statId, "abbr": ..., "label": ..., "points": ...}).
Nothing about the point values is hardcoded -- change the league settings
on ESPN and the next run picks them up.
"""

from __future__ import annotations

import math

from .constants import (
    GAME_MILESTONE_BONUSES,
    PER_YARDAGE_BUCKETS,
    STAT_ID_TO_KEY,
)

# Stat-line keys we know how to source from a projection. Anything else in the
# scoring format is ignored (with a note) so we never silently mis-score.
KNOWN_KEYS = set(STAT_ID_TO_KEY.values()) | {
    "passingYards", "rushingYards", "receivingYards",
}

# Scoring items that are real but near-zero expected value for a weekly
# projection (return TDs, special-teams TDs, defensive/misc buckets). We
# knowingly skip these without a warning -- projecting them adds noise, not signal.
SILENTLY_SKIPPED = {
    62, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105,
    106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120,
    121, 122, 123, 124, 125, 126, 127, 187, 188, 189, 190, 191, 192, 193, 194,
    195, 196, 197, 204, 205, 206, 207, 208, 209, 210, 211, 212, 213,
}


def score_stat_line(
    stat_line: dict[str, float],
    scoring_format: list[dict],
) -> tuple[float, dict[str, float], list[str]]:
    """Return (total_points, per_item_points, warnings)."""
    per_item: dict[str, float] = {}
    warnings: list[str] = []
    sl = {k: float(v or 0) for k, v in stat_line.items()}

    for item in scoring_format:
        sid = int(item["id"])
        pts = float(item.get("points") or 0)
        if pts == 0:
            continue
        abbr = item.get("abbr", str(sid))

        if sid in PER_YARDAGE_BUCKETS:
            src, per = PER_YARDAGE_BUCKETS[sid]
            val = math.floor(sl.get(src, 0) / per) * pts
            if val:
                per_item[abbr] = per_item.get(abbr, 0) + val
            continue

        if sid in GAME_MILESTONE_BONUSES:
            src, threshold = GAME_MILESTONE_BONUSES[sid]
            # projection is an expectation; treat as a soft probability that the
            # threshold is cleared rather than a hard 0/1.
            val = _milestone_expectation(sl.get(src, 0), threshold) * pts
            if val:
                per_item[abbr] = per_item.get(abbr, 0) + val
            continue

        if sid in SILENTLY_SKIPPED:
            continue

        key = STAT_ID_TO_KEY.get(sid)
        if key is None:
            if pts:
                warnings.append(f"unsupported scoring item {sid} ({abbr}) ignored")
            continue
        val = sl.get(key, 0) * pts
        if val:
            per_item[abbr] = per_item.get(abbr, 0) + val

    total = round(sum(per_item.values()), 2)
    return total, {k: round(v, 2) for k, v in per_item.items()}, warnings


def _milestone_expectation(projected: float, threshold: float) -> float:
    """Rough probability the single-game total clears `threshold`.

    Linear ramp centred on the threshold with a +/-40% band. Good enough for
    ranking; avoids a hard cliff at the projection point estimate.
    """
    if projected <= 0:
        return 0.0
    band = threshold * 0.4
    lo, hi = threshold - band, threshold + band
    if projected <= lo:
        return 0.0
    if projected >= hi:
        return 1.0
    return (projected - lo) / (hi - lo)


def infer_scoring_type(scoring_format: list[dict]) -> str:
    """Classify PPR-ness from the reception scoring item."""
    rec_pts = 0.0
    for item in scoring_format:
        if int(item["id"]) in (41, 53):  # receivingReceptions
            rec_pts = float(item.get("points") or 0)
            break
    if rec_pts >= 0.75:
        return "ppr"
    if rec_pts >= 0.25:
        return "half_ppr"
    return "standard"
