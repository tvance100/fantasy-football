"""Cloud self-test: proves the GitHub Actions copy can reach everything it needs.

Checks the NFL schedule, each ESPN league roster, and the Gmail login, then writes
a short, secret-free summary to ops/selftest-result.txt (committed by the workflow).
Sends no email and changes no send state.
"""

from __future__ import annotations

import smtplib
import ssl
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

lines: list[str] = []
ok = True


def check(name, fn):
    global ok
    try:
        lines.append(f"OK    {name}: {fn()}")
    except Exception as e:  # noqa: BLE001
        ok = False
        lines.append(f"FAIL  {name}: {e.__class__.__name__}: {str(e)[:200]}")
        traceback.print_exc()


from ffball.config import load_settings  # noqa: E402
from ffball.slate import ET, build_send_windows, current_nfl_week  # noqa: E402

now = datetime.now(ET)
settings = None


def _settings():
    global settings
    settings = load_settings()
    return f"season {settings.season}, {len(settings.leagues)} league(s), email_enabled={settings.email_enabled}"


check("settings", _settings)
if settings:
    week = settings.week or current_nfl_week(settings.season, now)

    def _sched():
        ws = build_send_windows(settings.season, week, now)
        return f"week {week}: " + ", ".join(f"{w.key} by {w.deadline:%a %-I:%M%p}" for w in ws)

    check("NFL schedule", _sched)

    from ffball.espn import load_league

    for cfg in settings.leagues:
        check(f"ESPN {cfg.league_name}",
              lambda cfg=cfg: f"{len(load_league(cfg, settings.season, week, settings.espn_s2, settings.swid).roster)} players on roster")

    def _smtp():
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
            s.login(settings.gmail_address, settings.gmail_app_password)
        return "login accepted (no email sent)"

    check("Gmail", _smtp)

out = ROOT / "ops" / "selftest-result.txt"
out.write_text(f"Self-test {now:%Y-%m-%d %H:%M %Z}: {'PASS' if ok else 'FAIL'}\n" + "\n".join(lines) + "\n")
print(out.read_text())
sys.exit(0 if ok else 1)
