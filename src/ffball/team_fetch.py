"""Fill team_analysis snapshots from ESPN (the only place this feature touches the network).

Every ESPN call that isn't essential is wrapped so a failure becomes a note in the
report instead of a crash: you still get the sections that did load.
"""

from __future__ import annotations

from .team_analysis import POSITIONS, LeagueSnapshot, PlayerLine, TeamSnapshot


def _num(x, default: float = 0.0) -> float:
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def to_line(p, *, slot: str | None = None, week_points=None, week_proj=None) -> PlayerLine:
    """espn_api Player / BoxPlayer -> PlayerLine (duck-typed; see espn_api/football/player.py)."""
    stats = getattr(p, "stats", None) or {}
    season = stats.get(0) or {}
    avg = _num(getattr(p, "avg_points", None)) or _num(season.get("avg_points"))
    total = _num(getattr(p, "total_points", None)) or _num(season.get("points"))
    proj_avg = _num(getattr(p, "projected_avg_points", None)) or _num(season.get("projected_avg_points"))
    games = int(round(total / avg)) if avg > 0 and total > 0 else 0
    return PlayerLine(
        name=getattr(p, "name", "?"),
        position=getattr(p, "position", "") or "",
        pro_team=getattr(p, "proTeam", "") or "",
        eligible_slots=list(getattr(p, "eligibleSlots", []) or []),
        injury=getattr(p, "injuryStatus", None) or "ACTIVE",
        slot=slot if slot is not None else (getattr(p, "lineupSlot", "") or ""),
        season_avg=avg, proj_avg=proj_avg, games=games,
        percent_owned=_num(getattr(p, "percent_owned", -1), -1.0),
        week_points=week_points, week_proj=week_proj,
        player_id=int(getattr(p, "playerId", 0) or 0),
    )


def _owner_name(t) -> str:
    """Human owner name(s) for an espn_api Team (owners are ESPN member dicts)."""
    names = []
    for o in getattr(t, "owners", None) or []:
        if isinstance(o, dict):
            n = f"{o.get('firstName', '')} {o.get('lastName', '')}".strip() or o.get("displayName", "")
            if n and n not in names:
                names.append(" ".join(n.split()))
    return ", ".join(names)


def _record_check(snap: LeagueSnapshot) -> list[str]:
    """Cross-check ESPN's W-L column against the per-week outcomes; return notes on any mismatch."""
    out = []
    for t in snap.teams:
        if len(t.outcomes) < snap.week:
            continue
        w, l = t.outcomes[:snap.week].count("W"), t.outcomes[:snap.week].count("L")
        if (w, l) != (t.wins, t.losses):
            out.append(f"{t.name}: ESPN's record column says {t.wins}-{t.losses} but weekly results "
                       f"through week {snap.week} add up to {w}-{l} (e.g. games from a later week counted).")
    return out


def bye_weeks(season: int) -> dict[str, int]:
    """NFL team -> its bye week, from the cached nflverse schedule."""
    from .shared_data import espn_team
    from .slate import _load_schedule

    sched = _load_schedule(season)
    if "game_type" in sched:
        sched = sched[sched["game_type"] == "REG"]
    home = sched["home_team"].map(espn_team)
    away = sched["away_team"].map(espn_team)
    weeks = sorted(int(w) for w in sched["week"].unique())
    out: dict[str, int] = {}
    for team in set(home) | set(away):
        played = {int(w) for w in sched.loc[(home == team) | (away == team), "week"]}
        missing = [w for w in weeks if w not in played]
        if len(missing) == 1:
            out[team] = missing[0]
    return out


def _find_box(league, team_id: int, week: int):
    for box in league.box_scores(week=week):
        for side, other in (("home", "away"), ("away", "home")):
            team = getattr(box, f"{side}_team", None)
            if getattr(team, "team_id", None) == team_id:
                opp = getattr(box, f"{other}_team", None)
                return {
                    "lineup": getattr(box, f"{side}_lineup"),
                    "score": getattr(box, f"{side}_score", None),
                    "opp_score": getattr(box, f"{other}_score", None),
                    "opp_name": getattr(opp, "team_name", "") or "",
                }
    raise ValueError(f"team_id {team_id} not found in week {week} box scores")


def fetch_snapshot(cfg, settings, season: int, week: int, bye_map: dict | None = None,
                   fa_size: int = 25) -> LeagueSnapshot:
    """Review of completed `week` for one league; free agents priced for week + 1."""
    from .espn import _roster_slot_counts, connect

    league = connect(cfg, season, settings.espn_s2, settings.swid)
    notes: list[str] = []
    slot_counts = _roster_slot_counts(league)

    teams: list[TeamSnapshot] = []
    for t in league.teams:
        teams.append(TeamSnapshot(
            team_id=t.team_id, name=t.team_name, wins=t.wins, losses=t.losses, ties=t.ties,
            points_for=_num(t.points_for), points_against=_num(t.points_against),
            standing=int(getattr(t, "standing", 0) or 0),
            owner=_owner_name(t),
            scores=[_num(s) for s in list(t.scores)[:week]],
            outcomes=list(t.outcomes)[:week],
            roster=[to_line(p) for p in t.roster],
        ))
    snap = LeagueSnapshot(
        league_name=cfg.league_name, league_id=str(cfg.league_id), week=week,
        slot_counts=slot_counts, teams=teams, my_id=cfg.team_id,
        bye_map=bye_map or {}, notes=notes,
    )
    if not any(t.team_id == cfg.team_id for t in teams):
        raise ValueError(f"team_id {cfg.team_id} not in league {cfg.league_id}")

    # ---- last week's box score: what we started, scored, and who we played ----
    try:
        box = _find_box(league, cfg.team_id, week)
        season_by_id = {p.player_id: p for p in snap.me.roster}
        for bp in box["lineup"]:
            line = to_line(bp, slot=getattr(bp, "slot_position", "") or "",
                           week_points=_num(getattr(bp, "points", 0)),
                           week_proj=_num(getattr(bp, "projected_points", 0)))
            known = season_by_id.get(line.player_id)
            if known:                                  # box scores lack season averages
                line.season_avg, line.proj_avg, line.games = known.season_avg, known.proj_avg, known.games
                line.percent_owned = known.percent_owned
            snap.my_box.append(line)
        snap.my_score, snap.opp_score, snap.opp_name = box["score"], box["opp_score"], box["opp_name"]
    except Exception as e:  # noqa: BLE001
        notes.append(f"Last week's box score unavailable: {e.__class__.__name__}: {e}")

    # ---- free agents (ESPN returns most-owned first), priced for the coming week ----
    seen: set[int] = set()
    for pos in POSITIONS:
        try:
            for bp in league.free_agents(week=week + 1, size=fa_size, position=pos):
                line = to_line(bp, slot="FA", week_proj=_num(getattr(bp, "projected_points", 0)))
                if line.player_id in seen:
                    continue
                seen.add(line.player_id)
                snap.free_agents.append(line)
        except Exception as e:  # noqa: BLE001
            notes.append(f"Free agents ({pos}) unavailable: {e.__class__.__name__}: {e}")
    if not snap.free_agents:
        notes.append("No free-agent data was returned, so waiver suggestions are skipped.")
    if not bye_map:
        notes.append("Bye-week data unavailable; bye warnings skipped.")
    notes.extend(_record_check(snap))
    return snap
