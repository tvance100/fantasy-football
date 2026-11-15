"""Reconstruct prediction-log rows from the HTML emails already in ./reports/.

Weeks sent before tracking existed are not lost: each email's tables hold every
player's ours / ESPN projection, matchup and lock status. This parses them into
the same tables `tracking.py` writes, marked provenance='backfill'.

Limits (all flagged in the scorecard):
  * no per-factor detail or stat lines (not in the HTML)
  * lineup-slot rows show only the slot, so a flex starter's position is filled in
    later from ESPN's box score
  * players already LOCKED when an email was generated are skipped -- their
    projections were computed after kickoff and would leak hindsight
  * `in_window` is read from the highlighted "locks ..." cell
"""

from __future__ import annotations

import html as _html
import json
import re
from datetime import datetime
from pathlib import Path

from .evaluate import eligible_from_position
from .shared_data import norm_name
from .slate import ET

FILE_RE = re.compile(r"^startsit_wk(\d+)_(pre_weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)_(\d{8})\.html$")
PLAIN_POSITIONS = {"QB", "RB", "WR", "TE", "K", "D/ST"}

_H2 = re.compile(r"<h2>(.*?)\s+—\s+Week\s+(\d+)</h2>", re.S)
_TR = re.compile(r"<tr>(.*?)</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAGS = re.compile(r"<[^>]+>")
_PLAYER = re.compile(r"^(.*?)\s*(?:★)?\s*<span class=['\"]muted['\"]>(.*?)</span>", re.S)
_SLOTCOUNT = re.compile(r"(\d+)×([A-Za-z/]+)")
_GENERATED = re.compile(r"Generated\s+\w+\s+(\w+)\s+(\d+),\s+(\d{4})\s+(\d+):(\d+)\s+(AM|PM)\s+ET")
_TOTAL = re.compile(r"Projected starters total:\s*<b>([\d.]+)</b>")


def _text(cell: str) -> str:
    return _html.unescape(_TAGS.sub("", cell)).strip()


def _num(s: str) -> float | None:
    try:
        return float(s)
    except ValueError:
        return None


def _generated_at(doc: str) -> datetime | None:
    m = _GENERATED.search(doc)
    if not m:
        return None
    mon, day, year, hh, mm, ap = m.groups()
    try:
        dt = datetime.strptime(f"{mon} {day} {year} {hh}:{mm} {ap}", "%b %d %Y %I:%M %p")
    except ValueError:
        return None
    return dt.replace(tzinfo=ET)


def parse_report(doc: str) -> list[dict]:
    """Return one dict per league section: name, week, slot_counts, projected_total,
    generated_at, players=[{name, team, position, opponent, locked, in_window,
    ours, espn, rec_slot, lock_text}]."""
    generated = _generated_at(doc)
    heads = list(_H2.finditer(doc))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(doc)
        sec = doc[h.end():end]
        slot_counts = {lab: int(n) for n, lab in _SLOTCOUNT.findall(_text(sec.split("<h3>")[0]))}
        total = _TOTAL.search(sec)

        players: list[dict] = []
        for part in sec.split("<h3>")[1:]:
            title = part.split("</h3>")[0].strip()
            if title == "Recommended lineup":
                kind = "lineup"
            elif title == "Bench":
                kind = "bench"
            else:
                continue
            for tr in _TR.findall(part):
                cells = _TD.findall(tr)
                if len(cells) < 6:
                    continue
                if kind == "lineup":
                    slot = _text(cells[0])
                    pm = _PLAYER.match(cells[1])
                    opp, lock_html, ours, espn = cells[2], cells[3], cells[4], cells[5]
                    position = slot if slot in PLAIN_POSITIONS else ""
                    rec_slot = slot
                else:
                    pm = _PLAYER.match(cells[0])
                    position = _text(cells[1])
                    opp, lock_html, ours, espn = cells[2], cells[3], cells[4], cells[5]
                    rec_slot = "BE"
                if not pm:
                    continue
                o, e = _num(_text(ours)), _num(_text(espn))
                if o is None or e is None:
                    continue
                lock_text = _text(lock_html)
                players.append({
                    "name": _html.unescape(_TAGS.sub("", pm.group(1))).strip(),
                    "team": _text(pm.group(2)),
                    "position": position,
                    "opponent": _text(opp),
                    "locked": lock_text == "LOCKED",
                    "in_window": "locknow" in lock_html,
                    "ours": o, "espn": e,
                    "rec_slot": rec_slot,
                    "lock_text": lock_text,
                })
        out.append({
            "league_name": _html.unescape(_TAGS.sub("", h.group(1))).strip(),
            "week": int(h.group(2)),
            "slot_counts": slot_counts,
            "projected_total": float(total.group(1)) if total else None,
            "generated_at": generated,
            "players": players,
        })
    return out


def backfill(conn, reports_dir: Path, season: int, leagues, overwrite: bool = False) -> dict:
    """Parse every scheduled-window report in `reports_dir` into the log.

    `leagues` = [LeagueConfig, ...] used to map league names to ids. Existing live
    rows are never overwritten; existing backfill rows are replaced only when
    `overwrite` is true (otherwise the file is skipped as already imported)."""
    ids = {cfg.league_name: str(cfg.league_id) for cfg in leagues}
    summary = {"files": 0, "skipped_files": 0, "rows": 0, "skipped_locked": 0, "unknown_league": []}
    for path in sorted(Path(reports_dir).glob("startsit_wk*_*.html")):
        m = FILE_RE.match(path.name)
        if not m:
            continue
        window = m.group(2)
        doc = path.read_text()
        sections = parse_report(doc)
        if not sections:
            continue
        imported = False
        for sec in sections:
            lid = ids.get(sec["league_name"])
            if lid is None:
                if sec["league_name"] not in summary["unknown_league"]:
                    summary["unknown_league"].append(sec["league_name"])
                continue
            week = sec["week"]
            sent = sec["generated_at"].isoformat() if sec["generated_at"] else None
            live = conn.execute(
                "SELECT 1 FROM lineups WHERE season=? AND week=? AND window=? AND league_id=? "
                "AND provenance='live'", (season, week, window, lid)).fetchone()
            if live:
                continue
            if not overwrite and conn.execute(
                    "SELECT 1 FROM lineups WHERE season=? AND week=? AND window=? AND league_id=?",
                    (season, week, window, lid)).fetchone():
                continue

            conn.execute(
                "INSERT OR REPLACE INTO lineups VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (season, week, window, lid, sec["league_name"], None,
                 json.dumps(sec["slot_counts"]), sec["projected_total"], sent,
                 "backfill-html", "backfill"),
            )
            for p in sec["players"]:
                if p["locked"]:
                    summary["skipped_locked"] += 1
                    continue
                on_bye = 1 if p["opponent"].upper() == "BYE" else 0
                elig = eligible_from_position(p["position"], sec["slot_counts"]) if p["position"] else []
                conn.execute(
                    "INSERT OR REPLACE INTO predictions VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        season, week, window, lid, norm_name(p["name"]), 0, p["name"],
                        p["position"], p["team"], p["opponent"], on_bye,
                        p["espn"], p["ours"], None, None, None, None,
                        json.dumps(elig) if elig else None,
                        None, p["rec_slot"], 1 if p["in_window"] else 0, None, sent,
                        "backfill-html", "backfill",
                    ),
                )
                summary["rows"] += 1
            imported = True
        summary["files" if imported else "skipped_files"] += 1
    conn.commit()
    return summary
