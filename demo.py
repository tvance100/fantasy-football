"""Generate a sample report with a synthetic roster + real shared data.

Run this to preview the output format without ESPN credentials:
    uv run demo.py   ->   reports/demo.html
"""

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from ffball.report import build_email_html, render_league_section  # noqa: E402
from ffball.shared_data import load_shared_data  # noqa: E402
from ffball.startsit import optimize  # noqa: E402
from ffball.projections import project_roster  # noqa: E402
from tests.test_pipeline_offline import _make_league  # noqa: E402

SEASON, WEEK = 2024, 12

sd = load_shared_data(season=SEASON, week=WEEK)
ld = _make_league()
ld.week = WEEK
projs = project_roster(ld, sd)
lineup = optimize(projs, ld.starting_slot_counts, WEEK)

# second synthetic league with different (standard) scoring to show the contrast
ld2 = _make_league()
ld2.week = WEEK
ld2.config.league_name = "College Friends League (Standard, SUPERFLEX)"
ld2.config.scoring_format = [x if x["id"] != 53 else dict(x, points=0) for x in ld2.config.scoring_format]
ld2.config.scoring_type = "standard"
ld2.config.roster_slots = {"QB": 1, "RB": 2, "WR": 3, "TE": 1, "FLEX": 1, "SUPERFLEX": 1, "K": 1, "D/ST": 1}
ld2.starting_slot_counts = ld2.config.roster_slots
projs2 = project_roster(ld2, sd)
lineup2 = optimize(projs2, ld2.starting_slot_counts, WEEK)

doc = build_email_html(
    [render_league_section(ld, lineup, projs), render_league_section(ld2, lineup2, projs2)],
    sd,
)
out = Path("reports/demo.html")
out.parent.mkdir(exist_ok=True)
out.write_text(doc)
print(f"wrote {out}")

# windowed preview (the Thursday email)
from datetime import datetime  # noqa: E402

from ffball.slate import ET, build_send_windows  # noqa: E402

windows = build_send_windows(SEASON, WEEK, datetime(SEASON, 11, 1, 12, 0, tzinfo=ET))
if windows:
    w = windows[0]
    wdoc = build_email_html(
        [render_league_section(ld, lineup, projs, w),
         render_league_section(ld2, lineup2, projs2, w)],
        sd, w,
    )
    wout = Path("reports/demo_window.html")
    wout.write_text(wdoc)
    print(f"wrote {wout}  (window: {w.key}, deadline {w.deadline})")
