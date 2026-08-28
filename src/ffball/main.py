"""Orchestrator.

    run.py                 build + send/write a full report right now
    run.py --scheduled     send this week's per-window email(s) only when due
    run.py --week 4        override the NFL week
    run.py --force         (with --scheduled) ignore the "already sent" state
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime, timedelta
from pathlib import Path

from .config import load_settings
from .email_send import send_email
from .espn import load_league
from .projections import project_roster
from .report import build_email_html, build_plaintext, render_league_section
from .shared_data import load_shared_data
from .slate import ET, build_send_windows, current_nfl_week
from .startsit import optimize

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "reports"
STATE_DIR = ROOT / ".state"
# how far before a window's deadline a scheduled run is allowed to fire it
FIRE_LEAD = timedelta(minutes=15)


def _load_state(season: int) -> dict:
    p = STATE_DIR / f"sent_{season}.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_state(season: int, state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    (STATE_DIR / f"sent_{season}.json").write_text(json.dumps(state, indent=2))


def _build_sections(settings, sd, week, window, now=None):
    """Return (html_sections, text_sections, any_roster)."""
    html_sections: list[str] = []
    text_sections: list[str] = []
    any_roster = False
    for cfg in settings.leagues:
        try:
            ld = load_league(cfg, settings.season, week, settings.espn_s2, settings.swid)
            print(f"[{cfg.league_name}] {cfg.scoring_type.upper()} · "
                  f"slots {cfg.roster_slots} · {len(ld.roster)} players")
            if not ld.roster:
                hint = ("draft hasn't happened yet"
                        if week <= 1 and settings.season >= 2026
                        else "wrong season/week or team_id")
                print(f"  ! roster EMPTY — {hint}. Test now with SEASON=2025 in .env.")
                html_sections.append(
                    f"<h2>{cfg.league_name} — Week {week}</h2>"
                    f"<p style='color:#c1121f'>Roster came back empty — {hint}.</p>"
                )
                text_sections.append(f"{cfg.league_name}: roster empty ({hint})")
                continue
            any_roster = True
            projections = project_roster(ld, sd)
            lineup = optimize(projections, ld.starting_slot_counts, week)
            print(f"  projected starters {lineup.projected_total} · {len(lineup.moves)} move(s)")
            for m in lineup.moves:
                print(f"    - {m}")
            html_sections.append(render_league_section(ld, lineup, projections, window, now))
            text_sections.append(build_plaintext(ld, lineup, window))
        except Exception as e:  # noqa: BLE001
            print(f"  ! {cfg.league_name} failed: {e}")
            traceback.print_exc()
            html_sections.append(
                f"<h2>{cfg.league_name}</h2><p style='color:#c1121f'>Failed: {e}</p>"
            )
            text_sections.append(f"{cfg.league_name}: FAILED — {e}")
    return html_sections, text_sections, any_roster


def _deliver(settings, sd, week, window, html_sections, text_sections) -> Path:
    REPORT_DIR.mkdir(exist_ok=True)
    html_doc = build_email_html(html_sections, sd, window)
    tag = window.key if window else "full"
    stamp = datetime.now().strftime("%Y%m%d")
    out = REPORT_DIR / f"startsit_wk{week}_{tag}_{stamp}.html"
    out.write_text(html_doc)
    print(f"Report written: {out}")

    league_names = ", ".join(l.league_name for l in settings.leagues)
    if window:
        lock = window.deadline.strftime("%-I:%M%p ET").lower()
        subject = f"Fantasy Start/Sit — Week {week} — {window.label} (lock {lock})"
    else:
        subject = f"Fantasy Start/Sit — Week {week} ({league_names})"
    send_email(settings, subject, html_doc, "\n\n".join(text_sections))
    return out


def run(scheduled: bool, week_override: int | None, force: bool,
        now_override: str | None = None, only_window: str | None = None) -> int:
    settings = load_settings()
    season = settings.season
    now = (datetime.fromisoformat(now_override).replace(tzinfo=ET)
           if now_override else datetime.now(ET))
    week = week_override or settings.week or current_nfl_week(season, now)
    print(f"Season {season} · Week {week} · now {now:%a %Y-%m-%d %H:%M %Z} · "
          f"leagues: {', '.join(l.league_name for l in settings.leagues)}")

    windows = build_send_windows(season, week, now)
    if only_window:
        windows = [w for w in windows if w.key == only_window] or windows

    if scheduled:
        state = _load_state(season)
        sent = set(state.get(str(week), []))
        due = []
        for w in windows:
            if w.key in sent and not force:
                continue
            if now < w.deadline - FIRE_LEAD:
                continue
            live = [g for g in w.games if g.kickoff > now]
            if not live and not force:
                print(f"  window '{w.key}': deadline passed and all games kicked off — skipping")
                sent.add(w.key)
                continue
            due.append(w)
        state[str(week)] = sorted(sent | {w.key for w in due})
        _save_state(season, state)

        if not due:
            nxt = min((w.deadline for w in windows if w.key not in sent), default=None)
            print(f"Nothing due. Next window deadline: {nxt}")
            return 0

        print("Pulling shared data...")
        sd = load_shared_data(season, week, settings.stats_season)
        for n in sd.notes:
            print(f"  · {n}")
        for w in due:
            print(f"\n=== sending window '{w.key}' — {w.label} "
                  f"(deadline {w.deadline:%a %-I:%M%p ET}) ===")
            hs, ts, _ = _build_sections(settings, sd, week, w, now)
            _deliver(settings, sd, week, w, hs, ts)
        return 0

    # ---- manual: report now ----
    print("Pulling shared data...")
    sd = load_shared_data(season, week, settings.stats_season)
    for n in sd.notes:
        print(f"  · {n}")
    print(f"Send windows this week: "
          f"{', '.join(f'{w.key}@{w.deadline:%a %-I:%M%p}' for w in windows) or 'none'}")
    win = next((w for w in windows if w.key == only_window), None) if only_window else None
    hs, ts, _ = _build_sections(settings, sd, week, win, now)
    _deliver(settings, sd, week, win, hs, ts)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scheduled", action="store_true",
                    help="only send a window's email when its deadline is near")
    ap.add_argument("--week", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="with --scheduled: resend even if already sent")
    ap.add_argument("--now", default=None,
                    help="pretend it's this ET time, e.g. 2025-09-18T16:30 (testing)")
    ap.add_argument("--window", default=None, choices=["pre_weekend", "sunday"],
                    help="manual mode: preview just this window's email")
    args = ap.parse_args()
    try:
        sys.exit(run(args.scheduled, args.week, args.force, args.now, args.window))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
