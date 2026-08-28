"""Environment / league configuration loading."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()


@dataclass
class LeagueConfig:
    league_id: str
    team_id: int
    league_name: str
    # Populated at runtime from the ESPN API (never hardcoded):
    scoring_type: str = ""            # "ppr" | "half_ppr" | "standard" (inferred)
    scoring_format: list = field(default_factory=list)  # live [{id,abbr,label,points}]
    roster_slots: dict = field(default_factory=dict)    # {"QB":1,"RB":2,...,"BE":6}
    is_superflex: bool = False
    idp_enabled: bool = False


@dataclass
class Settings:
    espn_s2: str | None
    swid: str | None
    season: int
    stats_season: int | None
    week: int | None
    gmail_address: str
    gmail_app_password: str
    email_to: str
    email_enabled: bool
    leagues: list[LeagueConfig]


def _clean_swid(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    if raw and not raw.startswith("{"):
        raw = "{" + raw + "}"
    return raw or None


def _load_leagues() -> list[LeagueConfig]:
    leagues: list[LeagueConfig] = []
    i = 1
    while True:
        lid = os.getenv(f"LEAGUE_{i}_ID")
        if not lid:
            break
        leagues.append(
            LeagueConfig(
                league_id=lid.strip(),
                team_id=int(os.environ[f"LEAGUE_{i}_TEAM_ID"]),
                league_name=os.getenv(f"LEAGUE_{i}_NAME", f"League {i}").strip(),
            )
        )
        i += 1
    return leagues


def load_settings() -> Settings:
    season = int(os.getenv("SEASON") or _default_season())
    stats_season_raw = os.getenv("STATS_SEASON", "").strip()
    week_raw = os.getenv("WEEK", "").strip()
    leagues = _load_leagues()
    if not leagues:
        raise SystemExit(
            "No leagues configured. Copy .env.example to .env and fill in "
            "LEAGUE_1_ID / LEAGUE_1_TEAM_ID (and any additional leagues)."
        )
    return Settings(
        espn_s2=(os.getenv("ESPN_S2") or "").strip() or None,
        swid=_clean_swid(os.getenv("ESPN_SWID")),
        season=season,
        stats_season=int(stats_season_raw) if stats_season_raw else None,
        week=int(week_raw) if week_raw else None,
        gmail_address=(os.getenv("GMAIL_ADDRESS") or "").strip(),
        gmail_app_password=(os.getenv("GMAIL_APP_PASSWORD") or "").strip(),
        email_to=(os.getenv("EMAIL_TO") or os.getenv("GMAIL_ADDRESS") or "").strip(),
        email_enabled=os.getenv("EMAIL_ENABLED", "0").strip() in ("1", "true", "True"),
        leagues=leagues,
    )


def _default_season() -> int:
    now = datetime.now()
    # NFL league year rolls over in March; before then we're still in the
    # prior season's playoffs / offseason.
    return now.year if now.month >= 3 else now.year - 1
