"""Tests for the src/check_cli.py pre-trade check.

All wallets/mints used here are the synthetic SAMPLE entries already
shipped in data/registry.json — no real wallet is accused. Exit codes:
1 = flagged, 0 = unknown / watch / expired (not currently flagged),
2 = invalid address.
"""
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

FLAGGED = "SAMPLEDEV11111111111111111111111111111111"
FLAGGED_MINT = "SAMPLEMNT1111111111111111111111111111111"
UNDER_REVIEW = "SAMPLEDEV33333333333333333333333333333333"
EXPIRED = "SAMPLEDEV44444444444444444444444444444444"
UNKNOWN = "UnknownDev" + "1" * 22  # 32 valid base58 chars, no registry entry
DATE = "2026-11-12"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "src.check_cli", *args, "--date", DATE],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_flagged_wallet_exits_1_with_reasons_and_evidence():
    result = _run(FLAGGED)
    assert result.returncode == 1
    assert "Risk level: high" in result.stdout
    assert "Flagged: YES" in result.stdout
    assert "Rugged mint count: 3" in result.stdout
    assert "Flagged serial rugger" in result.stdout
    # Evidence links are printed, not just a bare boolean.
    assert "https://solscan.io/token/SAMPLEMNT1111111111111111111111111111111" in result.stdout
    assert "SAMPLE DATA ONLY" in result.stdout


def test_flagged_wallet_with_mint_option():
    result = _run(FLAGGED, "--mint", FLAGGED_MINT)
    assert result.returncode == 1
    assert f"Mint: {FLAGGED_MINT}" in result.stdout
    assert "Risk level: high" in result.stdout


def test_unknown_wallet_exits_0():
    result = _run(UNKNOWN)
    assert result.returncode == 0
    assert "Risk level: unknown" in result.stdout
    assert "Flagged: no" in result.stdout
    assert "No record for this dev wallet" in result.stdout


def test_under_review_wallet_exits_0_watch():
    result = _run(UNDER_REVIEW)
    assert result.returncode == 0
    assert "Risk level: watch" in result.stdout
    assert "Flagged: no" in result.stdout
    assert "Rugged mint count: 1" in result.stdout
    assert "Under review" in result.stdout


def test_expired_wallet_exits_0_not_flagged():
    result = _run(EXPIRED)
    assert result.returncode == 0
    assert "Risk level: expired" in result.stdout
    assert "Flagged: no" in result.stdout
    assert "Entry expired" in result.stdout


def test_invalid_address_exits_2():
    result = _run("not-a-valid-address!!")
    assert result.returncode == 2
    assert "not a valid Solana address" in result.stderr


def test_invalid_mint_exits_2():
    result = _run(FLAGGED, "--mint", "bad!!")
    assert result.returncode == 2
    assert "not a valid Solana address" in result.stderr
