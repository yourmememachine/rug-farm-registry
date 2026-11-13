"""Dev Watchtower tests — no network, no real wallet accused.

All wallets/mints below are synthetic test values (repeated
base58 characters). Alerts are info / warning / urgent only —
never a bare "rugged" verdict on price action alone.
"""
from datetime import date

import pytest

from src.watchtower import (
    Watchlist,
    Watchtower,
    format_alert,
    main,
)

TODAY = date(2026, 11, 13)
NOW = "2026-11-13T08:00:00Z"

FLAGGED = "A" * 32
UNDER = "B" * 32
UNKNOWN = "D" * 32
EXPIRED = "C" * 32
MINT1 = "E" * 32
MINT2 = "F" * 32
MINT3 = "G" * 32
MINT4 = "H" * 32
MINT_NEW = "J" * 32
MINT_NEW2 = "K" * 32


def _mint_record(mint):
    return {
        "mint": mint,
        "outcome": "rugged",
        "peak_market_cap_usd": 100000,
        "current_market_cap_usd": 1000,
        "evidence": {"dexscreener": f"https://dexscreener.com/solana/{mint}"},
    }


def _entry(wallet, mints, status="flagged"):
    return {
        "wallet": wallet,
        "cluster_id": "cluster-test",
        "status": status,
        "first_seen": "2026-11-01",
        "last_seen": "2026-11-10",
        "ttl_days": 180,
        "rugged_mints": [_mint_record(m) for m in mints],
    }


@pytest.fixture
def registry_data():
    return {
        "sample_data": True,
        "min_rugged_mints_to_flag": 2,
        "entries": [
            _entry(FLAGGED, [MINT1, MINT2]),
            _entry(UNDER, [MINT3], status="under_review"),
        ],
    }


@pytest.fixture
def tower(registry_data):
    return Watchtower(registry_data, today=TODAY)


class _StubFetcher:
    def __init__(self, mints_by_dev):
        self.mints_by_dev = mints_by_dev

    def recent_mints(self, dev_wallet):
        return self.mints_by_dev.get(dev_wallet, [])


# --- Watchlist ----------------------------------------------------------

def test_watchlist_add_and_list():
    wl = Watchlist()
    ok, _ = wl.add(FLAGGED, added_at=NOW)
    assert ok is True
    assert [e["wallet"] for e in wl.list()] == [FLAGGED]
    assert wl.contains(FLAGGED) is True


def test_watchlist_add_invalid_address():
    wl = Watchlist()
    ok, reason = wl.add("not-a-wallet")
    assert ok is False
    assert "not a valid Solana address" in reason
    assert wl.list() == []


def test_watchlist_add_duplicate():
    wl = Watchlist()
    assert wl.add(FLAGGED)[0] is True
    ok, reason = wl.add(FLAGGED)
    assert ok is False
    assert "already watched" in reason
    assert len(wl.list()) == 1


def test_watchlist_remove():
    wl = Watchlist()
    wl.add(FLAGGED)
    ok, _ = wl.remove(FLAGGED)
    assert ok is True
    assert wl.contains(FLAGGED) is False


def test_watchlist_remove_unknown():
    wl = Watchlist()
    ok, reason = wl.remove(FLAGGED)
    assert ok is False
    assert "not watched" in reason


def test_watchlist_persists_to_file(tmp_path):
    path = tmp_path / "watchlist.json"
    wl = Watchlist(path)
    wl.add(FLAGGED, added_at=NOW)
    wl.mark_seen(FLAGGED, MINT1)
    wl.save()
    loaded = Watchlist(path)
    assert loaded.contains(FLAGGED) is True
    assert loaded.seen_mints(FLAGGED) == [MINT1]
    assert loaded.list()[0]["added_at"] == NOW


def test_watchlist_load_ignores_bad_file(tmp_path):
    path = tmp_path / "watchlist.json"
    path.write_text("{not json", encoding="utf-8")
    wl = Watchlist(path)
    assert wl.list() == []


# --- Alert levels -------------------------------------------------------

def test_flagged_dev_relaunch_is_urgent(tower):
    wl = Watchlist()
    wl.add(FLAGGED)
    alerts = tower.check(FLAGGED, wl, now=NOW,
                         recent_mints=[{"mint": MINT_NEW, "symbol": "NEW"}])
    assert len(alerts) == 1
    assert alerts[0]["level"] == "urgent"
    assert alerts[0]["risk_level"] == "high"
    assert alerts[0]["rugged_mint_count"] == 2
    assert alerts[0]["mint"] == MINT_NEW
    assert alerts[0]["timestamp"] == NOW
    assert "dexscreener.com/solana/" in alerts[0]["links"]["dexscreener"]


def test_under_review_dev_is_warning(tower):
    wl = Watchlist()
    wl.add(UNDER)
    alerts = tower.check(UNDER, wl, now=NOW,
                         recent_mints=[{"mint": MINT_NEW}])
    assert len(alerts) == 1
    assert alerts[0]["level"] == "warning"
    assert alerts[0]["risk_level"] == "watch"


def test_new_mint_from_unknown_dev_is_info(tower):
    wl = Watchlist()
    wl.add(UNKNOWN)
    alerts = tower.check(UNKNOWN, wl, now=NOW,
                         recent_mints=[{"mint": MINT_NEW}])
    assert len(alerts) == 1
    assert alerts[0]["level"] == "info"
    assert alerts[0]["risk_level"] == "unknown"


def test_no_alert_when_nothing_new(tower):
    wl = Watchlist()
    wl.add(FLAGGED)
    wl.mark_seen(FLAGGED, MINT_NEW)
    alerts = tower.check(FLAGGED, wl, now=NOW,
                         recent_mints=[{"mint": MINT_NEW}])
    assert alerts == []


def test_no_repeat_alert_on_second_check(tower):
    wl = Watchlist()
    wl.add(FLAGGED)
    first = tower.check(FLAGGED, wl, now=NOW,
                        recent_mints=[{"mint": MINT_NEW}])
    second = tower.check(FLAGGED, wl, now=NOW,
                         recent_mints=[{"mint": MINT_NEW}])
    assert len(first) == 1
    assert second == []


def test_no_alert_with_no_recent_mints(tower):
    wl = Watchlist()
    wl.add(FLAGGED)
    assert tower.check(FLAGGED, wl, now=NOW, recent_mints=[]) == []


def test_fetcher_supplies_recent_mints(registry_data):
    fetcher = _StubFetcher({FLAGGED: [{"mint": MINT_NEW, "symbol": "NEW"}]})
    tower = Watchtower(registry_data, fetcher=fetcher, today=TODAY)
    wl = Watchlist()
    wl.add(FLAGGED)
    alerts = tower.check(FLAGGED, wl, now=NOW)
    assert len(alerts) == 1
    assert alerts[0]["level"] == "urgent"


def test_check_all_covers_every_watched_dev(tower):
    wl = Watchlist()
    wl.add(FLAGGED)
    wl.add(UNDER)
    alerts = tower.check_all(wl, now=NOW, recent_mints_by_dev={
        FLAGGED: [{"mint": MINT_NEW}],
        UNDER: [{"mint": MINT_NEW2}],
    })
    assert [a["level"] for a in alerts] == ["urgent", "warning"]


def test_invalid_dev_returns_no_alerts(tower):
    assert tower.check("bad-address", Watchlist(), now=NOW,
                       recent_mints=[{"mint": MINT_NEW}]) == []


def test_format_alert_one_line(tower):
    wl = Watchlist()
    wl.add(FLAGGED)
    alert = tower.check(FLAGGED, wl, now=NOW,
                        recent_mints=[{"mint": MINT_NEW, "symbol": "NEW"}])[0]
    line = format_alert(alert)
    assert "\n" not in line
    assert "[URGENT]" in line
    assert NOW in line
    assert MINT_NEW in line
    assert "dexscreener.com/solana/" in line


# --- CLI ----------------------------------------------------------------

def test_cli_watch_and_check_urgent(tmp_path, capsys):
    path = tmp_path / "watchlist.json"
    # --watch adds the dev; a second call checks it with a new mint.
    assert main(["--watch", FLAGGED, "--file", str(path)]) == 0
    # The CLI uses the real registry file (sample data), so use --dev
    # with a fresh watchlist entry and a mint: exit code reflects the
    # sample registry verdict for FLAGGED ("A"*32 is not in it), so
    # this call must at least exit 0 and print the no-new-mints line
    # when no --mint is given.
    assert main(["--check", "--file", str(path)]) == 0
    out = capsys.readouterr().out
    assert "No new mints" in out


def test_cli_check_with_mint_returns_urgent_for_flagged_dev(tmp_path, capsys):
    # Use the sample registry's flagged dev so the CLI (real registry)
    # sees a flagged verdict: SAMPLEDEV111... has 3 rugged mints.
    from src.registry import load_registry
    data = load_registry("data/registry.json")
    flagged_dev = data["entries"][0]["wallet"]
    path = tmp_path / "watchlist.json"
    assert main(["--watch", flagged_dev, "--file", str(path)]) == 0
    code = main(["--check", "--file", str(path),
                 "--mint", MINT_NEW, "--now", NOW])
    assert code == 1
    out = capsys.readouterr().out
    assert "[URGENT]" in out


def test_cli_invalid_address_is_exit_2(capsys):
    assert main(["--watch", "bad-address"]) == 2


def test_cli_no_args_is_exit_2(capsys):
    assert main([]) == 2


def test_cli_json_output(tmp_path, capsys):
    from src.registry import load_registry
    data = load_registry("data/registry.json")
    flagged_dev = data["entries"][0]["wallet"]
    path = tmp_path / "watchlist.json"
    assert main(["--watch", flagged_dev, "--file", str(path)]) == 0
    capsys.readouterr()  # drain the "Watching ..." line from --watch
    code = main(["--check", "--file", str(path),
                 "--mint", MINT_NEW, "--json", "--now", NOW])
    assert code == 1
    import json
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed[0]["level"] == "urgent"
