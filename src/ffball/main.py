"""Orchestrator.

    run.py                 build + send/write a full report right now
    run.py --scheduled     send this week's per-window email(s) only when due
    run.py --week 4        override the NFL week
    run.py --force         (with --scheduled) ignore the "already sent" state
"""

from __future__ import annotations

import argparse
import json
import os
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
from .slate import ET, WINDOW_CHOICES, build_send_windows, current_nfl_week, legacy_sent_keys
from .startsit import optimize

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "reports"
STATE_DIR = ROOT / ".state"
# How far before a window's deadline a scheduled run may fire it. 15 min suits a
# machine that polls every 10 min; GitHub Actions sets FIRE_LEAD_MINUTES=120
# because its scheduled runs can start late.
FIRE_LEAD = timedelta(minutes=int(os.getenv("FIRE_LEAD_MINUTES") or 15))


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


def _idle_tick_jobs(settings, season, now, week) -> None:
    """Weekly jobs that ride along on scheduler ticks with no email due. Each one is a
    cheap no-op unless it is due, and a failure in one can never affect the others
    (or the time-critical start/sit emails, which are handled before we get here)."""
    try:
        from .team_report import maybe_send_team_report

        maybe_send_team_report(settings, season, now, week)       # Tuesday 8am ET
    except Exception as e:  # noqa: BLE001
        print(f"  ! team report hook failed: {e.__class__.__name__}: {e}")
    try:
        from .evaluate import maybe_auto_evaluate

        maybe_auto_evaluate(settings, season, now, week)          # grade last week's picks
    except Exception as e:  # noqa: BLE001
        print(f"  ! evaluate hook failed: {e.__class__.__name__}: {e}")


def _log_predictions(season, week, window, now, collected) -> None:
    """Snapshot what we just recommended. Never allowed to break the send path."""
    if not collected:
        return
    try:
        from . import tracking

        conn = tracking.connect()
        n = tracking.log_run(conn, season=season, week=week, window=window,
                             now=now, results=collected)
        print(f"  Logged {n} predictions to {tracking.DB_PATH.name}")
    except Exception as e:  # noqa: BLE001
        print(f"  ! prediction logging failed: {e.__class__.__name__}: {e}")


def _build_sections(settings, sd, week, window, now=None, collect=None):
    """Return (html_sections, text_sections, any_roster).

    If `collect` is a list, each league's (cfg, league_data, projections, lineup)
    is appended so the caller can log it after the email goes out."""
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
            if collect is not None:
                collect.append((cfg, ld, projections, lineup))
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
        now_override: str | None = None, only_window: str | None = None,
        log: bool = False) -> int:
    settings = load_settings()
    season = settings.season
    now = (datetime.fromisoformat(now_override).replace(tzinfo=ET)
           if now_override else datetime.now(ET))
    week = week_override or settings.week or current_nfl_week(season, now)
    print(f"Season {season} · Week {week} · now {now:%a %Y-%m-%d %H:%M %Z} · "
          f"leagues: {', '.join(l.league_name for l in settings.leagues)}")

    windows = build_send_windows(season, week, now)
    if only_window == "pre_weekend":      # legacy name: first window before Sunday
        only_window = next((w.key for w in windows if w.key not in ("sunday", "monday")), None)
    if only_window:
        windows = [w for w in windows if w.key == only_window] or windows

    if scheduled:
        state = _load_state(season)
        sent = legacy_sent_keys(set(state.get(str(week), [])), windows)
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
            if not now_override:
                _idle_tick_jobs(settings, season, now, week)
            return 0

        print("Pulling shared data...")
        sd = load_shared_data(season, week, settings.stats_season)
        for n in sd.notes:
            print(f"  · {n}")
        for w in due:
            print(f"\n=== sending window '{w.key}' — {w.label} "
                  f"(deadline {w.deadline:%a %-I:%M%p ET}) ===")
            collected: list = []
            hs, ts, _ = _build_sections(settings, sd, week, w, now, collect=collected)
            _deliver(settings, sd, week, w, hs, ts)
            if not now_override:
                _log_predictions(season, week, w, now, collected)
        return 0

    # ---- manual: report now ----
    print("Pulling shared data...")
    sd = load_shared_data(season, week, settings.stats_season)
    for n in sd.notes:
        print(f"  · {n}")
    print(f"Send windows this week: "
          f"{', '.join(f'{w.key}@{w.deadline:%a %-I:%M%p}' for w in windows) or 'none'}")
    win = next((w for w in windows if w.key == only_window), None) if only_window else None
    collected = []
    hs, ts, _ = _build_sections(settings, sd, week, win, now, collect=collected if log else None)
    _deliver(settings, sd, week, win, hs, ts)
    if log and not now_override:
        _log_predictions(season, week, win, now, collected)
    return 0


def run_backfill() -> int:
    """Import past report HTML in ./reports/ into the prediction log."""
    from . import tracking
    from .backfill import backfill

    settings = load_settings()
    conn = tracking.connect()
    s = backfill(conn, REPORT_DIR, settings.season, settings.leagues)
    print(f"Backfill: {s['files']} report(s) imported, {s['rows']} prediction rows, "
          f"{s['skipped_locked']} already-locked rows skipped, "
          f"{s['skipped_files']} file(s) already present/unmatched.")
    if s["unknown_league"]:
        print(f"  ! league name(s) not in .env, skipped: {', '.join(s['unknown_league'])}")
    return 0


def run_team_report_cmd(week: int | None, email: bool, demo: bool) -> int:
    """Build (and optionally email) the weekly team report for a finished week."""
    from .team_report import run_team_report

    if demo:
        run_team_report(None, 2026, week or 4, email=False, demo=True)
        return 0
    settings = load_settings()
    now = datetime.now(ET)
    target = week or current_nfl_week(settings.season, now) - 1
    if target < 1:
        print("No finished week to review yet.")
        return 0
    run_team_report(settings, settings.season, target, email=email, now=now)
    return 0


def run_evaluate_cmd(week: int | None, fetch: bool, email: bool) -> int:
    """Grade logged recommendations against ESPN's final box scores."""
    from . import tracking
    from .evaluate import run_evaluate

    settings = load_settings()
    now = datetime.now(ET)
    current_week = current_nfl_week(settings.season, now)
    conn = tracking.connect()
    run_evaluate(settings, conn, settings.season, week, current_week, fetch=fetch, email=email)
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
    ap.add_argument("--window", default=None, choices=WINDOW_CHOICES,
                    help="manual mode: preview just this game day's email (e.g. thursday, sunday, monday)")
    ap.add_argument("--log", action="store_true",
                    help="manual mode: also record this run's predictions in the tracking log")
    ap.add_argument("--backfill", action="store_true",
                    help="import past ./reports/ emails into the tracking log, then exit")
    ap.add_argument("--evaluate", action="store_true",
                    help="grade logged recommendations against actual results, then exit "
                         "(--week N for one week; default: all finished weeks not yet graded)")
    ap.add_argument("--no-fetch", action="store_true",
                    help="with --evaluate: use stored actuals only, no ESPN calls")
    ap.add_argument("--email", action="store_true",
                    help="with --evaluate: also email the scorecard")
    ap.add_argument("--team-report", action="store_true",
                    help="build the weekly team report (strengths/weaknesses, waiver + trade ideas) "
                         "for the last finished week (or --week N), then exit; add --email to send")
    ap.add_argument("--demo", action="store_true",
                    help="with --team-report: preview the layout on made-up data (no ESPN needed)")
    args = ap.parse_args()
    try:
        if args.team_report:
            sys.exit(run_team_report_cmd(args.week, args.email, args.demo))
        if args.backfill:
            sys.exit(run_backfill())
        if args.evaluate:
            sys.exit(run_evaluate_cmd(args.week, not args.no_fetch, args.email))
        sys.exit(run(args.scheduled, args.week, args.force, args.now, args.window, args.log))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
