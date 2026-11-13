"""AI agents tests — no network, no real wallet accused.

All wallets/mints below are synthetic test values (repeated
base58 characters). Watch labels are 'collapsed' /
'thin_liquidity' / 'active' only — never 'rugged'.
"""
from datetime import date

import pytest

from src.agents import (
    AlertAgent,
    EvidenceAgent,
    ScannerAgent,
    VerdictAgent,
    main,
    pipeline_check,
)

TODAY = date(2026, 11, 13)

FLAGGED = "A" * 32
UNDER = "B" * 32
EXPIRED = "C" * 32
UNKNOWN = "D" * 32
MINT1 = "E" * 32
MINT2 = "F" * 32
MINT3 = "G" * 32
MINT4 = "H" * 32
MINT5 = "J" * 32
MINT_COLLAPSED = "K" * 32
MINT_THIN = "L" * 32
MINT_ACTIVE = "M" * 32


def _mint_record(mint, symbol="TEST"):
    return {
        "mint": mint,
        "symbol": symbol,
        "outcome": "rugged",
        "peak_market_cap_usd": 100000,
        "current_market_cap_usd": 1000,
        "collapse_pct": 99.0,
        "evidence": {
            "dexscreener": f"https://dexscreener.com/solana/{mint}",
            "solscan": f"https://solscan.io/token/{mint}",
        },
    }


def _entry(wallet, status, mints, last_seen="2026-11-01"):
    return {
        "wallet": wallet,
        "cluster_id": f"cluster-{wallet[:4]}",
        "status": status,
        "first_seen": "2026-10-01",
        "last_seen": last_seen,
        "ttl_days": 180,
        "rugged_mints": [_mint_record(m) for m in mints],
    }


@pytest.fixture()
def registry_data():
    return {
        "sample_data": True,
        "entries": [
            _entry(FLAGGED, "flagged", [MINT1, MINT2]),
            _entry(UNDER, "under_review", [MINT3]),
            _entry(EXPIRED, "flagged", [MINT4, MINT5], last_seen="2025-01-01"),
        ],
    }


def _candidate(mint, symbol, change, liquidity):
    return {
        "mint": mint,
        "symbol": symbol,
        "name": symbol,
        "market_cap_usd": 10000,
        "liquidity_usd": liquidity,
        "volume_24h_usd": 5000,
        "change_24h_pct": change,
        "watch_label": "collapsed"
        if change is not None and change <= -70
        else "thin_liquidity"
        if liquidity is not None and liquidity < 1000
        else "active",
        "dexscreener_url": f"https://dexscreener.com/solana/{mint}",
    }


class StubWatch:
    def __init__(self, entries):
        self._entries = {e["mint"]: e for e in entries}

    def fetch_token_details(self, mints):
        return [self._entries[m] for m in mints if m in self._entries]

    def watch(self, limit=5):
        return list(self._entries.values())[:limit]


# -- ScannerAgent ---------------------------------------------------------

def test_scanner_collapsed_label():
    scanner = ScannerAgent(StubWatch([_candidate(MINT_COLLAPSED, "CRASH", -91.5, 2752)]))
    out = scanner.scan([MINT_COLLAPSED])
    assert out[0]["watch_label"] == "collapsed"
    assert out[0]["change_24h_pct"] == -91.5
    assert out[0]["market_cap_usd"] == 10000


def test_scanner_thin_liquidity_label():
    scanner = ScannerAgent(StubWatch([_candidate(MINT_THIN, "THIN", 22.0, 458)]))
    assert scanner.scan([MINT_THIN])[0]["watch_label"] == "thin_liquidity"


def test_scanner_active_label():
    scanner = ScannerAgent(StubWatch([_candidate(MINT_ACTIVE, "LEAD", 122.0, 303552)]))
    assert scanner.scan([MINT_ACTIVE])[0]["watch_label"] == "active"


def test_scanner_never_invents_holders_or_rugged():
    scanner = ScannerAgent(StubWatch([_candidate(MINT_COLLAPSED, "CRASH", -91.5, 2752)]))
    out = scanner.scan([MINT_COLLAPSED])[0]
    assert out["holders"] is None
    assert out["watch_label"] != "rugged"
    assert "rugged" not in str(out.values())


def test_scanner_invalid_mint_raises():
    with pytest.raises(ValueError):
        ScannerAgent(StubWatch([])).scan(["not-an-address"])


def test_scanner_watch_latest():
    scanner = ScannerAgent(StubWatch([_candidate(MINT_ACTIVE, "LEAD", 122.0, 303552)]))
    assert len(scanner.watch_latest(limit=5)) == 1


# -- EvidenceAgent --------------------------------------------------------

def test_evidence_sufficient_for_flagged_dev_and_mint(registry_data):
    result = EvidenceAgent(registry_data, today=TODAY).collect(FLAGGED, mint=MINT1)
    assert result["sufficient"] is True
    assert result["verdict"] == "sufficient"
    assert result["mint_in_registry"] is True
    urls = [e["url"] for e in result["evidence"]]
    assert f"https://dexscreener.com/solana/{MINT1}" in urls
    assert f"https://solscan.io/account/{FLAGGED}" in urls


def test_evidence_insufficient_for_unknown_wallet(registry_data):
    result = EvidenceAgent(registry_data, today=TODAY).collect(UNKNOWN, mint=MINT1)
    assert result["sufficient"] is False
    assert result["verdict"] == "insufficient evidence"
    assert result["mint_in_registry"] is False
    assert any("Insufficient evidence" in r for r in result["reasons"])


def test_evidence_insufficient_when_mint_not_in_registry(registry_data):
    result = EvidenceAgent(registry_data, today=TODAY).collect(FLAGGED, mint=MINT3)
    assert result["mint_in_registry"] is False
    assert result["sufficient"] is False


def test_evidence_invalid_wallet_raises(registry_data):
    with pytest.raises(ValueError):
        EvidenceAgent(registry_data, today=TODAY).collect("bad")


# -- VerdictAgent ---------------------------------------------------------

def test_verdict_flagged_two_rugs_with_evidence(registry_data):
    result = VerdictAgent(registry_data, today=TODAY).decide(dev_wallet=FLAGGED, mint=MINT1)
    assert result["verdict"] == "flagged"
    assert result["flagged"] is True
    assert result["exit_code"] == 1
    assert result["rugged_mint_count"] == 2


def test_verdict_under_review_one_rug(registry_data):
    result = VerdictAgent(registry_data, today=TODAY).decide(dev_wallet=UNDER, mint=MINT3)
    assert result["verdict"] == "under_review"
    assert result["flagged"] is False
    assert result["exit_code"] == 0


def test_verdict_unknown_wallet(registry_data):
    result = VerdictAgent(registry_data, today=TODAY).decide(dev_wallet=UNKNOWN)
    assert result["verdict"] == "unknown"
    assert result["exit_code"] == 0


def test_verdict_price_drop_alone_never_flags(registry_data):
    candidate = _candidate(MINT_COLLAPSED, "CRASH", -91.5, 2752)
    result = VerdictAgent(registry_data, today=TODAY).decide(
        dev_wallet=UNKNOWN, mint=MINT_COLLAPSED, candidate=candidate
    )
    assert result["verdict"] == "unknown"
    assert result["flagged"] is False
    assert result["exit_code"] == 0
    assert any("Price drop alone" in r for r in result["reasons"])


def test_verdict_expired_entry_not_flagged(registry_data):
    result = VerdictAgent(registry_data, today=TODAY).decide(dev_wallet=EXPIRED, mint=MINT4)
    assert result["verdict"] == "not_flagged"
    assert result["flagged"] is False
    assert result["exit_code"] == 0


def test_verdict_no_dev_wallet_is_unknown(registry_data):
    candidate = _candidate(MINT_COLLAPSED, "CRASH", -91.5, 2752)
    result = VerdictAgent(registry_data, today=TODAY).decide(candidate=candidate)
    assert result["verdict"] == "unknown"
    assert result["exit_code"] == 0


# -- AlertAgent -----------------------------------------------------------

def test_alert_blocked_scan_is_honest():
    text = AlertAgent.format_daily_alert("2026-11-13", verdicts=[], checked=0, scan_blocked=True)
    assert "No new verified alerts." in text
    assert "Creations checked: 0" in text
    assert "never invents a wallet" in text
    assert "HELIUS_API_KEY" in text


def test_alert_formats_flagged_and_under_review():
    verdicts = [
        {"verdict": "flagged", "dev_wallet": FLAGGED, "rugged_mint_count": 2, "reasons": ["Flagged serial rugger: 2 rugged mints with on-chain evidence."]},
        {"verdict": "under_review", "dev_wallet": UNDER, "rugged_mint_count": 1, "reasons": ["Under review."]},
    ]
    text = AlertAgent.format_daily_alert(
        "2026-11-13", verdicts=verdicts, checked=2, scan_blocked=False
    )
    assert "1 flagged, 1 under review" in text
    assert FLAGGED in text
    assert "flagged serial rugger" in text.lower()


# -- Pipeline + CLI --------------------------------------------------------

def test_pipeline_flagged_end_to_end(registry_data):
    scanner = ScannerAgent(StubWatch([_candidate(MINT1, "RUG", -99.0, 100)]))
    result = pipeline_check(registry_data, mint=MINT1, dev_wallet=FLAGGED, scanner=scanner, today=TODAY)
    assert result["candidate"]["watch_label"] == "collapsed"
    assert result["evidence"]["sufficient"] is True
    assert result["verdict"]["verdict"] == "flagged"
    assert result["verdict"]["exit_code"] == 1


def test_cli_flagged_exit_1(registry_data, tmp_path, capsys):
    path = tmp_path / "registry.json"
    import json as _json

    path.write_text(_json.dumps(registry_data))
    scanner = ScannerAgent(StubWatch([_candidate(MINT1, "RUG", -99.0, 100)]))
    code = main(["--mint", MINT1, "--dev", FLAGGED, "--registry", str(path)], scanner=scanner)
    assert code == 1
    assert "flagged" in capsys.readouterr().out


def test_cli_unknown_mint_exit_0(tmp_path, capsys):
    scanner = ScannerAgent(StubWatch([_candidate(MINT_ACTIVE, "LEAD", 122.0, 303552)]))
    code = main(["--mint", MINT_ACTIVE], scanner=scanner)
    assert code == 0


def test_cli_invalid_address_exit_2(capsys):
    assert main(["--mint", "bad-address"]) == 2


def test_cli_watch_exit_0(capsys):
    scanner = ScannerAgent(StubWatch([_candidate(MINT_ACTIVE, "LEAD", 122.0, 303552)]))
    assert main(["--watch", "--limit", "5"], scanner=scanner) == 0
    assert "LEAD" in capsys.readouterr().out


def test_cli_no_args_exit_2(capsys):
    assert main([]) == 2
