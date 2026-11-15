"""Pure analysis behind the weekly team report (no ESPN, no network).

Everything works on plain dataclasses (PlayerLine / TeamSnapshot / LeagueSnapshot)
so it can be tested offline; team_fetch.py fills them from ESPN.

Player value
------------
A single "expected points per game going forward" number per player, in THIS
league's scoring (ESPN already applies each league's rules to its averages):

    value = (w * season average  +  (1 - w) * ESPN projected average) * injury factor

where w grows with games played (0.3 after 1 game ... 0.7 from 4 games on), so
early in the season ESPN's projection dominates and later the player's real
production does. It is deliberately simple and explainable, not a forecast.

Everything downstream -- positional strength, free-agent upgrades, trade
search -- is "how much does the best lineup improve" under that value.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import cached_property
from itertools import combinations

from .evaluate import eligible_from_position
from .startsit import _DEDICATED, _SLOT_TOKEN

POSITIONS = ("QB", "RB", "WR", "TE", "K", "D/ST")
DEFAULT_K = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "D/ST": 1}
UNAVAILABLE = {"OUT", "IR", "INJURY_RESERVE", "SUSPENSION", "SUSPENDED", "PUP", "NFI"}
INJURY_FACTOR = {"DOUBTFUL": 0.3, "QUESTIONABLE": 0.95, "DAY_TO_DAY": 0.95, "GTD": 0.95}

# thresholds (points per week of lineup improvement)
MIN_FA_GAIN = 0.75
MIN_COVER_GAIN = 2.0
MIN_TRADE_GAIN = 1.0
MIN_THEIR_GAIN = 0.5
THEIR_SHARE = 0.35          # they must gain at least this share of what we gain
OVERPAY = 1.2               # value we ask for <= this x value we give (+1 pt slack)
WIN_EPS = 0.05


# --------------------------------------------------------------------------- #
# data model
# --------------------------------------------------------------------------- #
@dataclass(eq=False)          # identity semantics: players are compared with `is` / id()
class PlayerLine:
    name: str
    position: str
    pro_team: str = ""
    eligible_slots: list = field(default_factory=list)
    injury: str = "ACTIVE"
    slot: str = ""                       # current lineup slot ("BE", "IR", "QB", ...)
    season_avg: float = 0.0
    proj_avg: float = 0.0
    games: int = 0
    percent_owned: float = -1.0
    week_points: float | None = None     # actual points in the reviewed week (our box score)
    week_proj: float | None = None       # ESPN projection (reviewed week for ours, next week for FAs)
    player_id: int = 0

    @property
    def injury_key(self) -> str:
        return (self.injury or "ACTIVE").upper().replace(" ", "_")

    @property
    def available(self) -> bool:
        return self.injury_key not in UNAVAILABLE and self.slot != "IR"

    @cached_property
    def value(self) -> float:
        if self.games >= 4:
            w = 0.7
        elif self.games >= 2:
            w = 0.5
        elif self.games >= 1:
            w = 0.3
        else:
            w = 0.0
        if self.season_avg <= 0:
            base = self.proj_avg
        elif self.proj_avg <= 0:
            base = self.season_avg
        else:
            base = w * self.season_avg + (1 - w) * self.proj_avg
        if not self.available:
            return 0.0
        return round(base * INJURY_FACTOR.get(self.injury_key, 1.0), 3)

    def __repr__(self) -> str:                          # keeps test failures readable
        return f"PlayerLine({self.name}, {self.position}, v={self.value})"


@dataclass
class TeamSnapshot:
    team_id: int
    name: str
    wins: int = 0
    losses: int = 0
    ties: int = 0
    points_for: float = 0.0
    points_against: float = 0.0
    standing: int = 0
    owner: str = ""
    scores: list = field(default_factory=list)       # per completed week, in order
    outcomes: list = field(default_factory=list)     # "W"/"L"/"T" per completed week
    roster: list = field(default_factory=list)       # [PlayerLine]

    @property
    def record(self) -> str:
        return f"{self.wins}-{self.losses}" + (f"-{self.ties}" if self.ties else "")


@dataclass
class LeagueSnapshot:
    league_name: str
    league_id: str
    week: int                                         # the completed week being reviewed
    slot_counts: dict
    teams: list                                       # [TeamSnapshot]
    my_id: int
    my_box: list = field(default_factory=list)        # [PlayerLine] with slot/week_points/week_proj
    my_score: float | None = None
    opp_score: float | None = None
    opp_name: str = ""
    free_agents: list = field(default_factory=list)   # [PlayerLine], projections are next week's
    bye_map: dict = field(default_factory=dict)       # NFL team -> bye week
    notes: list = field(default_factory=list)

    @property
    def me(self) -> TeamSnapshot:
        return next(t for t in self.teams if t.team_id == self.my_id)

    @property
    def others(self) -> list:
        return [t for t in self.teams if t.team_id != self.my_id]


# --------------------------------------------------------------------------- #
# lineup engine (same greedy as startsit.optimize, but lean enough for search)
# --------------------------------------------------------------------------- #
def ensure_eligibility(players: list, slot_counts: dict) -> None:
    for p in players:
        if not p.eligible_slots:
            p.eligible_slots = eligible_from_position(p.position, slot_counts)


def _elig(p: PlayerLine, slot: str) -> bool:
    return _SLOT_TOKEN.get(slot, slot) in p.eligible_slots or p.position == slot


def best_lineup(players, slot_counts, key=None, avail=None):
    """Return (total, [(slot, player), ...]) using the same slot ordering as
    startsit.optimize: dedicated slots first, then flex slots by fewest candidates."""
    key = key or (lambda p: p.value)
    ok = avail or (lambda p: p.available)
    pool = sorted((p for p in players if ok(p)), key=key, reverse=True)
    dedicated = [s for s in slot_counts if s in _DEDICATED]
    flex = [s for s in slot_counts if s not in _DEDICATED]

    slots: list[str] = []
    for s in dedicated:
        slots += [s] * slot_counts[s]
    for s in sorted(flex, key=lambda s: sum(1 for p in pool if _elig(p, s))):
        slots += [s] * slot_counts[s]

    used: set[int] = set()
    picks: list = []
    for slot in slots:
        pick = next((p for p in pool if id(p) not in used and _elig(p, slot)), None)
        if pick is not None:
            used.add(id(pick))
            picks.append((slot, pick))
    return sum(key(p) for _, p in picks), picks


# --------------------------------------------------------------------------- #
# positional strength
# --------------------------------------------------------------------------- #
def _k_per_position(slot_counts: dict) -> dict:
    return {pos: (slot_counts.get(pos) or DEFAULT_K[pos]) for pos in POSITIONS}


def team_strength(players: list, slot_counts: dict) -> dict:
    """Per-position strength of one roster: the summed value of its top-k players at
    each position (k = dedicated starting slots; default 1/2/2/1/1/1 if the league
    has none), plus a 'FLEX/depth' row for the remaining starting spots."""
    ks = _k_per_position(slot_counts)
    live = [p for p in players if p.available]
    out: dict = {}
    taken: set[int] = set()
    for pos in POSITIONS:
        group = sorted((p for p in live if p.position == pos), key=lambda p: p.value, reverse=True)[: ks[pos]]
        taken.update(id(p) for p in group)
        out[pos] = {"value": sum(p.value for p in group), "players": group, "k": ks[pos]}

    total_slots = sum(slot_counts.values())
    flex_n = max(0, total_slots - sum(ks.values()))
    if flex_n:
        rest = sorted((p for p in live if id(p) not in taken
                       and p.position in ("RB", "WR", "TE", "QB")
                       and any(_elig(p, s) for s in slot_counts if s not in _DEDICATED)),
                      key=lambda p: p.value, reverse=True)[:flex_n]
        out["FLEX"] = {"value": sum(p.value for p in rest), "players": rest, "k": flex_n}
    out["_total"] = best_lineup(players, slot_counts)[0]
    return out


def position_table(snap: LeagueSnapshot) -> list[dict]:
    """My strength vs the league at every position: rank, gap to average, verdict."""
    strengths = {t.team_id: team_strength(t.roster, snap.slot_counts) for t in snap.teams}
    n_teams = len(snap.teams)
    cut = max(1, math.ceil(n_teams / 3))
    rows = []
    keys = [k for k in (*POSITIONS, "FLEX") if k in strengths[snap.my_id]]
    for pos in keys:
        vals = {tid: s[pos]["value"] for tid, s in strengths.items() if pos in s}
        mine = vals[snap.my_id]
        ranked = sorted(vals.values(), reverse=True)
        rank = 1 + sum(1 for v in ranked if v > mine + 1e-9)
        avg = sum(vals.values()) / len(vals)
        diff = mine - avg
        verdict = "neutral"
        if abs(diff) >= 1.0:
            if rank <= cut and diff > 0:
                verdict = "strength"
            elif rank > n_teams - cut and diff < 0:
                verdict = "weakness"
        rows.append({
            "pos": pos, "mine": mine, "avg": avg, "diff": diff, "rank": rank, "of": n_teams,
            "verdict": verdict,
            "players": [p.name for p in strengths[snap.my_id][pos]["players"]],
        })
    tot = {tid: s["_total"] for tid, s in strengths.items()}
    mine_t = tot[snap.my_id]
    rows.append({
        "pos": "LINEUP", "mine": mine_t, "avg": sum(tot.values()) / len(tot),
        "diff": mine_t - sum(tot.values()) / len(tot),
        "rank": 1 + sum(1 for v in tot.values() if v > mine_t + 1e-9), "of": n_teams,
        "verdict": "neutral", "players": [],
    })
    return rows


# --------------------------------------------------------------------------- #
# results & luck
# --------------------------------------------------------------------------- #
def standings_info(snap: LeagueSnapshot) -> dict:
    me, teams = snap.me, snap.teams
    n = len(teams)
    order = sorted(teams, key=lambda t: (t.standing if t.standing else 99, -t.wins))
    place = next(i for i, t in enumerate(order, 1) if t.team_id == me.team_id)
    pf_rank = 1 + sum(1 for t in teams if t.points_for > me.points_for)
    pa_rank = 1 + sum(1 for t in teams if t.points_against > me.points_against)  # 1 = toughest schedule

    weeks = min([len(t.scores) for t in teams] + [snap.week])
    games = 0
    exp_wins = 0.0
    for w in range(weeks):
        mine = me.scores[w]
        others = [t.scores[w] for t in teams if t.team_id != me.team_id]
        if not others:
            continue
        beat = sum(1.0 if mine > s + WIN_EPS else 0.5 if abs(mine - s) <= WIN_EPS else 0.0 for s in others)
        exp_wins += beat / len(others)
        games += 1
    actual = me.wins + 0.5 * me.ties
    return {
        "record": me.record, "place": place, "of": n, "pf_rank": pf_rank, "pa_rank": pa_rank,
        "points_for": me.points_for, "points_against": me.points_against,
        "expected_wins": exp_wins, "games": games, "actual_wins": actual,
        "luck": actual - exp_wins,
        "avg_score": (sum(me.scores[:weeks]) / weeks) if weeks else 0.0,
    }


# --------------------------------------------------------------------------- #
# last week's lineup decisions
# --------------------------------------------------------------------------- #
def week_recap(snap: LeagueSnapshot) -> dict:
    box = snap.my_box
    started = [p for p in box if p.slot not in ("BE", "IR", "", None)]
    bench = [p for p in box if p.slot == "BE"]
    pts = lambda p: p.week_points or 0.0   # noqa: E731
    got = sum(pts(p) for p in started)
    ensure_eligibility(box, snap.slot_counts)
    best, picks = best_lineup(box, snap.slot_counts, key=pts, avail=lambda p: p.slot != "IR")
    optimal_ids = {id(p) for _, p in picks}

    benched_good = sorted((p for p in bench if id(p) in optimal_ids), key=pts, reverse=True)
    started_bad = sorted((p for p in started if id(p) not in optimal_ids), key=pts)
    mistakes = []
    pool = list(started_bad)
    for b in benched_good:
        match = next((s for s in pool if _compatible(b, s)), None)
        if match is None:
            continue
        pool.remove(match)
        if pts(b) - pts(match) >= 3.0:
            mistakes.append({"benched": b.name, "bench_pts": pts(b), "started": match.name,
                             "started_pts": pts(match), "cost": pts(b) - pts(match)})
    mistakes.sort(key=lambda m: m["cost"], reverse=True)

    top = sorted(started, key=pts, reverse=True)[:3]
    busts = sorted((p for p in started if p.week_proj and p.week_proj > 0),
                   key=lambda p: pts(p) - p.week_proj)[:3]
    busts = [p for p in busts if pts(p) - p.week_proj <= -4.0]
    won = None
    if snap.my_score is not None and snap.opp_score is not None:
        won = "W" if snap.my_score > snap.opp_score else "L" if snap.my_score < snap.opp_score else "T"
    return {
        "score": snap.my_score if snap.my_score is not None else got,
        "opp_score": snap.opp_score, "opp_name": snap.opp_name, "result": won,
        "started_pts": got, "optimal_pts": best,
        "efficiency": (got / best) if best > 0 else None,
        "left_on_bench": max(0.0, best - got),
        "mistakes": mistakes[:3],
        "top": [(p.name, p.position, pts(p)) for p in top],
        "busts": [(p.name, p.position, pts(p), p.week_proj) for p in busts],
    }


def _compatible(a: PlayerLine, b: PlayerLine) -> bool:
    return bool(set(a.eligible_slots) & set(b.eligible_slots) - {"BE", "IR"}) or a.position == b.position


# --------------------------------------------------------------------------- #
# looking ahead
# --------------------------------------------------------------------------- #
def outlook(snap: LeagueSnapshot, horizon: int = 3) -> dict:
    """Injury flags on my likely starters and bye-week pressure over the next weeks."""
    roster = snap.me.roster
    _, picks = best_lineup(roster, snap.slot_counts)
    starters = [p for _, p in picks]
    flagged = [p for p in roster if p.injury_key not in ("ACTIVE", "NORMAL", "")
               and (p in starters or p.value > 6)]
    next_week = snap.week + 1
    byes = []
    for wk in range(next_week, next_week + horizon):
        who = [p.name for p in starters if snap.bye_map.get(p.pro_team) == wk]
        if who:
            byes.append({"week": wk, "players": who})
    return {"injuries": [(p.name, p.position, p.injury_key.title()) for p in flagged],
            "byes": byes}


# --------------------------------------------------------------------------- #
# free agents
# --------------------------------------------------------------------------- #
def free_agent_recs(snap: LeagueSnapshot, limit: int = 3) -> list[dict]:
    me = snap.me
    roster = me.roster
    base, picks = best_lineup(roster, snap.slot_counts)
    start_ids = {id(p) for _, p in picks}
    nxt = snap.week + 1

    def avail_next(p):
        return p.available and snap.bye_map.get(p.pro_team) != nxt

    base_next, _ = best_lineup(roster, snap.slot_counts, avail=avail_next)
    droppable = sorted((p for p in roster if id(p) not in start_ids and p.slot != "IR"
                        and p.available), key=lambda p: p.value)[:4]
    if not droppable:
        return []

    recs = []
    for fa in snap.free_agents:
        if not fa.available or fa.value <= 0:
            continue
        best = None
        for d in droppable:
            new = [p for p in roster if p is not d] + [fa]
            gain = best_lineup(new, snap.slot_counts)[0] - base
            gain_next = best_lineup(new, snap.slot_counts, avail=avail_next)[0] - base_next
            cand = (gain + 0.5 * gain_next, gain, gain_next, d)
            if best is None or cand[0] > best[0] + 1e-9:
                best = cand
        score, gain, gain_next, drop = best
        if gain >= MIN_FA_GAIN:
            kind = "upgrade"
        elif gain_next >= MIN_COVER_GAIN:
            kind = "cover"
        else:
            continue
        new = [p for p in roster if p is not drop] + [fa]
        _, new_picks = best_lineup(new, snap.slot_counts)
        replaced = [p.name for _, p in picks if p not in [q for _, q in new_picks]]
        slot = next((s for s, p in new_picks if p is fa), None)
        recs.append({
            "add": fa, "drop": drop, "kind": kind, "gain": gain, "gain_next": gain_next,
            "score": score, "slot": slot, "bumps": replaced,
        })

    recs.sort(key=lambda r: (r["score"], r["add"].percent_owned), reverse=True)
    out, seen_pos = [], {}
    for r in recs:                                   # at most 2 per position, no repeat drops
        pos = r["add"].position
        if seen_pos.get(pos, 0) >= 2 or any(o["drop"] is r["drop"] for o in out):
            continue
        seen_pos[pos] = seen_pos.get(pos, 0) + 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# trades
# --------------------------------------------------------------------------- #
def _tradable(p: PlayerLine) -> bool:
    return p.available and p.position in ("QB", "RB", "WR", "TE") and p.value > 0


def find_trades(snap: LeagueSnapshot, limit: int = 3) -> list[dict]:
    """1-for-1 and 2-for-1 (we send two, get one better player) trades that raise BOTH
    teams' best-lineup value, so the other manager has a reason to say yes."""
    sc = snap.slot_counts
    me = snap.me
    my_base, my_picks = best_lineup(me.roster, sc)
    mine = [p for p in me.roster if _tradable(p)]
    table = {r["pos"]: r for r in position_table(snap)}
    found = []

    for t in snap.others:
        their_base, _ = best_lineup(t.roster, sc)
        theirs = [p for p in t.roster if _tradable(p)]
        best = None

        def consider(give, get):
            nonlocal best
            vg, vr = sum(p.value for p in give), sum(p.value for p in get)
            if vr > OVERPAY * vg + 1.0:
                return
            gid, rid = {id(p) for p in give}, {id(p) for p in get}
            mine_after = [p for p in me.roster if id(p) not in gid] + list(get)
            gain = best_lineup(mine_after, sc)[0] - my_base
            if gain < MIN_TRADE_GAIN:
                return
            theirs_after = [p for p in t.roster if id(p) not in rid] + list(give)
            their_gain = best_lineup(theirs_after, sc)[0] - their_base
            if their_gain < max(MIN_THEIR_GAIN, THEIR_SHARE * gain):
                return
            score = gain + 0.5 * min(gain, their_gain)
            if best is None or score > best["score"]:
                best = {"partner": t.name, "give": list(give), "get": list(get), "gain": gain,
                        "their_gain": their_gain, "score": score,
                        "value_give": vg, "value_get": vr}

        for g in mine:
            for r in theirs:
                consider([g], [r])
        for g1, g2 in combinations(mine, 2):
            lo = max(g1.value, g2.value)
            for r in theirs:
                if r.value > lo:                      # the one player we get must be the best of the three
                    consider([g1, g2], [r])
        if best:
            found.append(best)

    found.sort(key=lambda r: r["score"], reverse=True)
    out = found[:limit]
    for tr in out:
        tr["why"] = _trade_why(tr, snap, table, my_picks, my_base)
    return out


def _trade_why(tr: dict, snap: LeagueSnapshot, table: dict, my_picks, my_base) -> list[str]:
    why = []
    sc = snap.slot_counts
    gid = {id(p) for p in tr["give"]}
    after = [p for p in snap.me.roster if id(p) not in gid] + tr["get"]
    _, new_picks = best_lineup(after, sc)
    before_ids = {id(p) for _, p in my_picks}
    after_ids = {id(p) for _, p in new_picks}
    entering = [p.name for _, p in new_picks if id(p) not in before_ids]
    leaving = [p.name for _, p in my_picks if id(p) not in after_ids and id(p) not in gid]
    why.append(f"Your lineup improves by about {tr['gain']:.1f} pts/week "
               f"({', '.join(entering)} in" + (f"; {', '.join(leaving)} to the bench" if leaving else "") + ").")
    for pos in dict.fromkeys(p.position for p in tr["get"]):
        row = table.get(pos)
        if row and row["verdict"] == "weakness":
            why.append(f"Fills a weak spot: your {pos} group ranks #{row['rank']} of {row['of']}.")
    for pos in dict.fromkeys(p.position for p in tr["give"]):       # once per position
        row = table.get(pos)
        if row and row["verdict"] == "strength":
            why.append(f"You're sending out from strength: your {pos} group ranks #{row['rank']} of {row['of']}.")
    why.append(f"{tr['partner']} improves by about {tr['their_gain']:.1f} pts/week too, "
               "which is your selling point.")
    if len(tr["give"]) > len(tr["get"]):
        why.append("They would need to drop a bench player to make room.")
    return why


# --------------------------------------------------------------------------- #
# the whole picture
# --------------------------------------------------------------------------- #
def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def analyze(snap: LeagueSnapshot) -> dict:
    for t in snap.teams:
        ensure_eligibility(t.roster, snap.slot_counts)
    ensure_eligibility(snap.free_agents, snap.slot_counts)
    ensure_eligibility(snap.my_box, snap.slot_counts)

    notes = list(snap.notes)
    out = {"snap": snap, "notes": notes}

    def guarded(key, fn, default):
        try:
            out[key] = fn()
        except Exception as e:  # noqa: BLE001 -- one failed section must not sink the report
            notes.append(f"{key} unavailable: {e.__class__.__name__}: {e}")
            out[key] = default

    guarded("standing", lambda: standings_info(snap), None)
    guarded("recap", lambda: week_recap(snap), None)
    guarded("positions", lambda: position_table(snap), [])
    guarded("outlook", lambda: outlook(snap), {"injuries": [], "byes": []})
    guarded("free_agents", lambda: free_agent_recs(snap), [])
    guarded("trades", lambda: find_trades(snap), [])
    out["assessment"] = build_assessment(out)
    return out


def build_assessment(a: dict) -> list[str]:
    """Plain-English read of the numbers above (rule-based, no model involved)."""
    lines: list[str] = []
    st, rc = a.get("standing"), a.get("recap")
    pos = a.get("positions") or []

    if st:
        s = (f"You're {st['record']}, {ordinal(st['place'])} of {st['of']}. Your {st['avg_score']:.1f} "
             f"points per week rank {ordinal(st['pf_rank'])} in the league.")
        if st["games"] >= 2:
            diff = st["luck"]
            if diff >= 1.0:
                s += (f" Your scoring would be good for about {st['expected_wins']:.1f} wins against "
                      f"everyone, so you've been lucky by {diff:.1f}; some regression is likely.")
            elif diff <= -1.0:
                s += (f" Your scoring would be good for about {st['expected_wins']:.1f} wins against "
                      f"everyone, so you've been unlucky by {abs(diff):.1f}; the record should improve.")
            else:
                s += " Your record matches your scoring, so it's a fair reflection of the team."
        lines.append(s)

    if rc and rc["result"]:
        word = {"W": "Won", "L": "Lost", "T": "Tied"}[rc["result"]]
        s = (f"Last week you {word.lower()} {rc['score']:.1f}-{rc['opp_score']:.1f}"
             + (f" against {rc['opp_name']}" if rc["opp_name"] else "") + ".")
        if rc["efficiency"] is not None:
            if rc["left_on_bench"] >= 8:
                s += (f" You left {rc['left_on_bench']:.1f} points on the bench "
                      f"({rc['efficiency']:.0%} lineup efficiency).")
            else:
                s += f" Lineup efficiency was {rc['efficiency']:.0%}, so your start/sit calls were close to ideal."
        lines.append(s)

    strengths = [r for r in pos if r["verdict"] == "strength"]
    weaknesses = [r for r in pos if r["verdict"] == "weakness"]
    if strengths:
        lines.append("Strengths: " + "; ".join(
            f"{r['pos']} (#{r['rank']} of {r['of']}, {r['diff']:+.1f} pts/wk vs the average team)"
            for r in sorted(strengths, key=lambda r: r["rank"])) + ".")
    if weaknesses:
        lines.append("Weaknesses: " + "; ".join(
            f"{r['pos']} (#{r['rank']} of {r['of']}, {r['diff']:+.1f} pts/wk vs the average team)"
            for r in sorted(weaknesses, key=lambda r: -r["rank"])) + ".")
    if not strengths and not weaknesses and pos:
        lines.append("No position stands out as clearly strong or weak against the league.")

    fas, trades = a.get("free_agents") or [], a.get("trades") or []
    if fas:
        r = fas[0]
        lines.append(f"Best waiver move: add {r['add'].name} ({r['add'].position}) and drop "
                     f"{r['drop'].name}, worth about {max(r['gain'], r['gain_next']):.1f} pts/week.")
    if trades:
        t = trades[0]
        lines.append(f"Top trade idea: your {' + '.join(p.name for p in t['give'])} for "
                     f"{t['partner']}'s {' + '.join(p.name for p in t['get'])} "
                     f"(+{t['gain']:.1f} pts/week for you, +{t['their_gain']:.1f} for them).")
    return lines
