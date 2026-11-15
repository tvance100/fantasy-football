"""Weekly team report: how each of your teams did, strengths/weaknesses, and what to do.

    run.py --team-report                 build the report for the last finished week (writes HTML)
    run.py --team-report --email         ...and email it
    run.py --team-report --week 4        a specific finished week
    run.py --team-report --demo          preview the layout on made-up data (no ESPN needed)

On the scheduler it goes out automatically each Tuesday at 8am ET (see
maybe_send_team_report), once per finished week.
"""

from __future__ import annotations

import html as _html
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

from .report import _CSS, _e
from .team_analysis import (
    LeagueSnapshot, PlayerLine, TeamSnapshot, analyze, best_lineup, ensure_eligibility, ordinal,
)

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / ".state"
REPORT_DIR = ROOT / "reports"

_EXTRA_CSS = """
.badge{display:inline-block;padding:1px 8px;border-radius:9px;font-size:11px;font-weight:700;color:#fff}
.b-strength{background:#1a7f37}.b-weakness{background:#c1121f}.b-neutral{background:#8a8f98}
.card{border:1px solid #e3e6ea;border-radius:6px;padding:8px 12px;margin:8px 0;background:#fafbfc}
.assess p{margin:6px 0}
.small{font-size:12px;color:#555}
"""


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _p(p: PlayerLine) -> str:
    return f"{p.name} ({p.position}{', ' + p.pro_team if p.pro_team else ''})"


def _partner(snap, name: str) -> str:
    for t in snap.teams:
        if t.name == name and t.owner:
            return f"{name} ({t.owner})"
    return name


def _league_html(a: dict) -> str:
    if a.get("error"):
        return (f"<h2>{_e(a['league_name'])}</h2>"
                f"<p style='color:#c1121f'>Could not build this league's report: {_e(a['error'])}</p>")
    snap: LeagueSnapshot = a["snap"]
    st, rc = a["standing"], a["recap"]
    h: list[str] = []
    head = f"{_e(snap.league_name)} — Week {snap.week} review"
    if st:
        head += f" ({_e(st['record'])}, {ordinal(st['place'])} of {st['of']})"
    h.append(f"<h2>{head}</h2>")
    me = snap.me
    who = f"<b>{_e(me.name)}</b>" + (f" (owner: {_e(me.owner)})" if me.owner else "")
    h.append(f"<p class='small'>Your team: {who} · ESPN league {_e(snap.league_id)}, team #{me.team_id} · "
             f"data pulled live from ESPN</p>")

    h.append('<h3>Overall assessment</h3><div class="assess">'
             + "".join(f"<p>{_e(line)}</p>" for line in a["assessment"]) + "</div>")

    # ---- last week ----
    if rc:
        h.append(f"<h3>Last week</h3>")
        if rc["result"]:
            h.append(f"<p><b>{_e({'W': 'Win', 'L': 'Loss', 'T': 'Tie'}[rc['result']])}</b> "
                     f"{rc['score']:.1f} – {rc['opp_score']:.1f}"
                     + (f" vs {_e(rc['opp_name'])}" if rc["opp_name"] else "") + "</p>")
        if rc["efficiency"] is not None:
            h.append(f"<p class='small'>Started {rc['started_pts']:.1f} of a possible "
                     f"{rc['optimal_pts']:.1f} points ({rc['efficiency']:.0%} lineup efficiency).</p>")
        if rc["mistakes"]:
            h.append("<ul>" + "".join(
                f"<li>Benched <b>{_e(m['benched'])}</b> ({m['bench_pts']:.1f}) while starting "
                f"{_e(m['started'])} ({m['started_pts']:.1f}): cost {m['cost']:.1f} pts.</li>"
                for m in rc["mistakes"]) + "</ul>")
        if rc["top"]:
            h.append("<p class='small'><b>Top scorers:</b> " + _e(", ".join(
                f"{n} {pts:.1f}" for n, _pos, pts in rc["top"])) + "</p>")
        if rc["busts"]:
            h.append("<p class='small'><b>Biggest misses vs ESPN's projection:</b> " + _e(", ".join(
                f"{n} {pts:.1f} (proj {proj:.1f})" for n, _pos, pts, proj in rc["busts"])) + "</p>")

    # ---- strengths / weaknesses ----
    if a["positions"]:
        h.append("<h3>Strengths &amp; weaknesses</h3><table>")
        h.append("<tr><th>Position</th><th>Your best</th><th class='num'>vs league avg</th>"
                 "<th class='num'>Rank</th><th></th></tr>")
        for r in a["positions"]:
            label = "Whole lineup" if r["pos"] == "LINEUP" else r["pos"]
            badge = (f"<span class='badge b-{r['verdict']}'>{r['verdict']}</span>"
                     if r["pos"] != "LINEUP" else "")
            h.append(f"<tr><td>{_e(label)}</td><td class='small'>{_e(', '.join(r['players']))}</td>"
                     f"<td class='num'>{r['diff']:+.1f}</td><td class='num'>{r['rank']}/{r['of']}</td>"
                     f"<td>{badge}</td></tr>")
        h.append("</table><p class='small'>Points per week from each position's starters, "
                 "compared with the average team in your league.</p>")

    # ---- looking ahead ----
    ol = a["outlook"]
    if ol["injuries"] or ol["byes"]:
        h.append("<h3>Looking ahead</h3><ul>")
        for n, pos, status in ol["injuries"]:
            h.append(f"<li>{_e(n)} ({_e(pos)}): {_e(status)}</li>")
        for b in ol["byes"]:
            h.append(f"<li>Week {b['week']} bye: {_e(', '.join(b['players']))}</li>")
        h.append("</ul>")

    # ---- free agents ----
    h.append("<h3>Waiver / free-agent targets</h3>")
    if a["free_agents"]:
        h.append("<table><tr><th>Add</th><th>Drop</th><th>Why</th>"
                 "<th class='num'>Season avg</th><th class='num'>ESPN next wk</th>"
                 "<th class='num'>Owned</th></tr>")
        for r in a["free_agents"]:
            fa = r["add"]
            if r["kind"] == "upgrade":
                why = (f"Starts at {r['slot']}" + (f" over {', '.join(r['bumps'])}" if r["bumps"] else "")
                       + f" (+{r['gain']:.1f} pts/wk)")
            else:
                why = f"Covers a bye/injury next week (+{r['gain_next']:.1f} pts)"
            proj = f"{fa.week_proj:.1f}" if fa.week_proj else "-"
            own = f"{fa.percent_owned:.0f}%" if fa.percent_owned >= 0 else "-"
            h.append(f"<tr><td><b>{_e(_p(fa))}</b></td><td>{_e(_p(r['drop']))}</td><td>{_e(why)}</td>"
                     f"<td class='num'>{fa.season_avg:.1f}</td><td class='num'>{proj}</td>"
                     f"<td class='num'>{own}</td></tr>")
        h.append("</table><p class='small'>Waivers usually process Wednesday morning; check your "
                 "league's waiver priority/FAAB before bidding.</p>")
    else:
        h.append("<p class='muted'>No free agent clearly improves your lineup right now.</p>")

    # ---- trades ----
    h.append("<h3>Trade ideas</h3>")
    if a["trades"]:
        for t in a["trades"]:
            give = " + ".join(_p(p) for p in t["give"])
            get = " + ".join(_p(p) for p in t["get"])
            h.append(f"<div class='card'><b>Give:</b> {_e(give)}<br><b>Get:</b> {_e(get)} "
                     f"<span class='small'>from {_e(_partner(snap, t['partner']))}</span><ul>"
                     + "".join(f"<li>{_e(w)}</li>" for w in t["why"]) + "</ul></div>")
        h.append("<p class='small'>Ideas only: each one improves both teams' best lineup on paper "
                 "(by expected points per game), so the other manager has a reason to accept. "
                 "Check injuries, bye weeks and league trade rules first.</p>")
    else:
        h.append("<p class='muted'>No trade found that clearly helps both sides.</p>")

    if a["notes"]:
        h.append("<h3>Data notes</h3><ul class='muted'>"
                 + "".join(f"<li>{_e(n)}</li>" for n in a["notes"]) + "</ul>")
    return "\n".join(h)


def _league_text(a: dict) -> str:
    if a.get("error"):
        return f"{a['league_name']}: could not build report: {a['error']}"
    snap = a["snap"]
    st = a["standing"]
    L = [f"{snap.league_name} - Week {snap.week} review" + (f" ({st['record']})" if st else ""),
         f"Your team: {snap.me.name}" + (f" (owner: {snap.me.owner})" if snap.me.owner else "")
         + f" - ESPN league {snap.league_id}, team #{snap.me.team_id}", ""]
    L += ["OVERALL ASSESSMENT"] + [f"  {x}" for x in a["assessment"]]
    if a["free_agents"]:
        L += ["", "WAIVER TARGETS"]
        for r in a["free_agents"]:
            L.append(f"  Add {_p(r['add'])}, drop {r['drop'].name} "
                     f"(+{max(r['gain'], r['gain_next']):.1f} pts/wk)")
    if a["trades"]:
        L += ["", "TRADE IDEAS"]
        for t in a["trades"]:
            L.append(f"  Give {' + '.join(p.name for p in t['give'])} for "
                     f"{' + '.join(p.name for p in t['get'])} ({_partner(snap, t['partner'])}); "
                     f"you +{t['gain']:.1f}, them +{t['their_gain']:.1f}")
    return "\n".join(L)


def render_email(analyses: list[dict], week: int, now: datetime | None = None,
                 late: bool = False, demo: bool = False) -> tuple[str, str, str]:
    now = now or datetime.now()
    recs = []
    for a in analyses:
        if a.get("error"):
            continue
        st = a["standing"]
        recs.append(f"{a['snap'].league_name} {st['record']}" if st else a["snap"].league_name)
    subject = f"Weekly Team Report — Week {week}" + (f": {', '.join(recs)}" if recs else "")
    body = "\n<hr>\n".join(_league_html(a) for a in analyses)
    late_note = ("<p class='windowbar'>Sent later than usual (your computer was likely asleep at 8am). "
                 "If your leagues process waivers on Wednesday morning, act before then.</p>") if late else ""
    demo_bar = ("<p style='background:#c1121f;color:#fff;padding:8px 12px;font-weight:700'>SAMPLE DATA — "
                "every team, player and record below is made up to preview the layout. "
                "These are NOT your leagues.</p>") if demo else ""
    if demo:
        subject = "[SAMPLE DATA] " + subject
    doc = f"""<!doctype html><html><head><meta charset="utf-8"><style>{_CSS}{_EXTRA_CSS}</style></head><body>
{demo_bar}<h1>Weekly Team Report — Week {week}</h1>
<p class="muted">Generated {_e(now.strftime('%A %b %-d, %Y %-I:%M %p'))}</p>
{late_note}
{body}
<hr>
<p class="muted">Player value = a blend of season production and ESPN's projected average (weighted
toward production as the season goes on), adjusted for injury status, in each league's own scoring.
Strengths, waiver targets and trades are all measured as "how many points per week does your best
lineup gain". It is a rule-based read of the numbers, not a prediction.</p>
</body></html>"""
    text = ("SAMPLE DATA - NOT YOUR LEAGUES\n" if demo else "") + f"Weekly Team Report - Week {week}\n\n" + "\n\n".join(_league_text(a) for a in analyses)
    return subject, doc, text


# --------------------------------------------------------------------------- #
# demo data (layout preview with no ESPN access; also used by tests)
# --------------------------------------------------------------------------- #
_FIRST = ["Alex", "Blake", "Casey", "Drew", "Elliot", "Finley", "Gray", "Harper", "Indigo", "Jules",
          "Kai", "Logan", "Morgan", "Nico", "Oakley", "Parker", "Quinn", "Reese", "Sage", "Tatum"]
_LAST = ["Archer", "Bishop", "Calloway", "Dunmore", "Ellery", "Fairbank", "Garrity", "Hollis",
         "Ingram", "Jessup", "Kessler", "Larkin", "Merritt", "Norwood", "Overton", "Pruitt"]


def demo_snapshot(name: str = "Demo League", seed: int = 7, week: int = 4) -> LeagueSnapshot:
    rng = random.Random(seed)
    slot_counts = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "D/ST": 1, "K": 1}
    used: set[str] = set()

    def nm() -> str:
        while True:
            n = f"{rng.choice(_FIRST)} {rng.choice(_LAST)}"
            if n not in used:
                used.add(n)
                return n

    def line(pos, lo, hi, team="KC", inj="ACTIVE", pid=None):
        avg = rng.uniform(lo, hi)
        return PlayerLine(name=nm(), position=pos, pro_team=team, injury=inj,
                          season_avg=round(avg, 1), proj_avg=round(avg + rng.uniform(-2, 2), 1),
                          games=week, player_id=pid or rng.randrange(10**6),
                          percent_owned=round(rng.uniform(50, 100), 0))

    spec = {"QB": (2, 12, 24), "RB": (5, 4, 16), "WR": (5, 4, 16), "TE": (2, 3, 12),
            "K": (1, 6, 9), "D/ST": (1, 5, 10)}
    teams = []
    for i in range(12):
        roster = []
        for pos, (n, lo, hi) in spec.items():
            for _ in range(n):
                if i == 0 and pos == "RB":
                    roster.append(line(pos, 13, 18))          # my team: strong RBs
                elif i == 0 and pos == "TE":
                    roster.append(line(pos, 2, 5))            # ...weak TEs
                elif i == 0 and pos == "WR":
                    roster.append(line(pos, 9, 15))           # ...deep WRs
                elif i in (3, 5) and pos == "TE":
                    roster.append(line(pos, 10, 14))          # strong TE teams that need WR/RB help
                elif i in (3, 5) and pos == "WR":
                    roster.append(line(pos, 3, 6))
                else:
                    roster.append(line(pos, lo, hi))
        teams.append(TeamSnapshot(team_id=i + 1, name="More Cowbell (demo)" if i == 0 else f"Team {_LAST[i]}",
                                  roster=roster))
    for t in teams:
        ensure_eligibility(t.roster, slot_counts)
        strength = best_lineup(t.roster, slot_counts)[0]
        t.scores = [round(strength + rng.uniform(-14, 14), 1) for _ in range(week)]
    for w in range(week):
        for a in range(0, 12, 2):
            x, y = teams[a], teams[a + 1]
            x.points_for += x.scores[w]; x.points_against += y.scores[w]
            y.points_for += y.scores[w]; y.points_against += x.scores[w]
            win, lose = (x, y) if x.scores[w] > y.scores[w] else (y, x)
            win.wins += 1; lose.losses += 1
            win.outcomes.append("W"); lose.outcomes.append("L")
    for rank, t in enumerate(sorted(teams, key=lambda t: (-t.wins, -t.points_for)), 1):
        t.standing = rank

    me = teams[0]
    _, picks = best_lineup(me.roster, slot_counts)
    started = {id(p) for _, p in picks}
    box = []
    for p in me.roster:
        copy = PlayerLine(**{k: getattr(p, k) for k in
                             ("name", "position", "pro_team", "eligible_slots", "injury", "season_avg",
                              "proj_avg", "games", "percent_owned", "player_id")})
        copy.slot = "BE" if id(p) not in started else next(s for s, q in picks if q is p)
        copy.week_proj = round(p.value, 1)
        copy.week_points = round(max(0.0, p.value + rng.uniform(-7, 9)), 1)
        box.append(copy)
    fas = [line("TE", 8, 10, team="DEN", pid=900001), line("RB", 8, 11, team="MIA", pid=900002),
           line("WR", 9, 12, team="SEA", pid=900003), line("K", 8, 9, team="BAL", pid=900004),
           line("D/ST", 8, 11, team="PIT", pid=900005), line("QB", 14, 17, team="IND", pid=900006)]
    for f in fas:
        f.slot, f.week_proj, f.percent_owned = "FA", round(f.value + 0.5, 1), round(rng.uniform(15, 60), 0)
    return LeagueSnapshot(
        league_name=name, league_id="demo", week=week, slot_counts=slot_counts, teams=teams, my_id=1,
        my_box=box, my_score=round(sum(p.week_points for p in box if p.slot != "BE"), 1),
        opp_score=round(rng.uniform(95, 125), 1), opp_name=teams[1].name, free_agents=fas,
        bye_map={"KC": week + 2, "DEN": week + 1, "MIA": week + 3},
        notes=["Demo data: made-up players and scores, shown only to preview the layout."],
    )


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def build_analyses(settings, season: int, week: int) -> list[dict]:
    from .team_fetch import bye_weeks, fetch_snapshot

    try:
        bye_map = bye_weeks(season)
    except Exception as e:  # noqa: BLE001
        print(f"  ! bye weeks unavailable: {e.__class__.__name__}: {e}")
        bye_map = {}
    out = []
    for cfg in settings.leagues:
        try:
            snap = fetch_snapshot(cfg, settings, season, week, bye_map)
            out.append(analyze(snap))
            print(f"  [{cfg.league_name}] analysed: {len(snap.teams)} teams, "
                  f"{len(snap.free_agents)} free agents")
        except Exception as e:  # noqa: BLE001
            print(f"  ! [{cfg.league_name}] team report failed: {e.__class__.__name__}: {e}")
            out.append({"error": f"{e.__class__.__name__}: {e}", "league_name": cfg.league_name})
    return out


def run_team_report(settings, season: int, week: int, email: bool = False, demo: bool = False,
                    now: datetime | None = None, late: bool = False) -> str:
    if demo:
        analyses = [analyze(demo_snapshot("More Cowbell (demo)", seed=7, week=week)),
                    analyze(demo_snapshot("CBFFL (demo)", seed=11, week=week))]
    else:
        analyses = build_analyses(settings, season, week)
        if not any(not a.get("error") for a in analyses):
            raise RuntimeError("no league produced a team report")

    subject, doc, text = render_email(analyses, week, now, late, demo=demo)
    print(text)
    REPORT_DIR.mkdir(exist_ok=True)
    out = REPORT_DIR / f"team_report_{season}_wk{week}{'_demo' if demo else ''}.html"
    out.write_text(doc)
    print(f"\nTeam report written: {out}")
    if email and not demo:
        from .email_send import send_email

        send_email(settings, subject, doc, text)
    return text


# ---- Tuesday 8am ET, driven from `run.py --scheduled` ----------------------- #
def _state_path(season: int) -> Path:
    return STATE_DIR / f"team_report_{season}.json"


def _load_state(season: int) -> dict:
    p = _state_path(season)
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except Exception:
        return {}


def _save_state(season: int, state: dict) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    _state_path(season).write_text(json.dumps(state, indent=2))


def maybe_send_team_report(settings, season: int, now: datetime, current_week: int,
                           throttle: timedelta = timedelta(minutes=60)) -> bool:
    """Email the report for the last finished week, once, from Tuesday 8am ET on.
    Safe to call on every scheduler tick; failures are retried hourly, never raised."""
    try:
        from .evaluate import week_ready_time

        week = current_week - 1
        if week < 1:
            return False
        state = _load_state(season)
        if week in state.get("weeks_sent", []):
            return False
        ready = week_ready_time(season, week)
        if not ready or now < ready:
            return False
        last = state.get("last_attempt")
        if last and now - datetime.fromisoformat(last) < throttle:
            return False
        state["last_attempt"] = now.isoformat()
        _save_state(season, state)

        print(f"Building weekly team report for week {week}...")
        late = now - ready > timedelta(hours=6)
        run_team_report(settings, season, week, email=True, now=now, late=late)
        state["weeks_sent"] = sorted(set(state.get("weeks_sent", [])) | {week})
        _save_state(season, state)
        return True
    except Exception as e:  # noqa: BLE001 -- never let this break the send path
        print(f"  ! team report failed: {e.__class__.__name__}: {e}")
        return False
