"""NFL game-day awareness: kickoff times, slates, and send windows.

The tool sends up to two emails per NFL week:

  * "pre-weekend"  — covers Thu / Fri / Sat games.
                     Deadline = earlier of {5:00pm ET on the game's day}
                     and {2h before the earliest of those kickoffs}.
  * "sunday"       — covers Sun / Mon games.
                     Deadline = earlier of {10:00am ET Sunday}
                     and {90m before the earliest Sunday kickoff}.

A scheduled run (`run.py --scheduled`) sends a window's email once, on the last
poll before its deadline, and never twice (state in .state/).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from .shared_data import _cached, _import, espn_team

ET = ZoneInfo("America/New_York")


@dataclass
class Game:
    home: str
    away: str
    kickoff: datetime           # tz-aware, ET
    weekday: str
    game_type: str

    def has_team(self, team: str) -> bool:
        return team in (self.home, self.away)


@dataclass
class SendWindow:
    key: str                    # "pre_weekend" | "sunday"
    label: str
    deadline: datetime          # tz-aware ET — email should arrive before this
    games: list[Game] = field(default_factory=list)

    @property
    def teams(self) -> set[str]:
        s: set[str] = set()
        for g in self.games:
            s.add(g.home)
            s.add(g.away)
        return s

    def earliest_kickoff(self) -> datetime | None:
        return min((g.kickoff for g in self.games), default=None)


def _load_schedule(season: int) -> pd.DataFrame:
    return _cached(f"sched_{season}", lambda: _import("import_schedules", [season]))


def _games_for_week(season: int, week: int) -> list[Game]:
    sched = _load_schedule(season)
    wk = sched[sched["week"] == week]
    games: list[Game] = []
    for _, r in wk.iterrows():
        gt = str(r.get("gametime") or "13:00")
        if ":" not in gt:
            gt = "13:00"
        try:
            kickoff = datetime.strptime(f"{r['gameday']} {gt}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
        except Exception:
            continue
        games.append(
            Game(
                home=espn_team(r["home_team"]),
                away=espn_team(r["away_team"]),
                kickoff=kickoff,
                weekday=str(r.get("weekday") or kickoff.strftime("%A")),
                game_type=str(r.get("game_type") or "REG"),
            )
        )
    return sorted(games, key=lambda g: g.kickoff)


def current_nfl_week(season: int, now: datetime | None = None) -> int:
    """First week that still has a game whose kickoff is in the future."""
    now = now or datetime.now(ET)
    sched = _load_schedule(season)
    reg = sched[sched["game_type"].isin(["REG", "POST"])] if "game_type" in sched else sched
    for week in sorted(reg["week"].unique()):
        games = _games_for_week(season, int(week))
        if games and max(g.kickoff for g in games) >= now:
            return int(week)
    return int(sched["week"].max()) if not sched.empty else 1


def build_send_windows(season: int, week: int, now: datetime | None = None) -> list[SendWindow]:
    """Split a week's games at the Sunday-00:00-ET boundary into two send windows.

    * pre_weekend: everything before Sunday (Wed/Thu/Fri/Sat openers all land here)
    * sunday:      Sunday + Monday (+ any rare Tuesday)
    """
    now = now or datetime.now(ET)
    games = _games_for_week(season, week)
    if not games:
        return []

    earliest = min(g.kickoff for g in games)
    days_to_sunday = (6 - earliest.astimezone(ET).weekday()) % 7
    sunday_start = (earliest.astimezone(ET) + timedelta(days=days_to_sunday)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    pre = [g for g in games if g.kickoff < sunday_start]
    sun = [g for g in games if g.kickoff >= sunday_start]
    windows: list[SendWindow] = []

    if pre:
        first = min(g.kickoff for g in pre).astimezone(ET)
        deadline = min(
            first.replace(hour=17, minute=0, second=0, microsecond=0),
            first - timedelta(hours=2),
        )
        windows.append(SendWindow("pre_weekend", _slate_label(pre), deadline, pre))

    if sun:
        first = min(g.kickoff for g in sun)
        deadline = min(
            sunday_start.replace(hour=10),
            first - timedelta(minutes=90),
        )
        windows.append(SendWindow("sunday", _slate_label(sun), deadline, sun))

    return windows


def _slate_label(games: list[Game]) -> str:
    order = ["Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Monday", "Tuesday"]
    days = sorted({g.weekday for g in games},
                  key=lambda d: order.index(d) if d in order else 9)
    return (" / ".join(days) if days else "week") + " games"


def lock_label(kickoff: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now(ET)
    k = kickoff.astimezone(ET)
    if k <= now:
        return "LOCKED"
    day = "today" if k.date() == now.date() else k.strftime("%a")
    return f"locks {day} {k.strftime('%-I:%M%p').lower()} ET"


def game_for_team(games: list[Game], team: str) -> Game | None:
    return next((g for g in games if g.has_team(team)), None)
