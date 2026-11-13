"""Tests for src/kol_audit.py — synthetic data only, no network."""

import json

import httpx
import pytest

from src.kol_audit import (
    EXIT_EXAGGERATED,
    EXIT_INVALID,
    EXIT_OK,
    HttpClient,
    NoDataClient,
    PnLAuditor,
    StaticClient,
    TradeRecord,
    audit_report,
    is_valid_wallet,
    main,
)

WALLET = "11111111111111111111111111111111"  # System Program, base58
EVM_WALLET = "0x" + "ab" * 20


def _trades(profits):
    return [
        TradeRecord(mint=f"mint{i}", symbol=f"T{i}", pnl_usd=p, won=p > 0)
        for i, p in enumerate(profits)
    ]


def _auditor(profits, profit=None):
    return PnLAuditor(client=StaticClient(_trades(profits), verified_profit=profit))


# --- Validation ---------------------------------------------------------

def test_valid_wallets():
    assert is_valid_wallet(WALLET)
    assert is_valid_wallet(EVM_WALLET)


def test_invalid_wallets():
    assert not is_valid_wallet("")
    assert not is_valid_wallet("not-a-wallet")
    assert not is_valid_wallet("0x1234")
    assert not is_valid_wallet("0OIl" * 10)


def test_audit_invalid_wallet_raises():
    with pytest.raises(ValueError):
        PnLAuditor().audit("nope", claimed_profit=100.0)


# --- Verdicts -----------------------------------------------------------

def test_consistent_exact_match():
    result = _auditor([600, 400]).audit(WALLET, claimed_profit=1000.0)
    assert result.verdict == "consistent"
    assert result.exit_code == EXIT_OK


def test_consistent_within_10_percent():
    result = _auditor([1000]).audit(WALLET, claimed_profit=1090.0)
    assert result.verdict == "consistent"


def test_exaggerated_more_than_2x():
    result = _auditor([1000]).audit(WALLET, claimed_profit=3000.0)
    assert result.verdict == "exaggerated"
    assert result.exit_code == EXIT_EXAGGERATED
    assert any("2x" in r for r in result.reasons)


def test_exaggerated_claimed_profit_on_verified_loss():
    result = _auditor([-500]).audit(WALLET, claimed_profit=500.0)
    assert result.verdict == "exaggerated"


def test_consistent_claimed_loss_matches_verified_loss():
    result = _auditor([-500]).audit(WALLET, claimed_profit=None)
    assert result.verdict == "unverifiable"  # no claim to check
    result = PnLAuditor(client=StaticClient(_trades([-500]))).audit(
        WALLET, claimed_profit=0.0)
    assert result.verdict == "consistent"


def test_unverifiable_no_data():
    result = PnLAuditor().audit(WALLET, claimed_profit=10000.0)
    assert result.verdict == "unverifiable"
    assert result.exit_code == EXIT_OK  # never accuse on no data
    assert "unverified" in " ".join(result.reasons).lower()


def test_unverifiable_inconclusive_gap():
    # 1.5x is above 10% but below the 2x threshold: inconclusive.
    result = _auditor([1000]).audit(WALLET, claimed_profit=1500.0)
    assert result.verdict == "unverifiable"
    assert result.exit_code == EXIT_OK


def test_unverifiable_no_claim():
    result = _auditor([1000]).audit(WALLET)
    assert result.verdict == "unverifiable"


def test_win_rate_note_present():
    result = _auditor([100, 100, 100, 100, -50]).audit(
        WALLET, claimed_profit=350.0, claimed_win_rate=80.0)
    assert result.verdict == "consistent"
    assert any("win rate" in r.lower() for r in result.reasons)


def test_static_client_computes_profit_from_trades():
    data = StaticClient(_trades([200, -50])).fetch(WALLET)
    assert data.verified_profit == 150
    assert data.verified_trades == 2
    assert data.verified_win_rate == 50.0


# --- Report -------------------------------------------------------------

def test_report_claimed_vs_verified_table():
    result = _auditor([1000]).audit(WALLET, claimed_profit=3000.0,
                                    claimed_win_rate=90.0)
    report = audit_report(result)
    assert "exaggerated" in report
    assert "$3,000.00" in report
    assert "$1,000.00" in report
    assert "90.0%" in report


def test_report_links_and_unverified_marking():
    result = PnLAuditor().audit(WALLET, claimed_profit=100.0)
    report = audit_report(result)
    assert "solscan.io/account/" + WALLET in report
    assert "dexscreener.com/search" in report
    assert "unverified" in report.lower()
    assert "not an accusation" in report.lower()


def test_report_evm_wallet_links():
    result = PnLAuditor().audit(EVM_WALLET, claimed_profit=10.0)
    assert "etherscan.io/address/" + EVM_WALLET in audit_report(result)


# --- HttpClient ---------------------------------------------------------

def test_http_client_parses_verified_trades():
    def handler(request):
        assert request.url.params["wallet"] == WALLET
        return httpx.Response(200, json={
            "trades": [
                {"mint": "m1", "symbol": "A", "pnl_usd": 800, "won": True},
                {"mint": "m2", "symbol": "B", "pnl_usd": 200, "won": True},
            ],
            "profit": 1000,
        })

    client = HttpClient("https://example.invalid/audit",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = PnLAuditor(client=client).audit(WALLET, claimed_profit=1000.0)
    assert result.verdict == "consistent"
    assert result.verified.verified_profit == 1000


def test_http_client_failure_is_unverified_not_invented():
    def handler(request):
        return httpx.Response(500, json={})

    client = HttpClient("https://example.invalid/audit",
                        client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = PnLAuditor(client=client).audit(WALLET, claimed_profit=100.0)
    assert result.verdict == "unverifiable"
    assert result.verified.verified_profit is None


# --- CLI ----------------------------------------------------------------

def test_cli_unverifiable_exit_0_no_data(capsys):
    assert main(["--wallet", WALLET, "--claimed-profit", "100"]) == EXIT_OK


def test_cli_exaggerated_exit_1_with_data_file(tmp_path, capsys):
    data = tmp_path / "data.json"
    data.write_text(json.dumps({
        "trades": [{"mint": "m1", "pnl_usd": 1000, "won": True}],
        "profit": 1000,
    }))
    assert main(["--wallet", WALLET, "--claimed-profit", "5000",
                 "--data-file", str(data)]) == EXIT_EXAGGERATED


def test_cli_consistent_exit_0_with_data_file(tmp_path, capsys):
    data = tmp_path / "data.json"
    data.write_text(json.dumps({
        "trades": [{"mint": "m1", "pnl_usd": 1000, "won": True}],
        "profit": 1000,
    }))
    assert main(["--wallet", WALLET, "--claimed-profit", "1000",
                 "--data-file", str(data)]) == EXIT_OK


def test_cli_invalid_wallet_exit_2(capsys):
    assert main(["--wallet", "nope", "--claimed-profit", "100"]) == EXIT_INVALID


def test_cli_invalid_winrate_exit_2(capsys):
    assert main(["--wallet", WALLET, "--claimed-profit", "100",
                 "--claimed-winrate", "150"]) == EXIT_INVALID


def test_cli_bad_data_file_exit_2(tmp_path, capsys):
    assert main(["--wallet", WALLET, "--claimed-profit", "100",
                 "--data-file", str(tmp_path / "missing.json")]) == EXIT_INVALID


def test_cli_json_output_has_verdict(tmp_path, capsys):
    data = tmp_path / "data.json"
    data.write_text(json.dumps({
        "trades": [{"mint": "m1", "pnl_usd": 100, "won": True}],
        "profit": 100,
    }))
    assert main(["--wallet", WALLET, "--claimed-profit", "100",
                 "--data-file", str(data), "--json"]) == EXIT_OK
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "consistent"
    assert out["verified_profit"] == 100
