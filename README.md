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

The tool knows each player's kickoff (from the NFL schedule) and sends up to
**two emails per week**:

| Window | Covers | Deadline (email arrives before this) |
|---|---|---|
| `pre_weekend` | Thu / Fri / Sat games (incl. a Wed opener) | **5:00pm ET on the game's day**, or 2h before the earliest of those kickoffs, whichever is earlier |
| `sunday` | Sunday + Monday games | **10:00am ET Sunday**, or 90m before the earliest Sunday kickoff (e.g. a 9:30am London game pulls it to 8:00am) |

Each email is scoped to the players who lock in that window; everyone else's
call comes in the next email. Every row shows a "Locks" time, and the players
locking now are sorted to the top.

### Preview a window without waiting

```bash
uv run run.py --window pre_weekend            # render the Thursday email now
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
| `src/ffball/main.py`        | orchestrator |
| `tests/`                    | scoring-engine + offline-pipeline tests |
