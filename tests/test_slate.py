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


def test_one_window_per_game_day():
    games = [
        _g("BUF", "MIA", datetime(2025, 9, 18, 20, 15)),   # Thu night
        _g("CAR", "ATL", datetime(2025, 9, 21, 13, 0)),    # Sun early
        _g("NYG", "KC", datetime(2025, 9, 21, 20, 20)),    # SNF
        _g("BAL", "DET", datetime(2025, 9, 22, 20, 15)),   # MNF
    ]
    ws = {w.key: w for w in _windows_from(games, datetime(2025, 9, 17, 9, 0))}
    assert list(ws) == ["thursday", "sunday", "monday"]

    assert ws["thursday"].deadline == datetime(2025, 9, 18, 17, 0, tzinfo=ET)
    assert ws["thursday"].teams == {"BUF", "MIA"}

    assert ws["sunday"].deadline == datetime(2025, 9, 21, 10, 0, tzinfo=ET)
    assert ws["sunday"].teams == {"CAR", "ATL", "NYG", "KC"}

    # Monday now gets its own email, by 5pm ET Monday
    assert ws["monday"].deadline == datetime(2025, 9, 22, 17, 0, tzinfo=ET)
    assert ws["monday"].teams == {"BAL", "DET"}


def test_early_london_game_pulls_sunday_deadline_earlier():
    games = [
        _g("BUF", "MIA", datetime(2025, 9, 18, 20, 15)),
        _g("JAX", "ATL", datetime(2025, 9, 21, 9, 30)),    # London 9:30am ET
        _g("CAR", "NO", datetime(2025, 9, 21, 13, 0)),
    ]
    ws = {w.key: w for w in _windows_from(games, datetime(2025, 9, 17, 9, 0))}
    assert ws["sunday"].deadline == datetime(2025, 9, 21, 8, 0, tzinfo=ET)


def test_wednesday_opener_gets_its_own_email():
    games = [
        _g("KC", "BAL", datetime(2025, 9, 3, 20, 20)),     # Wed opener
        _g("PHI", "GB", datetime(2025, 9, 7, 13, 0)),      # Sun
    ]
    ws = {w.key: w for w in _windows_from(games, datetime(2025, 9, 1, 9, 0))}
    assert set(ws) == {"wednesday", "sunday"}
    assert ws["wednesday"].teams == {"KC", "BAL"}
    assert ws["wednesday"].deadline == datetime(2025, 9, 3, 17, 0, tzinfo=ET)


def test_late_season_saturday_and_christmas_games():
    games = [
        _g("DEN", "KC", datetime(2025, 12, 20, 13, 0)),    # Sat 1pm
        _g("LAR", "SEA", datetime(2025, 12, 20, 20, 0)),   # Sat night
        _g("PHI", "WAS", datetime(2025, 12, 21, 13, 0)),   # Sun
        _g("DAL", "NYG", datetime(2025, 12, 25, 13, 0)),   # Christmas Thu
    ]
    ws = {w.key: w for w in _windows_from(games, datetime(2025, 12, 18, 9, 0))}
    assert list(ws) == ["saturday", "sunday", "thursday"]
    # 2h before the 1pm Saturday kickoff beats the 5pm default
    assert ws["saturday"].deadline == datetime(2025, 12, 20, 11, 0, tzinfo=ET)
    assert ws["saturday"].teams == {"DEN", "KC", "LAR", "SEA"}
    assert ws["thursday"].deadline == datetime(2025, 12, 25, 11, 0, tzinfo=ET)


def test_only_sunday_games():
    games = [_g("PHI", "GB", datetime(2025, 9, 7, 13, 0))]
    ws = {w.key: w for w in _windows_from(games, datetime(2025, 9, 1, 9, 0))}
    assert set(ws) == {"sunday"}


def test_legacy_state_from_two_email_schedule():
    from ffball.slate import legacy_sent_keys

    games = [
        _g("BUF", "MIA", datetime(2025, 9, 18, 20, 15)),
        _g("CAR", "ATL", datetime(2025, 9, 21, 13, 0)),
        _g("BAL", "DET", datetime(2025, 9, 22, 20, 15)),
    ]
    ws = _windows_from(games, datetime(2025, 9, 17, 9, 0))
    assert legacy_sent_keys({"pre_weekend"}, ws) == {"pre_weekend", "thursday"}
    # old "sunday" covered Monday too, but Monday now gets its own email
    assert "monday" not in legacy_sent_keys({"pre_weekend", "sunday"}, ws)


def test_lock_label():
    now = datetime(2025, 9, 18, 12, 0, tzinfo=ET)
    assert lock_label(datetime(2025, 9, 18, 20, 15, tzinfo=ET), now) == "locks today 8:15pm ET"
    assert lock_label(datetime(2025, 9, 21, 13, 0, tzinfo=ET), now).startswith("locks Sun 1:00pm")
    assert lock_label(datetime(2025, 9, 18, 11, 0, tzinfo=ET), now) == "LOCKED"
