"""Diagnose ESPN connection / roster issues.

    python3 -m uv run diagnose.py
"""

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from espn_api.football import League  # noqa: E402
from ffball.config import load_settings  # noqa: E402

s = load_settings()
print(f"season={s.season}  auth: espn_s2={'set' if s.espn_s2 else 'MISSING'} "
      f"swid={'set' if s.swid else 'MISSING'}\n")

for cfg in s.leagues:
    print("=" * 70)
    print(f"{cfg.league_name}  league_id={cfg.league_id}  team_id={cfg.team_id}")
    print("=" * 70)
    try:
        lg = League(league_id=int(cfg.league_id), year=s.season,
                    espn_s2=s.espn_s2, swid=s.swid)
    except Exception as e:
        print(f"  CONNECT FAILED: {type(e).__name__}: {e}")
        print("  -> wrong league_id, wrong season, or expired cookies\n")
        continue

    print(f"  connected. current_week={getattr(lg, 'current_week', '?')} "
          f"nfl_week={getattr(lg, 'nfl_week', '?')} "
          f"scoringPeriodId={getattr(lg, 'scoringPeriodId', '?')}")
    print(f"  teams in league: {len(lg.teams)}")
    for t in lg.teams:
        mark = "  <-- YOU" if t.team_id == cfg.team_id else ""
        print(f"    team_id={t.team_id:>3}  {t.team_name!r:<32} roster={len(t.roster)}{mark}")

    mine = [t for t in lg.teams if t.team_id == cfg.team_id]
    if not mine:
        print(f"\n  !! No team with team_id={cfg.team_id}. Use one of the team_id values above.\n")
        continue
    team = mine[0]
    print(f"\n  YOUR ROSTER ({team.team_name}): {len(team.roster)} players")
    for p in team.roster[:40]:
        wk = getattr(p, "stats", {}).get(getattr(lg, "current_week", 1), {})
        proj = wk.get("projected_points", "-")
        print(f"    {p.name:<26} {p.position:<5} {p.proTeam:<4} "
              f"slot={getattr(p, 'lineupSlot', '?'):<4} proj={proj}")

    try:
        boxes = lg.box_scores(week=getattr(lg, "current_week", 1))
        found = False
        for b in boxes:
            for side in ("home", "away"):
                tm = getattr(b, f"{side}_team", None)
                if getattr(tm, "team_id", None) == cfg.team_id:
                    found = True
                    lineup = getattr(b, f"{side}_lineup")
                    print(f"\n  box_scores lineup: {len(lineup)} players "
                          f"(projected: {sum(x.projected_points for x in lineup):.1f})")
        if not found:
            print("\n  box_scores: your team not found in any matchup this week "
                  "(normal before week 1 schedule is set)")
    except Exception as e:
        print(f"\n  box_scores failed: {type(e).__name__}: {e} "
              "(normal in the preseason — tool falls back to team.roster)")
    print()
