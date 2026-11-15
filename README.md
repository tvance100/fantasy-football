# Multi-league fantasy football start/sit

Pulls league-agnostic NFL data once, then for **each** ESPN league on your
account applies **that league's own live scoring + roster rules** to project
fantasy points and produce a start/sit report. One email, one clearly labelled
section per league.

```
shared data (once)                 per league (loop)
──────────────────                 ─────────────────────────────────────────
nfl_data_py weekly stats  ┐        1. live roster            (espn_api)
  → recent form           │        2. live scoring rules     (ESPN mSettings)
  → defense vs position   ├──────▶ 3. live roster slots      (ESPN mSettings)
nfl_data_py snap counts   │        4. project each player:
nfl_data_py injuries      │             ESPN stat projection
Open-Meteo weather        │             × form × DvP × weather × snaps × injury
nfl_data_py schedules     ┘             → score with THIS league's formula
                                   5. optimal lineup vs THIS league's slots
                                   6. start/sit + watch list + droppables
                                   ─────────────────────────────────────────
                                   combine → reports/*.html → Gmail SMTP
```

## Setup

```bash
cd ~/fantasy-football
python3 -m uv sync --extra dev          # creates .venv, installs deps
cp .env.example .env                    # then edit .env  (see below)
uv run demo.py                          # optional: preview report format, no creds needed
uv run run.py                           # the real thing
```

(`uv` is already installed via `python3 -m pip install --user uv`. If `uv` isn't
on PATH, use `python3 -m uv` instead.)

### .env — ESPN authentication (private leagues)

You need two browser cookies from a logged-in ESPN session: `espn_s2` and `SWID`.

**Chrome / Edge**
1. Log in at <https://fantasy.espn.com> and open one of your leagues.
2. `View → Developer → Developer Tools` (or `Cmd+Option+I`).
3. **Application** tab → left sidebar **Storage → Cookies → `https://fantasy.espn.com`**.
4. Find the row named **`espn_s2`** — double-click its *Value*, copy the whole
   thing (it's long and contains `%` characters). Paste as `ESPN_S2=` in `.env`.
   Do **not** URL-decode it.
5. Find the row named **`SWID`** — copy its value *including the curly braces*,
   e.g. `{1A2B3C4D-...}`. Paste as `ESPN_SWID=`.

**Safari**
1. `Safari → Settings → Advanced → ☑ Show features for web developers`.
2. `Develop → Show Web Inspector` → **Storage** tab → **Cookies** →
   `fantasy.espn.com`. Copy `espn_s2` and `SWID` as above.

**Firefox**
1. `Tools → Browser Tools → Web Developer Tools` → **Storage** tab →
   **Cookies** → `https://fantasy.espn.com`. Copy `espn_s2` and `SWID`.

These cookies last several months. If the tool starts returning empty rosters,
they've expired — repeat the steps.

> Public leagues need no cookies — leave `ESPN_S2` / `ESPN_SWID` blank.

### .env — finding league_id and team_id

Open your league on ESPN. The URL looks like:

```
https://fantasy.espn.com/football/team?leagueId=123456&teamId=4
                                                ^^^^^^         ^
                                             LEAGUE_n_ID   LEAGUE_n_TEAM_ID
```

Add a `LEAGUE_1_*`, `LEAGUE_2_*`, … block per league (numbering starts at 1 and
must be contiguous).

### .env — Gmail sending

1. Enable 2-Step Verification on the Google account.
2. Create an **App Password**: <https://myaccount.google.com/apppasswords>
   (name it e.g. "fantasy"). Google shows a 16-character string.
3. `GMAIL_APP_PASSWORD=` that string (spaces optional), `GMAIL_ADDRESS=` your
   Gmail, `EMAIL_TO=` where the report should go.
4. Set `EMAIL_ENABLED=1` when you're ready to actually send. With `=0` it only
   writes `reports/startsit_wkN_YYYYMMDD.html`.

## Game-day timing

The tool reads the NFL schedule (refreshed every 12 hours, so flexed or newly
set games are picked up) and sends **one email for every day that has a game**:
Thursday, Sunday and Monday most weeks, plus Wednesday, Friday, Saturday,
Tuesday or holiday games whenever the schedule has them.

| Day | Deadline (email arrives before this) |
|---|---|
| Sunday | **10:00am ET**, or 90m before the first Sunday kickoff (a 9:30am London game pulls it to 8:00am) |
| Any other game day | **5:00pm ET**, or 2h before that day's first kickoff (a 1:00pm Thanksgiving or Saturday game pulls it to 11:00am) |

Each email is scoped to the players whose games are that day; everyone else's
call comes in that day's email. Every row shows a "Locks" time, and the players
locking now are sorted to the top.

### Preview a window without waiting

```bash
uv run run.py --window thursday               # render this week's Thursday email now
uv run run.py --now 2025-09-21T09:00 --week 3 # pretend it's a given ET moment
```

### Run it automatically (macOS launchd)

```bash
cp launchd/com.ffball.startsit.plist ~/Library/LaunchAgents/
sed -i '' "s|__HOME__|$HOME|g" ~/Library/LaunchAgents/com.ffball.startsit.plist
launchctl load ~/Library/LaunchAgents/com.ffball.startsit.plist
```

It runs `bin/run-scheduled.sh` every 10 minutes. That script calls
`run.py --scheduled`, which is a **no-op unless a window deadline is within 15
minutes and hasn't been sent** (state in `.state/sent_<season>.json`). If the
Mac is asleep at the deadline, launchd fires it once on wake and the email still
goes out as long as the games haven't kicked off. Logs in `logs/`.

Set `EMAIL_ENABLED=1` in `.env` first, or it just writes `reports/*.html`.

Uninstall: `launchctl unload ~/Library/LaunchAgents/com.ffball.startsit.plist`

### Or cron (simpler, but won't fire if the laptop is asleep)

```cron
*/10 * * * 5,0,1 cd ~/fantasy-football && .venv/bin/python run.py --scheduled >> logs/cron.log 2>&1
```

## Weekly team report (Tuesday 8am ET)

Every Tuesday at 8am ET (once Monday night's game is final) an email goes out for
**each of your leagues** covering the week that just ended:

- **Overall assessment** in plain English: record and standing, scoring rank, an
  "all-play" luck check (how many wins your points would have earned against the whole
  league vs the wins you actually have), and last week's result and lineup efficiency.
- **Strengths & weaknesses** by position, ranked against every other team in the league
  (QB / RB / WR / TE / K / D/ST / flex), plus injuries and bye weeks coming up.
- **Last week's decisions**: points left on the bench, the costliest benching mistakes,
  top scorers and biggest misses against ESPN's projection.
- **Waiver / free-agent targets** with who to drop, ranked by how much your best lineup
  improves (including cover for next week's byes and injuries).
- **Trade ideas** (1-for-1 and 2-for-1) that raise *both* teams' best lineup on paper, so
  the other manager has a reason to say yes.

It runs from the same launchd job as everything else (no reinstall), sends once per
finished week, retries hourly if ESPN is down, and sends late (with a note) if the Mac
was asleep at 8am. State lives in `.state/team_report_<season>.json`.

```bash
.venv/bin/python run.py --team-report --demo    # preview the layout on made-up data, no ESPN needed
.venv/bin/python run.py --team-report           # build the real report (writes reports/team_report_*.html)
.venv/bin/python run.py --team-report --email   # ...and email it
.venv/bin/python run.py --team-report --week 3  # a specific finished week
```

How it judges players: one number per player, "expected points per game going
forward", in each league's own scoring: a blend of season production and ESPN's projected
average (weighted toward production as games are played), reduced for injury status.
Positional strength, waiver upgrades and trades are all "how many points per week does
the best lineup gain" under that number. The written assessment is rule-based text built
from those numbers (nothing is generated by a model at run time), so treat the trade
ideas as starting points: check injuries, bye weeks, waiver priority and your league's
trade rules before acting. Thresholds live at the top of `team_analysis.py`.

## Tracking & evaluation (does it actually beat ESPN?)

Every email the scheduled run sends is also written to a SQLite log at
`.state/tracking.sqlite` (gitignored): ESPN's projection, ours, every multiplier
applied (form, defense-vs-position, weather, snaps, injury), the projected stat
line, and the recommended slot for each player, as a snapshot at send time. A
tracking failure can never block an email.

```bash
uv run run.py --backfill                 # one-time: import the emails already in ./reports/
uv run run.py --evaluate                 # fetch final box scores for finished weeks, print the scorecard
uv run run.py --evaluate --week 4        # grade (re-fetch) one week
uv run run.py --evaluate --email         # also email the scorecard
uv run run.py --evaluate --no-fetch      # re-render from stored data, no ESPN calls
uv run run.py --log                      # manual run that also logs (scheduled runs always log)
```

You don't have to run `--evaluate` by hand: on an idle scheduler tick, once a
logged week is over (Tuesday 8am ET after Monday night) the scorecard is built
from ESPN's final box scores, saved to `reports/scorecard_<season>_wk<N>.txt`,
and emailed. Attempts are throttled to one per hour; state is in
`.state/eval_<season>.json`.

The scorecard reports (all against **your leagues' own scoring**, from ESPN box scores):

- **Lineup results** — actual points of the lineup the tool recommended, the lineup
  ESPN's numbers alone would pick, the lineup you actually started, and the
  hindsight-best lineup from the same roster.
- **Projection accuracy** — MAE and bias per position, ours vs ESPN's, with a 95%
  interval on how often ours was closer.
- **Ranking** — among same-position players in a league-week, how often the
  higher-projected one outscored the lower one, and who was right where we
  disagreed with ESPN.
- **Delta buckets** — when we differ from ESPN by a lot, who was closer?

How to read it: weekly fantasy scores are very noisy and a season is a few hundred
data points, so intervals will be wide for a long time. The scorecard says so when
the sample is small. Nothing here changes the model; it only measures it.
`MODEL_VERSION` in `projections.py` is stored with every row — bump it whenever the
projection logic or constants change so versions can be compared.

Backfilled weeks (reconstructed from old email HTML) have no factor detail, skip
players who were already locked when that email was generated (their numbers would
leak hindsight), and infer flex positions from the box score.

## Notes & limitations

- **Preseason / week-0:** if the configured `SEASON` has no games yet, projections
  fall back to the previous season's game logs (a data note says so). ESPN's own
  per-player projection is still used as the anchor.
- **`nfl_data_py` is stale:** v0.3.2 hardcodes the old nflverse URL for weekly
  player stats and 404s on 2025+. `shared_data._load_weekly()` falls back to the
  current `stats_player` release asset, so form + defense-vs-position still work.
  Snap counts / injuries / schedules are unaffected.
- Weekly injury reports don't exist in the preseason (nflverse publishes them
  from Week 1), so the injury layer is empty until the season starts — ESPN's own
  `injuryStatus` on each player still applies.
- **Weather** only resolves inside Open-Meteo's ~16-day forecast window; earlier
  than that every outdoor game shows "forecast not yet available" and weather has
  no effect.
- Projections are a *blend*, not gospel. The report always shows ESPN's number
  next to ours and the delta. Check official inactives before kickoff.
- Not financial or investment advice.

## Layout

| file | role |
|------|------|
| `src/ffball/shared_data.py` | step 1 — league-agnostic pulls, cached to `.cache/` |
| `src/ffball/espn.py`        | live roster + live scoring + live roster slots |
| `src/ffball/scoring.py`     | apply an arbitrary ESPN scoring format to a stat line |
| `src/ffball/projections.py` | ESPN projection × shared-data adjustments |
| `src/ffball/startsit.py`    | optimal lineup vs a league's slot counts |
| `src/ffball/slate.py`       | kickoff times, Thu/Sun send windows, NFL-week detection |
| `src/ffball/report.py`      | HTML + plaintext rendering (window-scoped) |
| `src/ffball/email_send.py`  | Gmail SMTP |
| `src/ffball/tracking.py`    | SQLite prediction log (snapshot at send time) |
| `src/ffball/evaluate.py`    | grade predictions vs ESPN box-score actuals; scorecard; weekly auto-run |
| `src/ffball/backfill.py`    | rebuild log rows from past `reports/*.html` emails |
| `src/ffball/team_analysis.py` | pure analysis for the weekly team report (strength, waivers, trades) |
| `src/ffball/team_fetch.py`  | fills that analysis from ESPN (rosters, standings, box scores, free agents) |
| `src/ffball/team_report.py` | team-report email rendering, demo data, Tuesday 8am scheduling |
| `src/ffball/main.py`        | orchestrator |
| `tests/`                    | scoring-engine + offline-pipeline tests |
