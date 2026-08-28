"""Send-window timing logic."""

from datetime import datetime, timedelta

from ffball.slate import ET, Game, SendWindow, build_send_windows, lock_label


def _g(home, away, dt):
    return Game(home=home, away=away, kickoff=dt.replace(tzinfo=ET),
                weekday=dt.strftime("%A"), game_type="REG")


def _windows_from(games, now):
    """Re-run the splitter's logic on an explicit game list (bypasses schedule)."""
    import ffball.slate as slate

    orig = slate._games_for_week
    slate._games_for_week = lambda s, w: games
    try:
        return build_send_windows(2025, 3, now.replace(tzinfo=ET))
    finally:
        slate._games_for_week = orig


def test_thursday_and_sunday_windows():
    games = [
        _g("BUF", "MIA", datetime(2025, 9, 18, 20, 15)),   # Thu night
        _g("CAR", "ATL", datetime(2025, 9, 21, 13, 0)),    # Sun early
        _g("NYG", "KC", datetime(2025, 9, 21, 20, 20)),    # SNF
        _g("BAL", "DET", datetime(2025, 9, 22, 20, 15)),   # MNF
    ]
    ws = {w.key: w for w in _windows_from(games, datetime(2025, 9, 17, 9, 0))}
    assert set(ws) == {"pre_weekend", "sunday"}

    # Thursday email must be out by Thursday 5:00pm ET
    pre = ws["pre_weekend"]
    assert pre.deadline == datetime(2025, 9, 18, 17, 0, tzinfo=ET)
    assert pre.teams == {"BUF", "MIA"}

    # Sunday email by Sunday 10:00am ET, and it covers MNF too
    sun = ws["sunday"]
    assert sun.deadline == datetime(2025, 9, 21, 10, 0, tzinfo=ET)
    assert "BAL" in sun.teams and "DET" in sun.teams


def test_early_london_game_pulls_sunday_deadline_earlier():
    games = [
        _g("BUF", "MIA", datetime(2025, 9, 18, 20, 15)),
        _g("JAX", "ATL", datetime(2025, 9, 21, 9, 30)),    # London 9:30am ET
        _g("CAR", "NO", datetime(2025, 9, 21, 13, 0)),
    ]
    ws = {w.key: w for w in _windows_from(games, datetime(2025, 9, 17, 9, 0))}
    # 90 min before 9:30am kickoff = 8:00am, earlier than the 10:00am default
    assert ws["sunday"].deadline == datetime(2025, 9, 21, 8, 0, tzinfo=ET)


def test_wednesday_opener_is_not_dropped():
    games = [
        _g("KC", "BAL", datetime(2025, 9, 3, 20, 20)),     # Wed opener
        _g("PHI", "GB", datetime(2025, 9, 7, 13, 0)),      # Sun
    ]
    ws = {w.key: w for w in _windows_from(games, datetime(2025, 9, 1, 9, 0))}
    assert "KC" in ws["pre_weekend"].teams
    assert ws["pre_weekend"].deadline == datetime(2025, 9, 3, 17, 0, tzinfo=ET)


def test_no_pre_weekend_window_when_all_sunday():
    games = [_g("PHI", "GB", datetime(2025, 9, 7, 13, 0))]
    ws = {w.key: w for w in _windows_from(games, datetime(2025, 9, 1, 9, 0))}
    assert set(ws) == {"sunday"}


def test_lock_label():
    now = datetime(2025, 9, 18, 12, 0, tzinfo=ET)
    assert lock_label(datetime(2025, 9, 18, 20, 15, tzinfo=ET), now) == "locks today 8:15pm ET"
    assert lock_label(datetime(2025, 9, 21, 13, 0, tzinfo=ET), now).startswith("locks Sun 1:00pm")
    assert lock_label(datetime(2025, 9, 18, 11, 0, tzinfo=ET), now) == "LOCKED"
