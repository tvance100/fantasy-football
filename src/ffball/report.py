"""Render per-league start/sit reports and combine them into one email."""

from __future__ import annotations

import html
from datetime import datetime

from .espn import LeagueData
from .projections import PlayerProjection
from .shared_data import SharedData
from .slate import ET, SendWindow, lock_label
from .startsit import Lineup

_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;
 line-height:1.45;max-width:760px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:22px 0 6px;border-bottom:2px solid #eee;padding-bottom:3px}
h3{font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:#666;margin:16px 0 4px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:4px 0}
th,td{text-align:left;padding:4px 8px;border-bottom:1px solid #eee}
th{color:#666;font-weight:600}
.num{text-align:right;font-variant-numeric:tabular-nums}
.pos{display:inline-block;min-width:34px;font-weight:700;font-size:11px;color:#fff;
 padding:1px 5px;border-radius:4px;text-align:center}
.QB{background:#e05252}.RB{background:#38a169}.WR{background:#3182ce}.TE{background:#dd6b20}
.K{background:#805ad5}.DST{background:#4a5568}.FLEX{background:#718096}.SUPERFLEX{background:#2c5282}
.up{color:#1a7f37;font-weight:600}.down{color:#c1121f;font-weight:600}
.locknow{color:#b45309;font-weight:700}
.windowbar{background:#eef6ff;border-left:3px solid #3182ce;padding:8px 12px;font-size:13px;margin:8px 0}
.move{background:#fff8e1;padding:6px 10px;border-left:3px solid #f5b301;margin:4px 0;font-size:13px}
.watch{background:#fdecea;padding:6px 10px;border-left:3px solid #e05252;margin:4px 0;font-size:13px}
.muted{color:#888;font-size:12px}
ul{margin:4px 0;padding-left:20px}
.flag{color:#555;font-size:12px}
"""


def _e(x) -> str:
    return html.escape(str(x))


def _pos_badge(pos: str) -> str:
    cls = pos.replace("/", "").replace("D/ST", "DST")
    cls = {"DST": "DST"}.get(cls, cls if cls in ("QB", "RB", "WR", "TE", "K", "FLEX", "SUPERFLEX") else "FLEX")
    return f'<span class="pos {cls}">{_e(pos)}</span>'


def _delta(p: PlayerProjection) -> str:
    d = p.delta_vs_espn
    if abs(d) < 1.0:
        return f'<span class="muted">{d:+.1f}</span>'
    cls = "up" if d > 0 else "down"
    return f'<span class="{cls}">{d:+.1f}</span>'


def _lock_cell(p: PlayerProjection, in_window: bool, now=None) -> str:
    if p.on_bye or not p.kickoff:
        return '<span class="muted">—</span>'
    lbl = lock_label(p.kickoff, now)
    if lbl == "LOCKED":
        return '<span class="down">LOCKED</span>'
    cls = "locknow" if in_window else "muted"
    return f'<span class="{cls}">{_e(lbl)}</span>'


def _in_window(p: PlayerProjection, window: SendWindow | None) -> bool:
    return bool(window) and p.pro_team in window.teams


def render_league_section(
    ld: LeagueData,
    lineup: Lineup,
    projections: list[PlayerProjection],
    window: SendWindow | None = None,
    now=None,
) -> str:
    cfg = ld.config
    slots = ", ".join(f"{v}×{k}" for k, v in cfg.roster_slots.items())
    parts: list[str] = []
    parts.append(f"<h2>{_e(cfg.league_name)} — Week {ld.week}</h2>")
    parts.append(
        f'<p class="muted">Scoring: {_e(cfg.scoring_type.upper())} '
        f'({_reception_pts(cfg)} pt/rec) &nbsp;·&nbsp; Lineup: {_e(slots)} '
        f'&nbsp;·&nbsp; Projected starters total: <b>{lineup.projected_total}</b></p>'
    )

    if window:
        wp = sorted({p.name for p in projections if _in_window(p, window)})
        lock = window.deadline.strftime("%a %-I:%M%p ET").lower()
        parts.append(
            f'<p class="windowbar">⏰ <b>{_e(window.label)}</b> — '
            f'{len(wp)} of your players lock before this email\'s deadline '
            f'(<b>{_e(lock)}</b>). Decisions for the rest come in the next email.</p>'
        )

    # sort: window players first, then by kickoff, then projection
    def order(p: PlayerProjection):
        return (0 if _in_window(p, window) else 1,
                p.kickoff.timestamp() if p.kickoff else 9e18,
                -p.our_points)

    starters = sorted(lineup.starters, key=lambda sp: order(sp[1]))
    bench = sorted(lineup.bench, key=order)

    # ---- recommended lineup ----
    parts.append("<h3>Recommended lineup</h3><table>")
    parts.append('<tr><th>Slot</th><th>Player</th><th>Matchup</th><th>Locks</th>'
                 '<th class="num">Ours</th><th class="num">ESPN</th><th class="num">Δ</th></tr>')
    for slot, p in starters:
        chg = " ★" if p.current_slot in ("BE", "IR", "", None) else ""
        parts.append(
            f"<tr><td>{_pos_badge(slot)}</td>"
            f"<td>{_e(p.name)}{chg} <span class='muted'>{_e(p.pro_team)}</span></td>"
            f"<td>{_e(p.opponent)}</td>"
            f"<td>{_lock_cell(p, _in_window(p, window), now)}</td>"
            f'<td class="num">{p.our_points:.1f}</td>'
            f'<td class="num muted">{p.espn_points:.1f}</td>'
            f'<td class="num">{_delta(p)}</td></tr>'
        )
    parts.append("</table><p class='muted'>★ = not currently in your starting lineup</p>")

    # ---- moves ----
    moves = _filter_by_window(lineup.moves, projections, window)
    if moves:
        head = "Suggested moves" if not window else f"Suggested moves — {window.label}"
        parts.append(f"<h3>{_e(head)}</h3>")
        for m in moves:
            parts.append(f'<div class="move">{_e(m)}</div>')
    elif window:
        parts.append(f'<p class="muted">No lineup changes needed for {_e(window.label)}.</p>')
    else:
        parts.append('<p class="muted">Your current lineup already matches the projection — no changes.</p>')

    # ---- watch list ----
    watch = _filter_by_window(lineup.watch, projections, window)
    if watch:
        parts.append("<h3>Watch before kickoff</h3>")
        for w in watch:
            parts.append(f'<div class="watch">{_e(w)}</div>')

    # ---- bench ----
    parts.append("<h3>Bench</h3><table>")
    parts.append('<tr><th>Player</th><th>Pos</th><th>Matchup</th><th>Locks</th>'
                 '<th class="num">Ours</th><th class="num">ESPN</th><th>Notes</th></tr>')
    for p in bench:
        parts.append(
            f"<tr><td>{_e(p.name)} <span class='muted'>{_e(p.pro_team)}</span></td>"
            f"<td>{_pos_badge(p.position)}</td><td>{_e(p.opponent)}</td>"
            f"<td>{_lock_cell(p, _in_window(p, window), now)}</td>"
            f'<td class="num">{p.our_points:.1f}</td>'
            f'<td class="num muted">{p.espn_points:.1f}</td>'
            f'<td class="flag">{_e("; ".join(p.flags[:2]))}</td></tr>'
        )
    parts.append("</table>")

    # ---- droppable ----
    if lineup.droppable:
        parts.append("<h3>Roster-clog / droppable</h3><ul>")
        for d in lineup.droppable:
            parts.append(f"<li>{_e(d)}</li>")
        parts.append("</ul>")

    return "\n".join(parts)


def _reception_pts(cfg) -> str:
    for item in cfg.scoring_format:
        if int(item["id"]) in (41, 53):
            return str(item.get("points", 0))
    return "0"


def _filter_by_window(items: list[str], projections: list[PlayerProjection],
                      window: SendWindow | None) -> list[str]:
    if not window:
        return items
    names = [p.name for p in projections if p.pro_team in window.teams]
    return [it for it in items if any(n in it for n in names)]


def build_email_html(sections: list[str], sd: SharedData,
                     window: SendWindow | None = None) -> str:
    now = datetime.now(ET).strftime("%A %b %-d, %Y %-I:%M %p ET")
    notes = "".join(f"<li>{_e(n)}</li>" for n in sd.notes)
    body = "\n<hr>\n".join(sections)
    if window:
        title = f"Fantasy Start/Sit — Week {sd.week} — {_e(window.label)}"
        sub = (f"Send by {window.deadline:%A %-I:%M%p ET}. This email covers only the "
               f"players whose games start in this window; the rest come next.")
    else:
        title = f"Fantasy Start/Sit — Week {sd.week}"
        sub = "Full-week report (all games)."
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{_CSS}</style></head><body>
<h1>{title}</h1>
<p class="muted">{_e(sub)}</p>
<p class="muted">Generated {_e(now)} · game logs from {sd.stats_season} season</p>
{body}
<hr>
<h3>Data notes</h3><ul class="muted">{notes}</ul>
<p class="muted">Projections blend ESPN's stat projection with recent form, defense-vs-position,
weather, snap trend and injury status, then score it with each league's live ESPN rules.
Not investment advice; check inactives before lock.</p>
</body></html>"""


def build_plaintext(ld: LeagueData, lineup: Lineup,
                    window: SendWindow | None = None) -> str:
    lines = [f"{ld.config.league_name} — Week {ld.week} (proj {lineup.projected_total})", ""]
    lines.append("LINEUP:")
    for slot, p in lineup.starters:
        star = " *" if p.current_slot in ("BE", "IR", "", None) else ""
        lock = ""
        if p.kickoff and not p.on_bye:
            lock = f"  [{lock_label(p.kickoff)}]"
        lines.append(f"  {slot:9} {p.name:22} {p.opponent:5} {p.our_points:5.1f}{star}{lock}")
    moves = _filter_by_window(lineup.moves, [pp for _, pp in lineup.starters] + lineup.bench, window)
    watch = _filter_by_window(lineup.watch, [pp for _, pp in lineup.starters] + lineup.bench, window)
    if moves:
        lines += ["", "MOVES:"] + [f"  - {m}" for m in moves]
    if watch:
        lines += ["", "WATCH:"] + [f"  - {w}" for w in watch]
    return "\n".join(lines)
