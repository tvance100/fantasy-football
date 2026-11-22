"""Grade past recommendations against what actually happened.

    run.py --evaluate              fetch missing actuals for completed weeks, print scorecard
    run.py --evaluate --week 4     grade (and re-fetch) one week
    run.py --evaluate --email      also email the scorecard
    run.py --evaluate --no-fetch   re-render from stored data, no ESPN calls

Truth comes from ESPN box scores (points under each league's own scoring), so a
"good call" is judged by exactly the rules you play under.

What is measured
----------------
* Projection error   MAE / bias of our projection vs ESPN's, per position.
* Ranking quality    among same-position players in a league-week, how often the
                     higher-projected one outscored the lower one -- and, where we
                     disagreed with ESPN, who was right. Start/sit is about
                     ordering, not absolute accuracy.
* Delta calibration  when we differ from ESPN by a lot, who was closer?
* Lineup results     actual points of: the lineup we recommended, the lineup ESPN's
                     numbers alone would give, the lineup you actually started, and
                     the hindsight-best lineup from the same roster.

Fantasy scoring is very noisy (a player's weekly standard deviation is several
points), so every comparison reports its sample size and a confidence interval.
Nothing here changes the model; it only measures it.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from . import tracking
from .constants import FLEX_ELIGIBILITY
from .projections import PlayerProjection
from .shared_data import norm_name
from .startsit import optimize

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / ".state"
REPORT_DIR = ROOT / "reports"

REAL_POSITIONS = {"QB", "RB", "WR", "TE", "K", "D/ST"}
NOT_STARTING = {"BE", "IR", "", None, "FA"}
EPS = 0.05  # points; differences smaller than this count as ties


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion k/n."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def _mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else 0.0


def eligible_from_position(position: str, slot_counts: dict) -> list[str]:
    """Slot tokens a player of `position` can fill (for rows logged without them)."""
    elig = [position] if position else []
    for label in slot_counts:
        token = {"FLEX": "RB/WR/TE", "SUPERFLEX": "OP"}.get(label, label)
        allowed = FLEX_ELIGIBILITY.get(token)
        if allowed and position in allowed:
            elig.append(token)
    return list(dict.fromkeys(elig))


# --------------------------------------------------------------------------- #
# 1. actuals
# --------------------------------------------------------------------------- #
def store_actuals(conn, season: int, week: int, league_id: str, players, fetched_at=None) -> int:
    """Persist one league's final box-score lineup. `players` are espn_api BoxPlayers
    (duck-typed: name, playerId, position, proTeam, points, slot_position,
    on_bye_week, game_played)."""
    fetched = (fetched_at or datetime.now()).isoformat()
    n = 0
    for bp in players:
        on_bye = bool(getattr(bp, "on_bye_week", False))
        complete = on_bye or float(getattr(bp, "game_played", 100) or 0) >= 100
        conn.execute(
            "INSERT OR REPLACE INTO actuals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                season, week, str(league_id), norm_name(bp.name),
                int(getattr(bp, "playerId", 0) or 0), bp.name,
                getattr(bp, "position", "") or "", getattr(bp, "proTeam", "") or "",
                float(getattr(bp, "points", 0) or 0),
                getattr(bp, "slot_position", "") or "",
                1 if on_bye else 0, 1 if complete else 0, fetched,
            ),
        )
        n += 1
    conn.commit()
    return n


def _our_box_lineup(league, team_id: int, week: int):
    for box in league.box_scores(week=week):
        for side in ("home", "away"):
            team = getattr(box, f"{side}_team", None)
            if getattr(team, "team_id", None) == team_id:
                return getattr(box, f"{side}_lineup")
    raise ValueError(f"team_id {team_id} not found in week {week} box scores")


def fetch_actuals(settings, conn, season: int, week: int) -> dict[str, int]:
    """Pull final box scores for every configured league. Returns {league_name: rows}."""
    from .espn import connect

    out: dict[str, int] = {}
    for cfg in settings.leagues:
        try:
            league = connect(cfg, season, settings.espn_s2, settings.swid)
            players = _our_box_lineup(league, cfg.team_id, week)
            out[cfg.league_name] = store_actuals(conn, season, week, cfg.league_id, players)
            print(f"  [{cfg.league_name}] week {week}: stored {out[cfg.league_name]} players' actuals")
        except Exception as e:  # noqa: BLE001
            print(f"  ! [{cfg.league_name}] week {week}: could not fetch actuals: {e}")
            out[cfg.league_name] = 0
    return out


def weeks_missing_actuals(conn, season: int) -> list[int]:
    """Logged weeks where some league has predictions but no actuals yet."""
    rows = conn.execute(
        """
        SELECT DISTINCT p.week FROM predictions p
        WHERE p.season=? AND NOT EXISTS (
            SELECT 1 FROM actuals a
            WHERE a.season=p.season AND a.week=p.week AND a.league_id=p.league_id)
        ORDER BY p.week
        """,
        (season,),
    ).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# 2. join predictions with actuals
# --------------------------------------------------------------------------- #
def graded_rows(conn, season: int, week: int) -> list[dict]:
    """One dict per (league, player): the decision-of-record projection + the actual."""
    actuals = {
        (r["league_id"], r["name_key"]): r
        for r in conn.execute("SELECT * FROM actuals WHERE season=? AND week=?", (season, week))
    }
    names = {r["league_id"]: r["league_name"]
             for r in conn.execute("SELECT league_id, league_name FROM lineups "
                                   "WHERE season=? AND week=?", (season, week))}
    out: list[dict] = []
    for d in tracking.decision_rows(conn, season, week):
        a = actuals.get((d["league_id"], d["name_key"]))
        if a is None:
            continue
        pos = d["position"] if d["position"] in REAL_POSITIONS else (a["position"] or d["position"])
        out.append({
            "season": season, "week": week,
            "league_id": d["league_id"], "league": names.get(d["league_id"], d["league_id"]),
            "name": d["name"], "name_key": d["name_key"], "position": pos,
            "pro_team": d["pro_team"], "opponent": d["opponent"],
            "ours": float(d["our_points"] or 0), "espn": float(d["espn_points"] or 0),
            "actual": float(a["actual_points"] or 0),
            "on_bye": bool(d["on_bye"]) or bool(a["on_bye"]),
            "complete": bool(a["game_complete"]),
            "provenance": d["provenance"],
            "model": d["model_version"] or "v1-hand-tuned",
            "eligible_json": d["eligible_json"], "slot": d["rec_slot"],
        })
    for r in out:
        r["delta"] = round(r["ours"] - r["espn"], 2)
    return out


def gradable(rows: list[dict]) -> list[dict]:
    """Rows that say something about projection quality: played, finished, and
    projected to do something by at least one source."""
    return [r for r in rows
            if not r["on_bye"] and r["complete"] and not (r["ours"] == 0 and r["espn"] == 0)]


# --------------------------------------------------------------------------- #
# 3. metrics (pure functions over row dicts)
# --------------------------------------------------------------------------- #
def error_metrics(rows: list[dict]) -> list[dict]:
    rows = gradable(rows)
    groups: dict[str, list[dict]] = {"ALL": rows}
    for r in rows:
        groups.setdefault(r["position"], []).append(r)
    out = []
    order = ["ALL", "QB", "RB", "WR", "TE", "K", "D/ST"]
    for pos in sorted(groups, key=lambda p: order.index(p) if p in order else 99):
        g = groups[pos]
        if not g:
            continue
        ours_wins = espn_wins = 0
        for r in g:
            eo, ee = abs(r["ours"] - r["actual"]), abs(r["espn"] - r["actual"])
            if abs(eo - ee) <= EPS:
                continue
            if eo < ee:
                ours_wins += 1
            else:
                espn_wins += 1
        decided = ours_wins + espn_wins
        lo, hi = wilson(ours_wins, decided)
        out.append({
            "pos": pos, "n": len(g),
            "mae_ours": _mean(abs(r["ours"] - r["actual"]) for r in g),
            "mae_espn": _mean(abs(r["espn"] - r["actual"]) for r in g),
            "bias_ours": _mean(r["ours"] - r["actual"] for r in g),
            "bias_espn": _mean(r["espn"] - r["actual"] for r in g),
            "ours_closer": ours_wins, "espn_closer": espn_wins, "decided": decided,
            "closer_pct": (ours_wins / decided) if decided else None,
            "closer_ci": (lo, hi),
        })
    return out


def ranking_metrics(rows: list[dict]) -> dict:
    """Pairwise ordering accuracy within (league, week, position)."""
    rows = gradable(rows)
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["league_id"], r["week"], r["position"]), []).append(r)

    ours_ok = ours_n = espn_ok = espn_n = 0
    dis_ours = dis_espn = 0
    pairs = 0
    for g in groups.values():
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                a, b = g[i], g[j]
                act = a["actual"] - b["actual"]
                if abs(act) <= EPS:
                    continue
                pairs += 1
                o = a["ours"] - b["ours"]
                e = a["espn"] - b["espn"]
                o_dec, e_dec = abs(o) > EPS, abs(e) > EPS
                o_right = (o > 0) == (act > 0)
                e_right = (e > 0) == (act > 0)
                if o_dec:
                    ours_n += 1
                    ours_ok += o_right
                if e_dec:
                    espn_n += 1
                    espn_ok += e_right
                if o_dec and e_dec and (o > 0) != (e > 0):
                    dis_ours += o_right
                    dis_espn += e_right
    return {
        "pairs": pairs,
        "ours_acc": ours_ok / ours_n if ours_n else None, "ours_n": ours_n,
        "espn_acc": espn_ok / espn_n if espn_n else None, "espn_n": espn_n,
        "ours_ci": wilson(ours_ok, ours_n), "espn_ci": wilson(espn_ok, espn_n),
        "disagree_ours_right": dis_ours, "disagree_espn_right": dis_espn,
    }


_BUCKETS = [
    ("ours >3 below ESPN", -math.inf, -3.0),
    ("ours 1-3 below", -3.0, -1.0),
    ("within 1 pt", -1.0, 1.0),
    ("ours 1-3 above", 1.0, 3.0),
    ("ours >3 above ESPN", 3.0, math.inf),
]


def delta_buckets(rows: list[dict]) -> list[dict]:
    rows = gradable(rows)
    out = []
    for label, lo, hi in _BUCKETS:
        g = [r for r in rows if lo <= r["delta"] < hi]
        if not g:
            continue
        wins = sum(1 for r in g if abs(r["ours"] - r["actual"]) < abs(r["espn"] - r["actual"]) - EPS)
        losses = sum(1 for r in g if abs(r["espn"] - r["actual"]) < abs(r["ours"] - r["actual"]) - EPS)
        out.append({
            "label": label, "n": len(g),
            "actual_minus_espn": _mean(r["actual"] - r["espn"] for r in g),
            "actual_minus_ours": _mean(r["actual"] - r["ours"] for r in g),
            "ours_closer": wins, "espn_closer": losses,
        })
    return out


def biggest_misses(rows: list[dict], n: int = 5) -> list[dict]:
    g = gradable(rows)
    return sorted(g, key=lambda r: abs(r["ours"] - r["actual"]), reverse=True)[:n]


# --------------------------------------------------------------------------- #
# 4. lineup comparison
# --------------------------------------------------------------------------- #
def _as_projection(r: dict, slot_counts: dict, points: float) -> PlayerProjection:
    elig = json.loads(r["eligible_json"]) if r.get("eligible_json") else []
    if not elig:
        elig = eligible_from_position(r["position"], slot_counts)
    return PlayerProjection(
        name=r["name"], position=r["position"], pro_team=r["pro_team"] or "",
        opponent=r["opponent"] or "", current_slot="BE", eligible_slots=elig,
        on_bye=r["on_bye"], espn_points=r["espn"], our_points=points,
        delta_vs_espn=0.0,
    )


def lineup_comparison(conn, season: int, week: int) -> list[dict]:
    """Per league: actual points of the tool's lineup vs ESPN-only vs yours vs best."""
    meta = tracking.latest_lineup_meta(conn, season, week)
    rows = graded_rows(conn, season, week)
    out = []
    for league_id, m in meta.items():
        lg = [r for r in rows if r["league_id"] == league_id]
        if not lg:
            continue
        slot_counts = json.loads(m["slot_counts_json"] or "{}")
        actual_by_name = {r["name"]: r["actual"] for r in lg}

        def best(points_of) -> float:
            projs = [_as_projection(r, slot_counts, points_of(r)) for r in lg]
            lineup = optimize(projs, slot_counts, week)
            return round(sum(actual_by_name[p.name] for _, p in lineup.starters), 2), lineup

        ours_pts, ours_lineup = best(lambda r: r["ours"])
        espn_pts, espn_lineup = best(lambda r: r["espn"])
        hind_pts, _ = best(lambda r: r["actual"])
        swaps = _swaps(ours_lineup, espn_lineup, lg)

        you = conn.execute(
            "SELECT final_slot, actual_points FROM actuals WHERE season=? AND week=? AND league_id=?",
            (season, week, league_id)).fetchall()
        you_pts = round(sum(r["actual_points"] or 0 for r in you
                            if r["final_slot"] not in NOT_STARTING), 2) if you else None

        out.append({
            "league_id": league_id, "league": m["league_name"], "week": week,
            "ours": ours_pts, "espn": espn_pts, "you": you_pts, "best": hind_pts,
            "projected": m["projected_total"], "n_players": len(lg),
            "provenance": m["provenance"],
            "starters": [(slot, p.name) for slot, p in ours_lineup.starters],
            "swaps": swaps,
        })
    return out


def _swaps(ours_lineup, espn_lineup, lg: list[dict]) -> list[dict]:
    """Starters the tool picked that ESPN's numbers alone would not (and vice versa),
    paired by position where possible, with what each actually scored."""
    info = {r["name"]: r for r in lg}
    tool_names = {p.name for _, p in ours_lineup.starters}
    espn_names = {p.name for _, p in espn_lineup.starters}
    tool_only = sorted(tool_names - espn_names, key=lambda n: -info[n]["ours"])
    espn_only = sorted(espn_names - tool_names, key=lambda n: -info[n]["espn"])
    out: list[dict] = []
    remaining = list(espn_only)
    for t in tool_only:
        match = next((e for e in remaining if info[e]["position"] == info[t]["position"]),
                     remaining[0] if remaining else None)
        if match is not None:
            remaining.remove(match)
        out.append(_swap_row(t, match, info))
    for e in remaining:                       # unpaired (shouldn't normally happen)
        out.append(_swap_row(None, e, info))
    return out


def _swap_row(tool: str | None, espn: str | None, info: dict) -> dict:
    def g(n, k):
        return info[n][k] if n else None
    return {
        "tool": tool, "tool_pos": g(tool, "position"), "tool_actual": g(tool, "actual"),
        "tool_proj": g(tool, "ours"), "tool_espn_proj": g(tool, "espn"),
        "espn": espn, "espn_pos": g(espn, "position"), "espn_actual": g(espn, "actual"),
        "espn_proj": g(espn, "espn"), "espn_our_proj": g(espn, "ours"),
    }


def swap_summary(lineups: list[dict]) -> dict:
    """Season-to-date: how often (and how profitably) our picks differed from ESPN-only."""
    league_weeks = len(lineups)
    changed = [lu for lu in lineups if lu["swaps"]]
    all_swaps = [sw for lu in lineups for sw in lu["swaps"] if sw["tool"] and sw["espn"]]
    won = sum(1 for sw in all_swaps if sw["tool_actual"] > sw["espn_actual"] + EPS)
    lost = sum(1 for sw in all_swaps if sw["espn_actual"] > sw["tool_actual"] + EPS)
    net = sum(sw["tool_actual"] - sw["espn_actual"] for sw in all_swaps)
    lo, hi = wilson(won, won + lost)
    return {
        "league_weeks": league_weeks, "changed": len(changed), "swaps": len(all_swaps),
        "won": won, "lost": lost, "net": net, "ci": (lo, hi),
    }


# --------------------------------------------------------------------------- #
# 5. report
# --------------------------------------------------------------------------- #
def _f(x, w=6, d=2) -> str:
    return f"{x:{w}.{d}f}" if x is not None else " " * (w - 1) + "-"


def _pct(x) -> str:
    return f"{x:.0%}" if x is not None else "-"


def model_version_section(rows: list[dict], lw_rows: list[dict] | None) -> list[str]:
    """Accuracy split by model version, so a model change is graded on its own picks."""
    L: list[str] = []
    for label, rs in (("your rosters", rows), ("league-wide", lw_rows or [])):
        g = gradable(rs)
        versions = sorted({r.get("model") or "v1-hand-tuned" for r in g})
        if len(versions) < 2 and not any(v != "v1-hand-tuned" for v in versions):
            continue
        for v in versions:
            vr = [r for r in g if (r.get("model") or "v1-hand-tuned") == v]
            w = sum(abs(r["ours"] - r["actual"]) + 0.05 < abs(r["espn"] - r["actual"]) for r in vr)
            l = sum(abs(r["espn"] - r["actual"]) + 0.05 < abs(r["ours"] - r["actual"]) for r in vr)
            mo = sum(abs(r["ours"] - r["actual"]) for r in vr) / len(vr)
            me = sum(abs(r["espn"] - r["actual"]) for r in vr) / len(vr)
            L.append(f"  {label:<13} {v:<15} n={len(vr):<5} MAE ours {mo:5.2f} vs ESPN {me:5.2f}   ours closer {w}-{l}")
    if L:
        L.insert(0, "BY MODEL VERSION (v2-no-raise from Week 11 Monday: never projects above ESPN)")
        L.append("")
    return L


def league_wide_section(lw_rows: list[dict], week: int) -> list[str]:
    """Same accuracy checks, run on every roster in both leagues."""
    L: list[str] = []
    g = gradable(lw_rows)
    if not g:
        return L
    weeks = sorted({r["week"] for r in g})
    L.append(f"LEAGUE-WIDE CHECK (every roster in both leagues; weeks {weeks[0]}-{weeks[-1]}; "
             f"{len(g)} graded projections)")
    L.append("  Bigger sample for judging the model. Your own lineup results above are unaffected.")
    L.append(f"{'Pos':<6}{'n':>5}{'MAE ours':>10}{'MAE ESPN':>10}{'bias ours':>11}{'bias ESPN':>11}   ours closer")
    for m in error_metrics(lw_rows):
        ci = (f"{_pct(m['closer_pct'])} ({m['closer_ci'][0]:.0%}-{m['closer_ci'][1]:.0%}, "
              f"{m['ours_closer']}-{m['espn_closer']})") if m["decided"] else "-"
        L.append(f"{m['pos']:<6}{m['n']:>5}{_f(m['mae_ours'],10)}{_f(m['mae_espn'],10)}"
                 f"{_f(m['bias_ours'],11)}{_f(m['bias_espn'],11)}   {ci}")
    rk = ranking_metrics(lw_rows)
    if rk["pairs"]:
        L.append(f"  Ranking: ours {_pct(rk['ours_acc'])} of {rk['ours_n']} pairs, "
                 f"ESPN {_pct(rk['espn_acc'])} of {rk['espn_n']}; where we ordered a pair "
                 f"differently: ours right {rk['disagree_ours_right']}, ESPN right {rk['disagree_espn_right']}")
    L.append(f"{'  When we differ from ESPN':<26}{'n':>5}{'actual-ESPN':>13}{'actual-ours':>13}   ours closer")
    for b in delta_buckets(lw_rows):
        L.append(f"  {b['label']:<24}{b['n']:>5}{_f(b['actual_minus_espn'],13)}"
                 f"{_f(b['actual_minus_ours'],13)}   {b['ours_closer']}-{b['espn_closer']}")
    L.append("")
    return L


def build_report(season: int, week: int, week_rows: list[dict], all_rows: list[dict],
                 lineups: list[dict], weeks_covered: list[int],
                 season_lineups: list[dict] | None = None,
                 lw_rows: list[dict] | None = None) -> str:
    L: list[str] = []
    n_all = len(gradable(all_rows))
    wk_span = f"{min(weeks_covered)}-{max(weeks_covered)}" if weeks_covered else "-"
    L.append(f"START/SIT SCORECARD  --  {season} Week {week}")
    L.append(f"Season to date: weeks {wk_span} · {n_all} graded player-projections")
    L.append("")

    # ---- lineup results ----
    L.append(f"WEEK {week} LINEUPS (actual fantasy points)")
    L.append(f"{'League':<18}{'Tool':>8}{'ESPN-only':>11}{'You':>8}{'Best':>8}{'Tool regret':>13}")
    t = {"ours": 0.0, "espn": 0.0, "you": 0.0, "best": 0.0}
    have_you = True
    for lu in lineups:
        reg = lu["best"] - lu["ours"]
        L.append(f"{lu['league'][:17]:<18}{_f(lu['ours'],8)}{_f(lu['espn'],11)}"
                 f"{_f(lu['you'],8)}{_f(lu['best'],8)}{_f(reg,13)}")
        t["ours"] += lu["ours"]; t["espn"] += lu["espn"]; t["best"] += lu["best"]
        if lu["you"] is None:
            have_you = False
        else:
            t["you"] += lu["you"]
    if len(lineups) > 1:
        L.append(f"{'TOTAL':<18}{_f(t['ours'],8)}{_f(t['espn'],11)}"
                 f"{_f(t['you'] if have_you else None,8)}{_f(t['best'],8)}{_f(t['best']-t['ours'],13)}")
    if not lineups:
        L.append("  (no graded lineups for this week)")
    L.append("  Tool = lineup our projections pick; ESPN-only = lineup ESPN's own numbers pick;")
    L.append("  You = what you actually started; Best = hindsight-optimal from the same roster.")
    L.append("")

    # ---- where we differed from ESPN-only ----
    L.append(f"WHERE THE TOOL DIFFERED FROM ESPN-ONLY, WEEK {week}")
    any_swap = False
    for lu in lineups:
        if not lu["swaps"]:
            L.append(f"  {lu['league']}: same lineup as ESPN-only (no decision changed)")
            continue
        any_swap = True
        L.append(f"  {lu['league']}: {len(lu['swaps'])} starter(s) differ")
        for sw in lu["swaps"]:
            if sw["tool"] and sw["espn"]:
                verdict = ("tool right" if sw["tool_actual"] > sw["espn_actual"] + EPS
                           else "ESPN right" if sw["espn_actual"] > sw["tool_actual"] + EPS else "tie")
                L.append(f"    started {sw['tool']} ({sw['tool_actual']:.1f}) "
                         f"over {sw['espn']} ({sw['espn_actual']:.1f})  [{verdict}, "
                         f"{sw['tool_actual'] - sw['espn_actual']:+.1f}]")
            else:
                who = sw["tool"] or sw["espn"]
                L.append(f"    unpaired: {who}")
    if not lineups:
        L.append("  (no graded lineups for this week)")
    ss = swap_summary(season_lineups if season_lineups is not None else lineups)
    if ss["league_weeks"]:
        rec = (f"won {ss['won']}, lost {ss['lost']}"
               + (f" ({ss['ci'][0]:.0%}-{ss['ci'][1]:.0%} win share)" if ss["won"] + ss["lost"] else ""))
        L.append(f"  Season to date: lineup differed in {ss['changed']} of {ss['league_weeks']} "
                 f"league-weeks; {ss['swaps']} swap(s): {rec}; net {ss['net']:+.1f} pts vs ESPN-only.")
    L.append("  This is the number that matters: a projection edge only helps if it changes who you start.")
    L.append("")

    # ---- error ----
    L.append(f"PROJECTION ACCURACY (season to date; MAE lower = better; bias = proj - actual)")
    L.append(f"{'Pos':<6}{'n':>5}{'MAE ours':>10}{'MAE ESPN':>10}{'bias ours':>11}{'bias ESPN':>11}   ours closer")
    for m in error_metrics(all_rows):
        ci = (f"{_pct(m['closer_pct'])} ({m['closer_ci'][0]:.0%}-{m['closer_ci'][1]:.0%}, "
              f"{m['ours_closer']}-{m['espn_closer']})") if m["decided"] else "-"
        L.append(f"{m['pos']:<6}{m['n']:>5}{_f(m['mae_ours'],10)}{_f(m['mae_espn'],10)}"
                 f"{_f(m['bias_ours'],11)}{_f(m['bias_espn'],11)}   {ci}")
    L.append("  'ours closer' = share of players where our number was nearer the actual than ESPN's")
    L.append("  (95% interval, then wins-losses). An interval that spans 50% means no detectable edge.")
    L.append("")

    # ---- ranking ----
    rk = ranking_metrics(all_rows)
    L.append("RANKING (same position, same league-week: did the higher-projected player outscore the lower?)")
    if rk["pairs"]:
        L.append(f"  Ours  {_pct(rk['ours_acc'])} of {rk['ours_n']} pairs "
                 f"({rk['ours_ci'][0]:.0%}-{rk['ours_ci'][1]:.0%})")
        L.append(f"  ESPN  {_pct(rk['espn_acc'])} of {rk['espn_n']} pairs "
                 f"({rk['espn_ci'][0]:.0%}-{rk['espn_ci'][1]:.0%})")
        L.append(f"  Where we ordered a pair differently from ESPN: ours right {rk['disagree_ours_right']}, "
                 f"ESPN right {rk['disagree_espn_right']}")
    else:
        L.append("  (not enough graded pairs yet)")
    L.append("")

    # ---- delta calibration ----
    L.append("WHEN WE DIFFER FROM ESPN")
    L.append(f"{'':<22}{'n':>4}{'actual-ESPN':>13}{'actual-ours':>13}   ours closer")
    for b in delta_buckets(all_rows):
        L.append(f"{b['label']:<22}{b['n']:>4}{_f(b['actual_minus_espn'],13)}{_f(b['actual_minus_ours'],13)}"
                 f"   {b['ours_closer']}-{b['espn_closer']}")
    L.append("  If 'actual-ESPN' sits near 0 for a bucket where we differ, our adjustment was noise;")
    L.append("  if 'actual-ours' is near 0, the adjustment was right.")
    L.append("")

    # ---- model versions ----
    L += model_version_section(all_rows, lw_rows)

    # ---- league-wide ----
    if lw_rows:
        L += league_wide_section(lw_rows, week)

    # ---- misses ----
    L.append(f"BIGGEST MISSES, WEEK {week} (ours vs actual)")
    for r in biggest_misses(week_rows):
        L.append(f"  {r['name'][:22]:<22} {r['position']:<5} proj {r['ours']:5.1f}  "
                 f"ESPN {r['espn']:5.1f}  actual {r['actual']:5.1f}  ({r['league']})")
    L.append("")

    # ---- honesty ----
    if n_all < 150:
        L.append(f"CAUTION: only {n_all} graded projections. Weekly fantasy scores are noisy (a typical "
                 "player varies by several points week to week), so differences here are directional, "
                 "not proof. Don't retune the model off a few weeks of data.")
    if any(lu["provenance"] == "backfill" for lu in lineups) or any(
            r["provenance"] == "backfill" for r in all_rows):
        L.append("NOTE: some weeks were reconstructed from old email HTML (no factor detail); "
                 "positions/eligibility there are inferred.")
    ungraded = [r for r in all_rows if not r["complete"] and not r["on_bye"]]
    if ungraded:
        L.append(f"NOTE: {len(ungraded)} player-weeks excluded because their game wasn't finished "
                 "when actuals were fetched; re-run `--evaluate --week N` later.")
    return "\n".join(L)


# --------------------------------------------------------------------------- #
# 6. orchestration
# --------------------------------------------------------------------------- #
def run_evaluate(settings, conn, season: int, week: int | None, current_week: int,
                 fetch: bool = True, email: bool = False) -> str:
    """Fetch actuals as needed, then build + print + save the scorecard."""
    explicit = week is not None
    if fetch:
        todo = [week] if explicit else [w for w in weeks_missing_actuals(conn, season)
                                         if w < current_week]
        for w in todo:
            fetch_actuals(settings, conn, season, w)
        try:
            from . import league_wide

            conn.executescript(league_wide.LW_SCHEMA)
            if explicit:
                league_wide.fetch_actuals(settings, conn, season, week)
            else:
                league_wide.fetch_missing(settings, conn, season, current_week)
        except Exception as e:  # noqa: BLE001
            print(f"  ! league-wide actuals failed: {e.__class__.__name__}: {e}")

    graded_weeks = [w for w in tracking.logged_weeks(conn, season)
                    if conn.execute("SELECT 1 FROM actuals WHERE season=? AND week=? LIMIT 1",
                                    (season, w)).fetchone()]
    if not graded_weeks:
        msg = ("Nothing to grade yet: no predictions with matching actuals. "
               "Run `--backfill` for past emails, or wait for a completed week.")
        print(msg)
        return msg
    if explicit:
        if week not in graded_weeks:
            msg = f"Week {week}: no predictions with matching actuals to grade."
            print(msg)
            return msg
        target = week
    else:
        finished = [w for w in graded_weeks if w < current_week]
        target = max(finished) if finished else max(graded_weeks)
    use = [w for w in graded_weeks if w <= target]

    all_rows: list[dict] = []
    for w in use:
        all_rows += graded_rows(conn, season, w)
    week_rows = [r for r in all_rows if r["week"] == target]
    season_lineups: list[dict] = []
    for w in use:
        season_lineups += lineup_comparison(conn, season, w)
    lineups = [lu for lu in season_lineups if lu["week"] == target]
    lw_rows: list[dict] = []
    try:
        from . import league_wide

        conn.executescript(league_wide.LW_SCHEMA)
        lw_rows = league_wide.season_rows(conn, season, target)
    except Exception as e:  # noqa: BLE001
        print(f"  ! league-wide grading failed: {e.__class__.__name__}: {e}")
    text = build_report(season, target, week_rows, all_rows, lineups, use, season_lineups, lw_rows)
    print(text)

    REPORT_DIR.mkdir(exist_ok=True)
    out = REPORT_DIR / f"scorecard_{season}_wk{target}.txt"
    out.write_text(text)
    print(f"\nScorecard written: {out}")

    if email:
        import html as _html

        from .email_send import send_email

        body = (f"<html><body><pre style=\"font-family:ui-monospace,Menlo,monospace;font-size:12px\">"
                f"{_html.escape(text)}</pre></body></html>")
        send_email(settings, f"Start/Sit scorecard — Week {target}", body, text)
    return text


# ---- automatic weekly run, driven from `run.py --scheduled` ------------------ #
def _eval_state_path(season: int) -> Path:
    return STATE_DIR / f"eval_{season}.json"


def _load_eval_state(season: int) -> dict:
    p = _eval_state_path(season)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def week_ready_time(season: int, week: int) -> datetime | None:
    """Tuesday 8am ET after the week's last kickoff (stat corrections have settled)."""
    from .slate import ET, _games_for_week

    games = _games_for_week(season, week)
    if not games:
        return None
    last = max(g.kickoff for g in games).astimezone(ET)
    return (last + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)


def maybe_auto_evaluate(settings, season: int, now: datetime, current_week: int,
                        throttle: timedelta = timedelta(minutes=60)) -> bool:
    """Grade any finished, logged week that hasn't been graded. Safe to call on every
    scheduled tick: it is a cheap no-op unless a week is due, and attempts are
    throttled so a failing ESPN call isn't retried every 10 minutes."""
    try:
        conn = tracking.connect()
        state = _load_eval_state(season)
        done = set(state.get("weeks_done", []))
        due = []
        for w in tracking.logged_weeks(conn, season):
            if w in done or w >= current_week:
                continue
            ready = week_ready_time(season, w)
            if ready and now >= ready:
                due.append(w)
        if not due:
            return False
        last = state.get("last_attempt")
        if last and now - datetime.fromisoformat(last) < throttle:
            return False

        state["last_attempt"] = now.isoformat()
        STATE_DIR.mkdir(exist_ok=True)
        _eval_state_path(season).write_text(json.dumps(state, indent=2))

        print(f"Auto-evaluating completed week(s): {due}")
        run_evaluate(settings, conn, season, None, current_week, fetch=True, email=True)
        still_missing = set(weeks_missing_actuals(conn, season))
        newly = [w for w in due if w not in still_missing]   # every league fetched
        state["weeks_done"] = sorted(done | set(newly))
        _eval_state_path(season).write_text(json.dumps(state, indent=2))
        return True
    except Exception as e:  # noqa: BLE001 -- never let grading break the send path
        print(f"  ! auto-evaluate failed: {e.__class__.__name__}: {e}")
        return False
