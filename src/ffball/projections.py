"""Turn ESPN's raw stat projection into our adjusted fantasy-point projection.

Pipeline per player:
  1. start from ESPN's projected stat line for the week (volume + TDs)
  2. multiply by shared-data factors: recent form, defense-vs-position,
     weather, snap-share trend
  3. apply an injury status factor
  4. score the adjusted stat line with THIS league's live scoring rules
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import LeagueConfig
from .scoring import score_stat_line
from .shared_data import SharedData, norm_name

_VOLUME_KEYS = {
    "passingYards", "passingAttempts", "passingCompletions",
    "rushingYards", "rushingAttempts",
    "receivingYards", "receivingReceptions", "receivingTargets",
}
_TD_KEYS = {"passingTouchdowns", "rushingTouchdowns", "receivingTouchdowns"}

_OUT_STATUSES = {"OUT", "IR", "INJURY_RESERVE", "SUSPENSION", "SUSPENDED", "PUP", "NFI", "DOUBTFUL"}
_Q_STATUSES = {"QUESTIONABLE", "GTD", "DAY_TO_DAY"}


@dataclass
class PlayerProjection:
    name: str
    position: str
    pro_team: str
    opponent: str
    current_slot: str
    eligible_slots: list[str]
    on_bye: bool
    espn_points: float
    our_points: float
    delta_vs_espn: float
    factors: dict = field(default_factory=dict)     # name -> multiplier
    flags: list[str] = field(default_factory=list)  # human-readable callouts
    scoring_breakdown: dict = field(default_factory=dict)
    kickoff: object = None                          # tz-aware datetime | None


def _match_name(rp_name: str, player_id: int, sd: SharedData) -> str:
    by_id = sd.espn_id_to_name.get(int(player_id)) if player_id else None
    return by_id or norm_name(rp_name)


def _injury_factor(espn_status: str, report_status: str) -> tuple[float, str | None]:
    s = (espn_status or "").upper().replace(" ", "_")
    r = (report_status or "").upper().replace(" ", "_")
    worst = {s, r}
    if worst & _OUT_STATUSES:
        label = "DOUBTFUL" if "DOUBTFUL" in worst and not (worst & (_OUT_STATUSES - {"DOUBTFUL"})) else "OUT"
        return (0.15 if label == "DOUBTFUL" else 0.0), label
    if worst & _Q_STATUSES:
        return 0.92, "QUESTIONABLE"
    return 1.0, None


def project_player(rp, cfg: LeagueConfig, sd: SharedData) -> PlayerProjection:
    name_key = _match_name(rp.name, rp.player_id, sd)
    pos = rp.position
    breakdown = dict(rp.proj_breakdown)
    factors: dict[str, float] = {}
    flags: list[str] = []
    kickoff = sd.kickoff(rp.pro_team)

    # ---- bye week ----
    if rp.on_bye or (not rp.pro_opponent and rp.position != "D/ST" and not breakdown):
        if rp.on_bye:
            flags.append("BYE week")
            return PlayerProjection(
                rp.name, pos, rp.pro_team, "BYE", rp.current_slot,
                rp.eligible_slots, True, rp.espn_proj_points, 0.0,
                -rp.espn_proj_points, {"bye": 0.0}, flags, {}, None,
            )

    # ---- recent form ----
    form = sd.form.get(name_key)
    if form and form["games"] >= 2:
        f = 1.0 + 0.5 * form["delta_pct"]
        factors["form"] = round(f, 3)
        if abs(form["delta_pct"]) >= 0.20:
            arrow = "hot" if form["delta_pct"] > 0 else "cold"
            flags.append(f"{arrow}: {form['recent_ppg']} last-3 vs {form['season_ppg']} avg")

    # ---- defense vs position ----
    dvp = sd.dvp.get((pos, _opp_team(rp, sd)))
    if dvp:
        factors["dvp"] = dvp["mult"]
        if dvp["rank"] <= max(4, dvp["of"] // 8):
            flags.append(f"plus matchup: {rp.pro_opponent} allows {dvp['ppg_allowed']}/g to {pos} (#{dvp['rank']} softest)")
        elif dvp["rank"] >= dvp["of"] - max(3, dvp["of"] // 8):
            flags.append(f"tough matchup: {rp.pro_opponent} allows {dvp['ppg_allowed']}/g to {pos} (#{dvp['rank']})")

    # ---- weather ----
    wx = sd.player_weather(rp.pro_team)
    wx_vol = wx_td = 1.0
    if wx and wx.get("severity"):
        sev = wx["severity"]
        if pos in ("QB", "WR", "TE", "K"):
            wx_vol = 1.0 - sev
            wx_td = 1.0 - sev * 0.7
            flags.append(f"weather: {wx['summary']} at {_home_team(rp, sd)}")
        elif pos == "RB":
            wx_vol = 1.0 + sev * 0.3  # game script leans run
        factors["weather"] = round(wx_vol, 3)

    # ---- snap trend ----
    snap = sd.snaps.get(name_key)
    if snap and abs(snap["trend_pct"]) >= 0.12 and pos in ("RB", "WR", "TE"):
        f = 1.0 + max(-0.12, min(0.12, snap["trend_pct"] * 0.5))
        factors["snaps"] = round(f, 3)
        direction = "rising" if snap["trend_pct"] > 0 else "falling"
        flags.append(f"snap share {direction} ({snap['recent_pct']:.0%} recent, {snap['trend_pct']:+.0%})")

    # ---- injury ----
    report = sd.injuries.get(name_key, {})
    inj_factor, inj_label = _injury_factor(rp.injury_status, report.get("status", ""))
    if inj_label:
        detail = f" ({report['injury']})" if report.get("injury") else ""
        flags.append(f"{inj_label}{detail}")

    # ---- combine ----
    vol_mult = _prod(factors.get("form", 1), factors.get("dvp", 1), wx_vol, factors.get("snaps", 1))
    td_mult = _prod(factors.get("form", 1), factors.get("dvp", 1), wx_td)
    for k in list(breakdown):
        if k in _VOLUME_KEYS:
            breakdown[k] = float(breakdown[k]) * vol_mult
        elif k in _TD_KEYS:
            breakdown[k] = float(breakdown[k]) * td_mult
    breakdown = {k: v * inj_factor for k, v in breakdown.items()}

    our_points, per_item, warns = score_stat_line(breakdown, cfg.scoring_format)
    for w in warns:
        if w not in flags:
            flags.append(w)

    # If ESPN gave no stat breakdown (common in the preseason), fall back to
    # scaling ESPN's own point projection by the same factors.
    if not rp.proj_breakdown:
        our_points = round(rp.espn_proj_points * _prod(vol_mult, inj_factor), 2)
        per_item = {}

    return PlayerProjection(
        name=rp.name,
        position=pos,
        pro_team=rp.pro_team,
        opponent=rp.pro_opponent or "BYE",
        current_slot=rp.current_slot,
        eligible_slots=rp.eligible_slots,
        on_bye=rp.on_bye,
        espn_points=round(rp.espn_proj_points, 2),
        our_points=our_points,
        delta_vs_espn=round(our_points - rp.espn_proj_points, 2),
        factors={**factors, "injury": round(inj_factor, 3)},
        flags=flags,
        scoring_breakdown=per_item,
        kickoff=kickoff,
    )


def project_roster(league_data, sd: SharedData) -> list[PlayerProjection]:
    return [project_player(rp, league_data.config, sd) for rp in league_data.roster]


def _opp_team(rp, sd: SharedData) -> str:
    m = sd.matchups.get(rp.pro_team)
    return m["opp"] if m else rp.pro_opponent


def _home_team(rp, sd: SharedData) -> str:
    m = sd.matchups.get(rp.pro_team)
    if not m:
        return rp.pro_team
    return rp.pro_team if m["home"] else m["opp"]


def _prod(*xs: float) -> float:
    out = 1.0
    for x in xs:
        out *= x
    return out
