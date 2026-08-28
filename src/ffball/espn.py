"""ESPN league access: live roster, live scoring rules, live roster slots."""

from __future__ import annotations

from dataclasses import dataclass, field

from espn_api.football import League

from .config import LeagueConfig
from .constants import NON_STARTING_SLOTS, SLOT_ID_TO_POS
from .scoring import infer_scoring_type


@dataclass
class RosteredPlayer:
    name: str
    player_id: int
    position: str                 # QB / RB / WR / TE / K / D/ST / LB ...
    pro_team: str                 # e.g. "KC"
    eligible_slots: list[str]
    current_slot: str             # where the manager has them now
    injury_status: str            # ACTIVE / QUESTIONABLE / OUT / IR / ...
    pro_opponent: str             # this week's opponent, "" if bye
    on_bye: bool
    espn_proj_points: float       # ESPN's own projection under this league
    proj_breakdown: dict[str, float] = field(default_factory=dict)  # raw stat proj
    percent_started: float = -1.0


@dataclass
class LeagueData:
    config: LeagueConfig
    week: int
    roster: list[RosteredPlayer]
    starting_slot_counts: dict[str, int]   # {"QB":1,"RB":2,"WR":2,"FLEX":1,...}


def connect(cfg: LeagueConfig, season: int, espn_s2, swid) -> League:
    return League(
        league_id=int(cfg.league_id),
        year=season,
        espn_s2=espn_s2,
        swid=swid,
    )


def _roster_slot_counts(league: League) -> dict[str, int]:
    """Live starting-lineup slot counts, parsed straight from mSettings."""
    data = league.espn_request.league_get(params={"view": "mSettings"})
    raw = data["settings"]["rosterSettings"]["lineupSlotCounts"]
    counts: dict[str, int] = {}
    for slot_id, n in raw.items():
        if not n:
            continue
        pos = SLOT_ID_TO_POS.get(int(slot_id), slot_id)
        if pos in NON_STARTING_SLOTS:
            continue
        # normalise the common flex label
        label = {"RB/WR/TE": "FLEX", "OP": "SUPERFLEX"}.get(pos, pos)
        counts[label] = counts.get(label, 0) + int(n)
    return counts


def _extract_projection(player, week: int) -> tuple[float, dict]:
    stats = getattr(player, "stats", {}) or {}
    wk = stats.get(week, {})
    proj_pts = wk.get("projected_points", 0) or getattr(player, "projected_points", 0) or 0
    breakdown = wk.get("projected_breakdown", {}) or {}
    return float(proj_pts), dict(breakdown)


def load_league(
    cfg: LeagueConfig,
    season: int,
    week: int | None,
    espn_s2,
    swid,
) -> LeagueData:
    league = connect(cfg, season, espn_s2, swid)

    target_week = week or getattr(league, "current_week", None) or getattr(league, "nfl_week", 1) or 1

    # ---- live scoring rules (never hardcoded) ----
    cfg.scoring_format = list(league.settings.scoring_format)
    cfg.scoring_type = infer_scoring_type(cfg.scoring_format)

    # ---- live roster slots ----
    slot_counts = _roster_slot_counts(league)
    cfg.roster_slots = slot_counts
    cfg.is_superflex = "SUPERFLEX" in slot_counts
    cfg.idp_enabled = any(
        p in slot_counts for p in ("DT", "DE", "LB", "DL", "CB", "S", "DB", "DP")
    )

    # ---- live roster + per-player projection ----
    players: list[RosteredPlayer] = []
    box_players = _load_box_lineup(league, cfg.team_id, target_week)
    if box_players is not None:
        for bp in box_players:
            proj_pts, breakdown = _extract_projection(bp, target_week)
            players.append(
                RosteredPlayer(
                    name=bp.name,
                    player_id=bp.playerId,
                    position=bp.position or (bp.eligibleSlots[0] if bp.eligibleSlots else "?"),
                    pro_team=bp.proTeam,
                    eligible_slots=list(bp.eligibleSlots),
                    current_slot=getattr(bp, "slot_position", "") or "",
                    injury_status=getattr(bp, "injuryStatus", "") or "ACTIVE",
                    pro_opponent=getattr(bp, "pro_opponent", "") or "",
                    on_bye=getattr(bp, "on_bye_week", False),
                    espn_proj_points=proj_pts,
                    proj_breakdown=breakdown,
                    percent_started=getattr(bp, "percent_started", -1.0),
                )
            )
    else:
        team = next((t for t in league.teams if t.team_id == cfg.team_id), None)
        if team is None:
            ids = ", ".join(str(t.team_id) for t in league.teams)
            raise ValueError(
                f"team_id {cfg.team_id} not in league {cfg.league_id}. "
                f"Valid team_id values: {ids}"
            )
        if not team.roster:
            # preseason: the season view sometimes lags; force a roster-week pull
            try:
                league.load_roster_week(target_week)
                team = next(t for t in league.teams if t.team_id == cfg.team_id)
            except Exception:
                pass
        for p in team.roster:
            proj_pts, breakdown = _extract_projection(p, target_week)
            players.append(
                RosteredPlayer(
                    name=p.name,
                    player_id=p.playerId,
                    position=p.position or "?",
                    pro_team=p.proTeam,
                    eligible_slots=list(p.eligibleSlots),
                    current_slot=getattr(p, "lineupSlot", "") or "",
                    injury_status=getattr(p, "injuryStatus", "") or "ACTIVE",
                    pro_opponent="",
                    on_bye=False,
                    espn_proj_points=proj_pts,
                    proj_breakdown=breakdown,
                    percent_started=getattr(p, "percent_started", -1.0),
                )
            )

    return LeagueData(
        config=cfg,
        week=target_week,
        roster=players,
        starting_slot_counts=slot_counts,
    )


def _load_box_lineup(league: League, team_id: int, week: int):
    """Return the list of BoxPlayer for our team this week, or None on failure."""
    try:
        boxes = league.box_scores(week=week)
    except Exception:
        return None
    for box in boxes:
        home = getattr(box, "home_team", None)
        away = getattr(box, "away_team", None)
        if getattr(home, "team_id", None) == team_id:
            return box.home_lineup
        if getattr(away, "team_id", None) == team_id:
            return box.away_lineup
    return None
