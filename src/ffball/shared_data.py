"""Step 1: pull league-agnostic data ONCE per run.

  * nfl_data_py weekly stats  -> recent form + defense-vs-position
  * nfl_data_py snap counts   -> snap-share trend
  * nfl_data_py injuries      -> latest injury report
  * nfl_data_py schedules     -> matchups + roof type
  * Open-Meteo forecast       -> wind / precip for outdoor games this week

Everything is keyed by player name or NFL team -- never by any roster -- and
cached to ./.cache so re-runs within a few hours are instant and offline-ish.
"""

from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

CACHE_DIR = Path(__file__).resolve().parents[2] / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
CACHE_TTL = timedelta(hours=12)

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# nfl_data_py team abbreviations -> ESPN (espn_api PRO_TEAM_MAP) abbreviations.
_TEAM_FIX = {"LA": "LAR", "WAS": "WSH", "JAC": "JAX", "OAK": "LV", "SD": "LAC", "STL": "LAR"}


def espn_team(abbr) -> str:
    a = str(abbr).strip().upper()
    return _TEAM_FIX.get(a, a)


def norm_name(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = re.sub(r"[.'`]", "", s.lower())
    s = re.sub(r"[^a-z ]", " ", s)
    parts = [p for p in s.split() if p and p not in _SUFFIXES]
    return " ".join(parts)


def _s(v) -> str:
    """Coerce a possibly-NaN/None cell to a string."""
    if v is None:
        return ""
    try:
        import math as _m
        if isinstance(v, float) and _m.isnan(v):
            return ""
    except (TypeError, ValueError):
        pass
    return str(v)


def _cached(name: str, builder):
    path = CACHE_DIR / f"{name}.parquet"
    if path.exists() and datetime.now() - datetime.fromtimestamp(path.stat().st_mtime) < CACHE_TTL:
        try:
            return pd.read_parquet(path)
        except Exception:
            pass
    df = builder()
    try:
        df.to_parquet(path)
    except Exception:
        pass
    return df


def _import(fn_name: str, seasons: list[int]) -> pd.DataFrame:
    import nfl_data_py as nfl

    fn = getattr(nfl, fn_name)
    return fn(seasons)


# nflverse moved current weekly player stats out of the frozen `player_stats`
# release (which nfl_data_py 0.3.2 hardcodes) into the `stats_player` release.
# Try the library first, then fall back to the new asset URL directly.
_WEEKLY_FALLBACK = (
    "https://github.com/nflverse/nflverse-data/releases/download/"
    "stats_player/stats_player_week_{year}.parquet"
)
_WEEKLY_COL_ALIASES = {
    "passing_interceptions": "interceptions",
    "team": "recent_team",
    "sacks_suffered": "sacks",
}


def _load_weekly(season: int) -> pd.DataFrame:
    try:
        df = _import("import_weekly_data", [season])
        if not df.empty:
            return df
    except Exception:
        pass
    df = pd.read_parquet(_WEEKLY_FALLBACK.format(year=season))
    for new, old in _WEEKLY_COL_ALIASES.items():
        if new in df.columns and old not in df.columns:
            df[old] = df[new]
    return df


@dataclass
class SharedData:
    season: int
    stats_season: int
    week: int
    form: dict = field(default_factory=dict)        # norm_name -> form dict
    dvp: dict = field(default_factory=dict)         # (pos, def_team) -> dvp dict
    snaps: dict = field(default_factory=dict)       # norm_name -> snap dict
    injuries: dict = field(default_factory=dict)    # norm_name -> injury dict
    weather: dict = field(default_factory=dict)     # home_team -> weather dict
    matchups: dict = field(default_factory=dict)    # team -> {"opp":.., "home":bool}
    espn_id_to_name: dict = field(default_factory=dict)  # int espn_id -> norm_name
    games: list = field(default_factory=list)        # slate.Game for the target week
    notes: list = field(default_factory=list)

    def player_weather(self, pro_team: str) -> dict | None:
        m = self.matchups.get(pro_team)
        if not m:
            return None
        home_team = pro_team if m["home"] else m["opp"]
        return self.weather.get(home_team)

    def kickoff(self, pro_team: str):
        for g in self.games:
            if g.has_team(pro_team):
                return g.kickoff
        return None


# --------------------------------------------------------------------------- #
# builders
# --------------------------------------------------------------------------- #
def _pick_stats_season(season: int, forced: int | None) -> tuple[int, list[str]]:
    notes: list[str] = []
    if forced:
        return forced, [f"stats season forced to {forced}"]
    try:
        wk = _cached(f"weekly_{season}", lambda: _load_weekly(season))
        reg = wk[wk["season_type"] == "REG"] if "season_type" in wk else wk
        if reg["week"].nunique() >= 1:
            return season, notes
    except Exception as e:  # noqa: BLE001
        notes.append(f"weekly {season} unavailable ({e.__class__.__name__})")
    notes.append(f"{season} has no games yet -> using {season - 1} game logs for projections")
    return season - 1, notes


def _build_form(stats_season: int, week: int) -> dict:
    wk = _cached(f"weekly_{stats_season}", lambda: _load_weekly(stats_season))
    if "season_type" in wk:
        wk = wk[wk["season_type"] == "REG"]
    wk = wk.sort_values("week")
    out: dict = {}
    for name, g in wk.groupby("player_display_name"):
        pts = g["fantasy_points_ppr"].fillna(0)
        if len(pts) == 0:
            continue
        season_ppg = float(pts.mean())
        recent = pts.tail(3)
        recent_ppg = float(recent.mean()) if len(recent) else season_ppg
        delta = (recent_ppg - season_ppg) / season_ppg if season_ppg > 3 else 0.0
        out[norm_name(name)] = {
            "recent_ppg": round(recent_ppg, 1),
            "season_ppg": round(season_ppg, 1),
            "delta_pct": round(max(-0.5, min(0.5, delta)), 3),
            "games": int(len(pts)),
        }
    return out


def _build_dvp(stats_season: int) -> dict:
    """Defense vs position: fantasy points (PPR) allowed per game, ranked."""
    wk = _cached(f"weekly_{stats_season}", lambda: _load_weekly(stats_season))
    if "season_type" in wk:
        wk = wk[wk["season_type"] == "REG"]
    wk = wk[wk["position"].isin(["QB", "RB", "WR", "TE"])]
    grp = (
        wk.groupby(["opponent_team", "position"])
        .agg(pts=("fantasy_points_ppr", "sum"), games=("week", "nunique"))
        .reset_index()
    )
    grp["ppg"] = grp["pts"] / grp["games"].clip(lower=1)
    out: dict = {}
    for pos, g in grp.groupby("position"):
        g = g.sort_values("ppg", ascending=False).reset_index(drop=True)  # rank 1 = softest
        n = len(g)
        league_avg = float(g["ppg"].mean())
        for rank, row in g.iterrows():
            ppg = float(row["ppg"])
            mult = 1.0 + max(-0.15, min(0.15, (ppg - league_avg) / league_avg * 0.5)) if league_avg else 1.0
            out[(pos, espn_team(row["opponent_team"]))] = {
                "rank": int(rank) + 1,
                "of": n,
                "ppg_allowed": round(ppg, 1),
                "mult": round(float(mult), 3),
            }
    return out


def _build_snaps(stats_season: int) -> dict:
    try:
        sc = _cached(f"snaps_{stats_season}", lambda: _import("import_snap_counts", [stats_season]))
    except Exception:
        return {}
    sc = sc[sc["game_type"] == "REG"] if "game_type" in sc else sc
    sc = sc.sort_values("week")
    out: dict = {}
    for name, g in sc.groupby("player"):
        pct = g["offense_pct"].fillna(0)
        if len(pct) == 0:
            continue
        recent = float(pct.tail(3).mean())
        prior = float(pct.head(max(1, len(pct) - 3)).mean()) if len(pct) > 3 else recent
        out[norm_name(name)] = {
            "recent_pct": round(recent, 2),
            "trend_pct": round(recent - prior, 2),
        }
    return out


def _build_injuries(season: int) -> dict:
    try:
        inj = _cached(f"inj_{season}", lambda: _import("import_injuries", [season]))
    except Exception:
        return {}
    if inj.empty or "full_name" not in inj.columns:
        return {}
    sort_cols = [c for c in ("week", "date_modified", "season_type") if c in inj.columns]
    if sort_cols:
        inj = inj.sort_values(sort_cols)
    out: dict = {}
    for name, g in inj.groupby("full_name"):
        row = g.iloc[-1]
        status = (_s(row.get("report_status")) or _s(row.get("practice_status"))).strip()
        if not status:
            continue
        out[norm_name(name)] = {
            "status": status,
            "injury": _s(row.get("report_primary_injury")).strip(),
            "week": int(row["week"]),
        }
    return out


def _build_matchups_and_weather(season: int, week: int) -> tuple[dict, dict, list[str]]:
    from .constants import STADIUMS

    notes: list[str] = []
    try:
        sched = _cached(f"sched_{season}", lambda: _import("import_schedules", [season]))
    except Exception as e:  # noqa: BLE001
        return {}, {}, [f"schedule unavailable ({e.__class__.__name__})"]

    games = sched[(sched["week"] == week) & (sched["game_type"].isin(["REG", "POST"]))]
    if games.empty:
        games = sched[sched["week"] == week]
    matchups: dict = {}
    weather: dict = {}
    for _, g in games.iterrows():
        home, away = espn_team(g["home_team"]), espn_team(g["away_team"])
        matchups[home] = {"opp": away, "home": True}
        matchups[away] = {"opp": home, "home": False}

        stad = STADIUMS.get(home)
        roof = (str(g.get("roof")) or "").lower()
        is_indoor = roof in ("dome", "closed") or (stad and stad["roof"] == "dome")
        if is_indoor:
            weather[home] = {"roof": "indoor", "severity": 0.0, "summary": "indoor / climate controlled"}
            continue
        if not stad:
            continue
        wx = _fetch_forecast(stad["lat"], stad["lon"], str(g.get("gameday")), str(g.get("gametime")))
        if wx is None:
            weather[home] = {"roof": "outdoor", "severity": 0.0, "summary": "forecast not yet available"}
            continue
        weather[home] = wx
    if not any(w.get("summary", "").startswith("wind") or w.get("severity") for w in weather.values()):
        notes.append("no significant weather flagged this week (or forecast window not open)")
    return matchups, weather, notes


def _fetch_forecast(lat: float, lon: float, gameday: str, gametime: str) -> dict | None:
    try:
        date = datetime.strptime(gameday, "%Y-%m-%d").date()
    except Exception:
        return None
    if not (0 <= (date - datetime.now().date()).days <= 15):
        return None
    try:
        hour = int((gametime or "13:00").split(":")[0])
    except Exception:
        hour = 13
    cache = CACHE_DIR / f"wx_{lat:.2f}_{lon:.2f}_{gameday}.parquet"
    try:
        if cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600:
            df = pd.read_parquet(cache)
        else:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat, "longitude": lon,
                    "hourly": "temperature_2m,precipitation_probability,wind_speed_10m,wind_gusts_10m",
                    "wind_speed_unit": "mph", "temperature_unit": "fahrenheit",
                    "precipitation_unit": "inch", "start_date": gameday, "end_date": gameday,
                    "timezone": "America/New_York",
                },
                timeout=15,
            )
            r.raise_for_status()
            df = pd.DataFrame(r.json()["hourly"])
            df.to_parquet(cache)
    except Exception:
        return None

    window = df.iloc[max(0, hour - 1): hour + 3]
    if window.empty:
        window = df
    wind = float(window["wind_speed_10m"].max())
    gust = float(window["wind_gusts_10m"].max())
    precip = float(window["precipitation_probability"].max())
    temp = float(window["temperature_2m"].mean())

    severity = 0.0
    bits = []
    if wind >= 15:
        severity += min(0.20, (wind - 12) / 60)
        bits.append(f"wind {wind:.0f} mph (gust {gust:.0f})")
    if precip >= 50:
        severity += 0.05
        bits.append(f"{precip:.0f}% precip")
    if temp <= 20:
        severity += 0.04
        bits.append(f"{temp:.0f}°F")
    return {
        "roof": "outdoor",
        "wind": round(wind, 1),
        "gust": round(gust, 1),
        "precip_prob": round(precip, 0),
        "temp": round(temp, 0),
        "severity": round(min(severity, 0.25), 3),
        "summary": ", ".join(bits) if bits else "clear",
    }


def _build_id_map() -> dict:
    try:
        ids = _cached("ids", lambda: _import("import_ids", []))
    except Exception:
        return {}
    out: dict = {}
    for _, row in ids.dropna(subset=["espn_id"]).iterrows():
        try:
            out[int(row["espn_id"])] = norm_name(str(row["name"]))
        except (ValueError, TypeError):
            continue
    return out


def load_shared_data(season: int, week: int, stats_season: int | None = None) -> SharedData:
    picked_stats_season, notes = _pick_stats_season(season, stats_season)
    matchups, weather, wx_notes = _build_matchups_and_weather(season, week)
    sd = SharedData(
        season=season,
        stats_season=picked_stats_season,
        week=week,
        form=_safe(_build_form, picked_stats_season, week),
        dvp=_safe(_build_dvp, picked_stats_season),
        snaps=_safe(_build_snaps, picked_stats_season),
        injuries=_safe(_build_injuries, season) or _safe(_build_injuries, season - 1),
        weather=weather,
        matchups=matchups,
        espn_id_to_name=_safe(_build_id_map),
        notes=notes + wx_notes,
    )
    try:
        from . import slate

        sd.games = slate._games_for_week(season, week)
    except Exception as e:  # noqa: BLE001
        print(f"  ! game-time load failed: {e.__class__.__name__}: {e}")
    return sd


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        print(f"  ! {fn.__name__} failed: {e.__class__.__name__}: {e}")
        return {}
