"""NFL game-day awareness: kickoff times, slates, and send windows.

The tool sends one email for every day that has an NFL game, read from the
actual NFL schedule (Thursday, Sunday and Monday most weeks, plus Wednesday,
Friday, Saturday, Tuesday or holiday games whenever the schedule has them).
Each day's email covers only the players whose games are that day.

  * Sunday         — deadline = earlier of {10:00am ET} and
                     {90m before the first Sunday kickoff} (London/early games).
  * any other day  — deadline = earlier of {5:00pm ET that day} and
                     {2h before that day's first kickoff}.

Window keys are lowercase day names ("thursday", "sunday", "monday", ...).

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
    key: str                    # lowercase ET day name: "thursday", "sunday", "monday", ...
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


DAY_KEYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WINDOW_CHOICES = DAY_KEYS + ["pre_weekend"]   # pre_weekend: legacy alias, see below


def legacy_sent_keys(sent: set[str], windows: list["SendWindow"]) -> set[str]:
    """Translate send-state written by the old two-email schedule.

    The old "pre_weekend" email covered every game before Sunday, and the old
    "sunday" email covered Sunday and Monday. Pre-Sunday days are treated as sent
    when "pre_weekend" was sent. Monday is deliberately NOT inherited from "sunday":
    Monday games now get their own, fresher email."""
    out = set(sent)
    if "pre_weekend" in sent:
        out |= {w.key for w in windows if w.key not in ("sunday", "monday", "tuesday")}
    return out


def build_send_windows(season: int, week: int, now: datetime | None = None) -> list[SendWindow]:
    """One send window per ET calendar day that has at least one game this week."""
    now = now or datetime.now(ET)
    games = _games_for_week(season, week)
    if not games:
        return []

    by_day: dict = {}
    for g in games:
        by_day.setdefault(g.kickoff.astimezone(ET).date(), []).append(g)

    windows: list[SendWindow] = []
    for day in sorted(by_day):
        day_games = sorted(by_day[day], key=lambda g: g.kickoff)
        first = day_games[0].kickoff.astimezone(ET)
        key = DAY_KEYS[first.weekday()]
        if key == "sunday":
            deadline = min(first.replace(hour=10, minute=0, second=0, microsecond=0),
                           first - timedelta(minutes=90))
        else:
            deadline = min(first.replace(hour=17, minute=0, second=0, microsecond=0),
                           first - timedelta(hours=2))
        # A week never has the same weekday twice, but guard anyway.
        if any(w.key == key for w in windows):
            key = f"{key}_{first:%m%d}"
        windows.append(SendWindow(key, _slate_label(day_games), deadline, day_games))
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
