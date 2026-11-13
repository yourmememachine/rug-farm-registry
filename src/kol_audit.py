"""KOL / Fake-PnL Auditor — v2.4.

Check a wallet's claimed PnL against real on-chain activity. The
auditor never invents verified numbers: every figure it cannot
verify from the injected data client is marked "unverified", and
it never accuses fraud on unverifiable data.

    PnLAuditor   — claimed vs verified comparison, verdict
    VerifiedData — what the data client could actually verify
    audit_report — short markdown audit report (claimed vs verified)

Verdicts:
    consistent    — claimed profit within 10% of verified profit
                    (and claimed win rate within 10 points, when
                    both are known)
    exaggerated   — claimed profit is more than 2x verified profit
    unverifiable  — no verified data to check the claim against,
                    or the gap is inconclusive (between 10% and 2x)

    python -m src.kol_audit --wallet <wallet> \\
        --claimed-profit <usd> --claimed-winrate <pct>
        [--claimed-trades N] [--data-file <path>] [--json]

Exit codes: 0 = consistent / unverifiable, 1 = exaggerated,
2 = invalid address / args.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import httpx

EXIT_OK = 0
EXIT_EXAGGERATED = 1
EXIT_INVALID = 2

VERDICTS = ("consistent", "exaggerated", "unverifiable")

_SOLANA_BASE58 = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")

DISCLAIMER = (
    "An 'unverifiable' verdict is not an accusation: it means "
    "there is no verified data to check the claim against."
)


def is_valid_wallet(address: str) -> bool:
    """Accept a Solana base58 wallet or an EVM 0x wallet."""
    if not isinstance(address, str) or not address:
        return False
    return bool(_SOLANA_BASE58.match(address) or _EVM_ADDRESS.match(address))


def solscan_link(wallet: str) -> str:
    if wallet.startswith("0x"):
        return f"https://etherscan.io/address/{wallet}"
    return f"https://solscan.io/account/{wallet}"


def dexscreener_link(wallet: str) -> str:
    return f"https://dexscreener.com/search?q={wallet}"


@dataclass
class TradeRecord:
    """One verified trade. All numbers are caller-supplied real data."""

    mint: str
    symbol: str = ""
    buy_usd: float | None = None
    sell_usd: float | None = None
    pnl_usd: float | None = None
    won: bool | None = None


@dataclass
class VerifiedData:
    """What the data client could actually verify about a wallet."""

    wallet: str
    trades: list[TradeRecord] = field(default_factory=list)
    verified_profit: float | None = None
    source: str = "injected client"
    unverified_fields: list[str] = field(default_factory=list)

    @property
    def verified_trades(self) -> int:
        return len(self.trades)

    @property
    def verified_wins(self) -> int:
        return sum(1 for t in self.trades if t.won is True)

    @property
    def verified_win_rate(self) -> float | None:
        if not self.trades:
            return None
        known = [t for t in self.trades if t.won is not None]
        if not known:
            return None
        return 100.0 * sum(1 for t in known if t.won) / len(known)


class NoDataClient:
    """Default client: no verified data source is wired up.

    Honest by construction — it returns no trades and marks
    everything unverified instead of guessing numbers.
    """

    def fetch(self, wallet: str) -> VerifiedData:
        return VerifiedData(
            wallet=wallet,
            trades=[],
            verified_profit=None,
            source="no data source wired up",
            unverified_fields=["profit", "trades", "win rate"],
        )


class StaticClient:
    """In-memory client for tests, demos and offline audits."""

    def __init__(self, trades: list[TradeRecord], verified_profit: float | None = None,
                 source: str = "static data"):
        self._trades = list(trades)
        self._profit = verified_profit
        self._source = source

    def fetch(self, wallet: str) -> VerifiedData:
        profit = self._profit
        if profit is None:
            known = [t.pnl_usd for t in self._trades if t.pnl_usd is not None]
            profit = sum(known) if known else None
        unverified: list[str] = []
        if profit is None:
            unverified.append("profit")
        if not self._trades:
            unverified.append("trades")
        if not any(t.won is not None for t in self._trades):
            unverified.append("win rate")
        return VerifiedData(
            wallet=wallet,
            trades=list(self._trades),
            verified_profit=profit,
            source=self._source,
            unverified_fields=unverified,
        )


class HttpClient:
    """Fetch verified trades from a caller-supplied JSON endpoint.

    The endpoint must answer ``GET <base_url>?wallet=<address>``
    with JSON like ``{"trades": [{"mint": ..., "pnl_usd": ...,
    "won": true}], "profit": 123.4}``. Any failure returns empty
    verified data marked unverified — never invented numbers.
    """

    def __init__(self, base_url: str, client: httpx.Client | None = None):
        self.base_url = base_url
        self._client = client

    def fetch(self, wallet: str) -> VerifiedData:
        client = self._client or httpx.Client(timeout=15)
        try:
            resp = client.get(self.base_url, params={"wallet": wallet})
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return VerifiedData(
                wallet=wallet, trades=[], verified_profit=None,
                source=self.base_url,
                unverified_fields=["profit", "trades", "win rate"],
            )
        trades = []
        for raw in payload.get("trades") or []:
            trades.append(TradeRecord(
                mint=str(raw.get("mint", "")),
                symbol=str(raw.get("symbol", "")),
                buy_usd=raw.get("buy_usd"),
                sell_usd=raw.get("sell_usd"),
                pnl_usd=raw.get("pnl_usd"),
                won=raw.get("won"),
            ))
        return StaticClient(
            trades, verified_profit=payload.get("profit"),
            source=self.base_url,
        ).fetch(wallet)


@dataclass
class AuditResult:
    wallet: str
    claimed_profit: float | None
    claimed_win_rate: float | None
    claimed_trades: int | None
    verified: VerifiedData
    verdict: str
    reasons: list[str] = field(default_factory=list)
    exit_code: int = EXIT_OK


class PnLAuditor:
    """Compare claimed PnL against verified on-chain activity."""

    def __init__(self, client=None):
        self.client = client or NoDataClient()

    def audit(self, wallet: str, claimed_profit: float | None = None,
              claimed_win_rate: float | None = None,
              claimed_trades: int | None = None) -> AuditResult:
        if not is_valid_wallet(wallet):
            raise ValueError(f"invalid wallet address: {wallet!r}")
        verified = self.client.fetch(wallet)
        reasons: list[str] = []
        verdict = "unverifiable"

        if verified.verified_profit is None and not verified.trades:
            reasons.append(
                "No verified trade data for this wallet — "
                "claim cannot be checked."
            )
        elif claimed_profit is None:
            reasons.append("No claimed profit was supplied — nothing to check.")
        else:
            vp = verified.verified_profit
            if vp is None:
                reasons.append(
                    "Verified profit could not be computed from the "
                    "verified trades — claim cannot be checked."
                )
            elif vp > 0:
                ratio = claimed_profit / vp if vp else float("inf")
                if abs(claimed_profit - vp) <= 0.10 * vp:
                    verdict = "consistent"
                    reasons.append(
                        f"Claimed profit ${claimed_profit:,.2f} is within "
                        f"10% of verified profit ${vp:,.2f}."
                    )
                elif claimed_profit > 2 * vp:
                    verdict = "exaggerated"
                    reasons.append(
                        f"Claimed profit ${claimed_profit:,.2f} is more "
                        f"than 2x verified profit ${vp:,.2f} "
                        f"({ratio:.1f}x)."
                    )
                elif claimed_profit > vp:
                    reasons.append(
                        f"Claimed profit ${claimed_profit:,.2f} exceeds "
                        f"verified profit ${vp:,.2f} by "
                        f"{(ratio - 1) * 100:.0f}%, but stays below the "
                        f"2x exaggeration threshold — inconclusive."
                    )
                else:
                    verdict = "consistent"
                    reasons.append(
                        f"Claimed profit ${claimed_profit:,.2f} is at or "
                        f"below verified profit ${vp:,.2f}."
                    )
            else:  # verified profit <= 0
                if claimed_profit > 0:
                    verdict = "exaggerated"
                    reasons.append(
                        f"Claimed profit ${claimed_profit:,.2f} while "
                        f"verified profit is ${vp:,.2f} (a loss or zero)."
                    )
                else:
                    verdict = "consistent"
                    reasons.append(
                        f"Claimed profit ${claimed_profit:,.2f} matches a "
                        f"verified loss/zero of ${vp:,.2f}."
                    )

        # Win rate is supporting evidence, never the sole trigger.
        vwr = verified.verified_win_rate
        if (claimed_win_rate is not None and vwr is not None
                and verified.verified_trades >= 5):
            if abs(claimed_win_rate - vwr) <= 10:
                reasons.append(
                    f"Claimed win rate {claimed_win_rate:.0f}% is within "
                    f"10 points of verified {vwr:.0f}%."
                )
            else:
                reasons.append(
                    f"Claimed win rate {claimed_win_rate:.0f}% differs "
                    f"from verified {vwr:.0f}% by "
                    f"{abs(claimed_win_rate - vwr):.0f} points."
                )

        if claimed_trades is not None and verified.trades:
            if claimed_trades != verified.verified_trades:
                reasons.append(
                    f"Claimed {claimed_trades} trades vs "
                    f"{verified.verified_trades} verified trades."
                )

        if verified.unverified_fields:
            reasons.append(
                "Unverified: " + ", ".join(verified.unverified_fields) + "."
            )

        return AuditResult(
            wallet=wallet,
            claimed_profit=claimed_profit,
            claimed_win_rate=claimed_win_rate,
            claimed_trades=claimed_trades,
            verified=verified,
            verdict=verdict,
            reasons=reasons,
            exit_code=EXIT_EXAGGERATED if verdict == "exaggerated" else EXIT_OK,
        )


def _fmt_usd(value: float | None) -> str:
    if value is None:
        return "unverified"
    return f"${value:,.2f}"


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "unverified"
    return f"{value:.1f}%"


def audit_report(result: AuditResult) -> str:
    """Short markdown audit report: claimed vs verified table."""
    v = result.verified
    lines = [f"# KOL PnL audit — {result.wallet}", ""]
    lines.append(f"**Verdict: {result.verdict}**")
    lines.append("")
    lines.append("| Metric | Claimed | Verified |")
    lines.append("| --- | --- | --- |")
    lines.append(
        f"| Profit (USD) | {_fmt_usd(result.claimed_profit)} | "
        f"{_fmt_usd(v.verified_profit)} |"
    )
    lines.append(
        f"| Win rate | {_fmt_pct(result.claimed_win_rate)} | "
        f"{_fmt_pct(v.verified_win_rate)} |"
    )
    lines.append(
        f"| Trades | "
        f"{result.claimed_trades if result.claimed_trades is not None else 'unverified'} | "
        f"{v.verified_trades if v.trades else 'unverified'} |"
    )
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    lines.append(f"- Solscan: {solscan_link(result.wallet)}")
    lines.append(f"- DexScreener: {dexscreener_link(result.wallet)}")
    lines.append(f"- Data source: {v.source}")
    if v.unverified_fields:
        lines.append(f"- Unverified: {', '.join(v.unverified_fields)}")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    for reason in result.reasons:
        lines.append(f"- {reason}")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _load_data_file(path: str):
    raw = json.loads(Path(path).read_text())
    trades = [
        TradeRecord(
            mint=str(t.get("mint", "")),
            symbol=str(t.get("symbol", "")),
            buy_usd=t.get("buy_usd"),
            sell_usd=t.get("sell_usd"),
            pnl_usd=t.get("pnl_usd"),
            won=t.get("won"),
        )
        for t in (raw.get("trades") or [])
    ]
    return StaticClient(trades, verified_profit=raw.get("profit"),
                        source=f"file: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="src.kol_audit",
        description="Audit a wallet's claimed PnL against verified data.",
    )
    parser.add_argument("--wallet", required=True)
    parser.add_argument("--claimed-profit", type=float, default=None)
    parser.add_argument("--claimed-winrate", type=float, default=None)
    parser.add_argument("--claimed-trades", type=int, default=None)
    parser.add_argument("--data-file", default=None,
                        help="JSON file with verified trades "
                             "({\"trades\": [...], \"profit\": ...})")
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not is_valid_wallet(args.wallet):
        print(f"ERROR: invalid wallet address: {args.wallet!r}", file=sys.stderr)
        return EXIT_INVALID
    if args.claimed_profit is not None and args.claimed_profit < 0:
        print("ERROR: --claimed-profit must be >= 0", file=sys.stderr)
        return EXIT_INVALID
    if args.claimed_winrate is not None and not 0 <= args.claimed_winrate <= 100:
        print("ERROR: --claimed-winrate must be 0-100", file=sys.stderr)
        return EXIT_INVALID
    try:
        client = _load_data_file(args.data_file) if args.data_file else None
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read --data-file: {exc}", file=sys.stderr)
        return EXIT_INVALID

    result = PnLAuditor(client=client).audit(
        args.wallet,
        claimed_profit=args.claimed_profit,
        claimed_win_rate=args.claimed_winrate,
        claimed_trades=args.claimed_trades,
    )
    if args.json:
        print(json.dumps({
            "wallet": result.wallet,
            "verdict": result.verdict,
            "claimed_profit": result.claimed_profit,
            "verified_profit": result.verified.verified_profit,
            "claimed_win_rate": result.claimed_win_rate,
            "verified_win_rate": result.verified.verified_win_rate,
            "unverified_fields": result.verified.unverified_fields,
            "reasons": result.reasons,
        }, indent=2))
    else:
        print(audit_report(result))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
