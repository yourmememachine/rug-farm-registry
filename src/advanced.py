"""Advanced detection — v2.5.

What the open repos do not have (GitHub survey, 2026-11-13): the best
detection lives in closed services; open repos mostly wrap
RugCheck / GMGN / Helius, same-block bundler checks are evaded by
pre-funding + delayed buys, and nobody open-sources a
stealth/unbundled-cluster detector, a true exit simulation at your
size, a wash-trading / fake-holder score, or an outcome-tracked
scorer. This module builds those four, from caller-supplied data
only — it never invents a funder, a holder, liquidity, or an outcome.

    StealthClusterDetector — pre-funded wallets, delayed buys, and
        shared funding sources across 2+ hops, which same-block
        bundler checks miss.
    ExitSimulator — what you would actually receive selling a
        given size, with price impact and slippage, from real
        liquidity. "Unreliable" when liquidity is missing.
    WashScore — wash-trading / fake-holder score from holder
        concentration (HHI-style), top-1 / top-10 share, and a
        volume-vs-holders anomaly. "Insufficient data" when
        holder data is missing.
    OutcomeTracker — record a verdict + the coin's later outcome
        and compute accuracy (precision / recall) over time.
        Stores to a caller-given JSON file, never the registry.

    python -m src.advanced --exit --mc <n> --liquidity <n> --size <usd>
    python -m src.advanced --wash --holders <n> --top10 0.4,0.2,...
    python -m src.advanced --cluster --funding <events.json>
    python -m src.advanced --track --verdict flagged --outcome rugged --mint <mint>

Exit codes: 0 = ok, 2 = invalid args.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = 0
EXIT_INVALID = 2

# --- StealthClusterDetector thresholds ---------------------------------
PREFUND_WINDOW_SECONDS = 24 * 60 * 60  # funded >= 24h before launch
DELAYED_BUY_SECONDS = 60 * 60  # bought >= 1h after launch
MIN_CLUSTER_SIZE = 2

# --- WashScore thresholds ----------------------------------------------
WASH_HIGH = 60
WASH_MEDIUM = 30


# ======================================================================
# StealthClusterDetector
# ======================================================================
@dataclass
class FundingEvent:
    """One wallet's funding + buy, all caller-supplied.

    ``funder`` is the wallet that funded this wallet, or None when
    unknown — an unknown funder is never invented. Timestamps are
    epoch seconds; ``launch_at`` is the coin's launch time.
    """

    wallet: str
    funder: str | None = None
    amount_sol: float | None = None
    funded_at: int | None = None
    bought_at: int | None = None
    launch_at: int | None = None

    def is_prefunded(self) -> bool:
        """Funded at least 24h before launch — invisible to same-block checks."""
        if self.funded_at is None or self.launch_at is None:
            return False
        return self.funded_at <= self.launch_at - PREFUND_WINDOW_SECONDS

    def is_delayed_buy(self) -> bool:
        """Bought at least 1h after launch — not a same-block bundle buy."""
        if self.bought_at is None or self.launch_at is None:
            return False
        return self.bought_at - self.launch_at >= DELAYED_BUY_SECONDS


@dataclass
class Cluster:
    members: list[str] = field(default_factory=list)
    funders: list[str] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)


class StealthClusterDetector:
    """Detect stealth clusters that same-block bundler checks miss.

    Wallets are grouped when they share a funder, or when a funder
    is itself a funded wallet in the set (a 2+ hop funding chain).
    A group is reported when it has at least one stealth signal;
    a lone wallet is reported only when it is BOTH pre-funded and
    a delayed buyer — either fact alone is not a cluster.
    """

    def detect(self, events: list[FundingEvent]) -> list[Cluster]:
        if not events:
            return []
        wallets = [e.wallet for e in events]
        by_wallet = {e.wallet: e for e in events}

        # Union-find over wallets: same funder, or funder-is-a-wallet.
        parent = {w: w for w in wallets}

        def find(x: str) -> str:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[rb] = ra

        funder_to_wallets: dict[str, list[str]] = {}
        for e in events:
            if e.funder:
                funder_to_wallets.setdefault(e.funder, []).append(e.wallet)
        for funder, members in funder_to_wallets.items():
            for other in members[1:]:
                union(members[0], other)
            # 2-hop: the funder is itself one of the wallets.
            if funder in by_wallet:
                union(members[0], funder)

        groups: dict[str, list[FundingEvent]] = {}
        for e in events:
            groups.setdefault(find(e.wallet), []).append(e)

        clusters: list[Cluster] = []
        for members in groups.values():
            signals: list[str] = []
            evidence: list[str] = []
            funders = sorted({e.funder for e in members if e.funder})
            if len(funders) == 1 and len(members) >= MIN_CLUSTER_SIZE:
                signals.append("shared_funder")
                evidence.append(
                    f"{len(members)} wallets share funder {funders[0]}."
                )
            elif len(funders) > 1:
                signals.append("shared_funder")
                evidence.append(
                    f"{len(members)} wallets share {len(funders)} funders."
                )
            prefunded = [e for e in members if e.is_prefunded()]
            if prefunded:
                signals.append("prefunded")
                evidence.append(
                    f"{len(prefunded)} wallet(s) funded >= 24h before launch: "
                    + ", ".join(e.wallet for e in prefunded) + "."
                )
            delayed = [e for e in members if e.is_delayed_buy()]
            if delayed:
                signals.append("delayed_buy")
                evidence.append(
                    f"{len(delayed)} wallet(s) bought >= 1h after launch: "
                    + ", ".join(e.wallet for e in delayed) + "."
                )
            # 2-hop: a funder in the group is itself funded by
            # another wallet in the group.
            chained = [
                e
                for e in members
                if e.funder in by_wallet and e.funder != e.wallet
            ]
            if chained:
                signals.append("two_hop")
                evidence.append(
                    "Funding chain spans 2+ hops: "
                    + ", ".join(f"{e.funder} -> {e.wallet}" for e in chained)
                    + "."
                )
            keep = len(members) >= MIN_CLUSTER_SIZE and bool(signals)
            keep = keep or (
                len(members) == 1 and "prefunded" in signals and "delayed_buy" in signals
            )
            if not keep:
                continue
            clusters.append(
                Cluster(
                    members=sorted(e.wallet for e in members),
                    funders=funders,
                    signals=sorted(set(signals)),
                    evidence=evidence,
                )
            )
        clusters.sort(key=lambda c: (-len(c.members), c.members))
        return clusters


def detect_stealth_clusters(events: list[FundingEvent]) -> list[Cluster]:
    """One-shot helper over :class:`StealthClusterDetector`."""
    return StealthClusterDetector().detect(events)


# ======================================================================
# ExitSimulator
# ======================================================================
@dataclass
class ExitResult:
    sell_size_usd: float
    quoted_value_usd: float
    estimated_received_usd: float | None
    price_impact_pct: float | None
    slippage_pct: float | None
    reliable: bool
    reasons: list[str] = field(default_factory=list)


def simulate_exit(
    market_cap_usd: float | None,
    liquidity_usd: float | None,
    sell_size_usd: float,
    volume_24h_usd: float | None = None,
) -> ExitResult:
    """Simulate selling ``sell_size_usd`` at the quoted price.

    Constant-product approximation: the pool's token side is about
    half the reported liquidity, so selling ``s`` into a pool of
    liquidity ``L`` receives ``s * (L/2) / (L/2 + s)``. The gap
    between the quoted value and what you receive is the price
    impact (reported as both impact and slippage — they are the
    same number in this model). Liquidity is never invented: when
    it is missing or <= 0 the result is unreliable and no receive
    amount is given.
    """
    if sell_size_usd is None or sell_size_usd <= 0 or math.isnan(sell_size_usd):
        raise ValueError("sell_size_usd must be a positive number.")
    if market_cap_usd is not None and (market_cap_usd < 0 or math.isnan(market_cap_usd)):
        raise ValueError("market_cap_usd must be non-negative or None.")
    reasons: list[str] = []
    if liquidity_usd is None or liquidity_usd <= 0 or math.isnan(liquidity_usd):
        return ExitResult(
            sell_size_usd=sell_size_usd,
            quoted_value_usd=sell_size_usd,
            estimated_received_usd=None,
            price_impact_pct=None,
            slippage_pct=None,
            reliable=False,
            reasons=["Liquidity is missing/unreported — exit cannot be estimated."],
        )
    pool_side = liquidity_usd / 2.0
    received = sell_size_usd * pool_side / (pool_side + sell_size_usd)
    impact = 100.0 * (1.0 - received / sell_size_usd)
    if impact >= 50.0:
        reasons.append(f"Severe price impact: {impact:.1f}% of the quoted value is lost.")
    elif impact > 10.0:
        reasons.append(f"High price impact: {impact:.1f}%.")
    if volume_24h_usd is not None and volume_24h_usd < 2 * sell_size_usd:
        reasons.append(
            "24h volume is less than 2x your sell size — a single exit may move the market."
        )
    if market_cap_usd is not None and sell_size_usd > 0.1 * market_cap_usd:
        reasons.append("Sell size is over 10% of market cap.")
    return ExitResult(
        sell_size_usd=sell_size_usd,
        quoted_value_usd=sell_size_usd,
        estimated_received_usd=round(received, 2),
        price_impact_pct=round(impact, 2),
        slippage_pct=round(impact, 2),
        reliable=True,
        reasons=reasons,
    )


# ======================================================================
# WashScore
# ======================================================================
@dataclass
class WashResult:
    score: float | None
    level: str  # low | medium | high | insufficient_data
    reasons: list[str] = field(default_factory=list)
    insufficient_data: bool = False


def _normalise_shares(shares: list[float] | None) -> list[float]:
    """Shares as fractions 0-1; values > 1 are treated as percents."""
    out: list[float] = []
    for s in shares or []:
        if s is None or (isinstance(s, float) and math.isnan(s)):
            continue
        out.append(s / 100.0 if s > 1.0 else float(s))
    return [max(0.0, min(1.0, s)) for s in out]


def wash_score(
    holder_count: int | None,
    top10_shares: list[float] | None,
    top1_share: float | None = None,
    volume_24h_usd: float | None = None,
    market_cap_usd: float | None = None,
) -> WashResult:
    """Score wash-trading / fake-holder risk, 0-100.

    Concentration is measured HHI-style (sum of squared top-10
    shares). No holder data at all — no shares and no top-1 —
    returns ``insufficient_data`` with no score, never a guess.
    """
    shares = _normalise_shares(top10_shares)
    top1 = None
    if top1_share is not None and not (isinstance(top1_share, float) and math.isnan(top1_share)):
        top1 = top1_share / 100.0 if top1_share > 1.0 else float(top1_share)
    if not shares and top1 is None:
        return WashResult(
            score=None,
            level="insufficient_data",
            reasons=["No holder data supplied — cannot score wash risk."],
            insufficient_data=True,
        )
    if shares:
        hhi = sum(s * s for s in shares)
        top10_total = sum(shares)
    else:
        hhi = (top1 or 0.0) ** 2
        top10_total = top1 or 0.0
    if top1 is None and shares:
        top1 = max(shares)

    score = 0.0
    reasons: list[str] = []
    if hhi > 0.5:
        score += 40
        reasons.append(f"Extreme concentration (HHI {hhi:.2f} > 0.50).")
    elif hhi > 0.25:
        score += 30
        reasons.append(f"High concentration (HHI {hhi:.2f} > 0.25).")
    elif hhi > 0.10:
        score += 15
        reasons.append(f"Moderate concentration (HHI {hhi:.2f} > 0.10).")
    if top1 is not None:
        if top1 > 0.5:
            score += 25
            reasons.append(f"Top holder owns {top1:.0%} of supply.")
        elif top1 > 0.2:
            score += 15
            reasons.append(f"Top holder owns {top1:.0%} of supply.")
        elif top1 > 0.1:
            score += 5
            reasons.append(f"Top holder owns {top1:.0%} of supply.")
    if top10_total > 0.8:
        score += 20
        reasons.append(f"Top 10 holders own {top10_total:.0%} of supply.")
    elif top10_total > 0.5:
        score += 10
        reasons.append(f"Top 10 holders own {top10_total:.0%} of supply.")
    if (
        holder_count is not None
        and holder_count < 50
        and volume_24h_usd is not None
        and market_cap_usd
        and market_cap_usd > 0
        and volume_24h_usd / market_cap_usd > 5.0
    ):
        score += 15
        reasons.append(
            f"Volume is {volume_24h_usd / market_cap_usd:.1f}x market cap with only "
            f"{holder_count} holders — volume looks washed."
        )
    if holder_count is not None and holder_count < 10:
        score += 10
        reasons.append(f"Only {holder_count} holders.")
    score = min(100.0, score)
    level = "high" if score >= WASH_HIGH else "medium" if score >= WASH_MEDIUM else "low"
    if not reasons:
        reasons.append("No wash signals in the supplied holder data.")
    return WashResult(score=score, level=level, reasons=reasons)


# ======================================================================
# OutcomeTracker
# ======================================================================
VERDICTS = ("flagged", "not_flagged", "under_review")
OUTCOMES = ("rugged", "survived", "unknown")


@dataclass
class TrackedOutcome:
    mint: str
    verdict: str
    outcome: str
    collapse_pct: float | None = None


def _refuse_registry_path(path: Path) -> None:
    if path.name == "registry.json":
        raise ValueError("Refusing to write to registry.json — use a tracker file.")


def load_outcomes(path: str | Path) -> list[dict]:
    """Load tracked outcomes from a JSON file; missing file -> []."""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Outcome file {p} is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"Outcome file {p} must hold a JSON list.")
    return data


def save_outcomes(path: str | Path, records: list[dict]) -> None:
    p = Path(path)
    _refuse_registry_path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(records, indent=2), encoding="utf-8")


def record_outcome(
    path: str | Path,
    mint: str,
    verdict: str,
    outcome: str,
    collapse_pct: float | None = None,
) -> dict:
    """Append one verdict + later outcome. Outcomes are only ever
    caller-supplied — nothing here invents whether a coin rugged."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {', '.join(VERDICTS)}.")
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {', '.join(OUTCOMES)}.")
    if not mint:
        raise ValueError("mint must not be empty.")
    records = load_outcomes(path)
    entry = {
        "mint": mint,
        "verdict": verdict,
        "outcome": outcome,
        "collapse_pct": collapse_pct,
    }
    records.append(entry)
    save_outcomes(path, records)
    return entry


def outcome_accuracy(records: list[dict]) -> dict:
    """Precision / recall / accuracy over tracked outcomes.

    Flagged + rugged = true positive; flagged + survived = false
    positive; not flagged + rugged = false negative; not flagged +
    survived = true negative. Under-review and unknown outcomes
    are counted in ``total`` but not in the accuracy maths, and
    every rate is None when its denominator is 0.
    """
    known = [r for r in records if r.get("outcome") in ("rugged", "survived")]
    tp = sum(1 for r in known if r.get("verdict") == "flagged" and r.get("outcome") == "rugged")
    fp = sum(1 for r in known if r.get("verdict") == "flagged" and r.get("outcome") == "survived")
    fn = sum(
        1
        for r in known
        if r.get("verdict") == "not_flagged" and r.get("outcome") == "rugged"
    )
    tn = sum(
        1
        for r in known
        if r.get("verdict") == "not_flagged" and r.get("outcome") == "survived"
    )
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    accuracy = (tp + tn) / len(known) if known else None
    return {
        "total": len(records),
        "tracked": len(known),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "precision": precision,
        "recall": recall,
        "accuracy": accuracy,
    }


# ======================================================================
# CLI
# ======================================================================
def _join_negative_values(argv: list[str]) -> list[str]:
    """Rewrite '--opt -12.3' as '--opt=-12.3' for numeric options."""
    numeric_opts = {"--mc", "--liquidity", "--size", "--volume", "--change"}
    out: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in numeric_opts and i + 1 < len(argv):
            nxt = argv[i + 1]
            try:
                float(nxt.split(",")[0])
            except ValueError:
                out.append(token)
                i += 1
                continue
            if nxt.startswith("-"):
                out.append(f"{token}={nxt}")
                i += 2
                continue
        out.append(token)
        i += 1
    return out


def _parse_shares(text: str | None) -> list[float]:
    if not text:
        return []
    try:
        return [float(p) for p in text.split(",") if p.strip()]
    except ValueError:
        raise ValueError(f"--top10 must be comma-separated numbers, got: {text!r}")


def _load_funding(path: str) -> list[FundingEvent]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ValueError(f"Cannot read funding file {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("Funding file must hold a JSON list of events.")
    events = []
    for item in data:
        if not isinstance(item, dict) or not item.get("wallet"):
            raise ValueError("Each funding event needs a 'wallet'.")
        events.append(FundingEvent(**{k: item.get(k) for k in FundingEvent.__dataclass_fields__}))
    return events


def main(argv: list[str] | None = None) -> int:
    """Demo CLI.

    ``python -m src.advanced --exit --mc 1000000 --liquidity 50000 --size 1000``
    ``python -m src.advanced --wash --holders 40 --top10 0.4,0.2,0.1``
    ``python -m src.advanced --cluster --funding events.json``
    ``python -m src.advanced --track --verdict flagged --outcome rugged --mint <mint>``
    Exit 0 = ok, 2 = invalid args.
    """
    parser = argparse.ArgumentParser(
        description="Rug-Farm advanced detection: stealth clusters, exit simulation, wash score, outcome tracking."
    )
    parser.add_argument("--exit", action="store_true", help="Simulate an exit")
    parser.add_argument("--wash", action="store_true", help="Score wash / fake-holder risk")
    parser.add_argument("--cluster", action="store_true", help="Detect stealth clusters")
    parser.add_argument("--track", action="store_true", help="Record a verdict + outcome")
    parser.add_argument("--mc", type=float, default=None, help="Market cap in USD")
    parser.add_argument("--liquidity", type=float, default=None, help="Liquidity in USD")
    parser.add_argument("--size", type=float, default=None, help="Sell size in USD")
    parser.add_argument("--volume", type=float, default=None, help="24h volume in USD")
    parser.add_argument("--holders", type=int, default=None, help="Holder count")
    parser.add_argument("--top10", type=str, default=None, help="Comma-separated top-10 holder shares (0-1 or %)")
    parser.add_argument("--top1", type=float, default=None, help="Top holder share (0-1 or %)")
    parser.add_argument("--funding", type=str, default=None, help="JSON file of funding events")
    parser.add_argument("--verdict", type=str, default=None, help="Verdict: flagged / not_flagged / under_review")
    parser.add_argument("--outcome", type=str, default=None, help="Outcome: rugged / survived / unknown")
    parser.add_argument("--mint", type=str, default=None, help="Token mint")
    parser.add_argument("--collapse", type=float, default=None, help="Collapse % for the tracked outcome")
    parser.add_argument("--file", type=str, default=None, help="Outcome tracker JSON file")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    argv = _join_negative_values(list(argv) if argv is not None else sys.argv[1:])
    args = parser.parse_args(argv)

    modes = [args.exit, args.wash, args.cluster, args.track]
    if sum(modes) != 1:
        print("ERROR: pick exactly one mode: --exit, --wash, --cluster or --track.", file=sys.stderr)
        return EXIT_INVALID

    try:
        if args.exit:
            if args.size is None:
                print("ERROR: --exit needs --size (USD).", file=sys.stderr)
                return EXIT_INVALID
            result = simulate_exit(args.mc, args.liquidity, args.size, args.volume)
            payload = {
                "sell_size_usd": result.sell_size_usd,
                "quoted_value_usd": result.quoted_value_usd,
                "estimated_received_usd": result.estimated_received_usd,
                "price_impact_pct": result.price_impact_pct,
                "slippage_pct": result.slippage_pct,
                "reliable": result.reliable,
                "reasons": result.reasons,
            }
            if args.json:
                print(json.dumps(payload, indent=2))
                return EXIT_OK
            if not result.reliable:
                print("Exit: unreliable — " + "; ".join(result.reasons))
                return EXIT_OK
            print(f"Sell ${result.sell_size_usd:,.2f} (quoted) -> receive ~${result.estimated_received_usd:,.2f}")
            print(f"Price impact: {result.price_impact_pct:.2f}%  Slippage: {result.slippage_pct:.2f}%")
            for reason in result.reasons:
                print(f"  - {reason}")
            return EXIT_OK

        if args.wash:
            try:
                shares = _parse_shares(args.top10)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return EXIT_INVALID
            result = wash_score(args.holders, shares, args.top1, args.volume, args.mc)
            payload = {
                "score": result.score,
                "level": result.level,
                "reasons": result.reasons,
                "insufficient_data": result.insufficient_data,
            }
            if args.json:
                print(json.dumps(payload, indent=2))
                return EXIT_OK
            if result.insufficient_data:
                print("Wash score: insufficient data — " + "; ".join(result.reasons))
            else:
                print(f"Wash score: {result.score:.0f}/100 ({result.level})")
                for reason in result.reasons:
                    print(f"  - {reason}")
            return EXIT_OK

        if args.cluster:
            if not args.funding:
                print("ERROR: --cluster needs --funding <events.json>.", file=sys.stderr)
                return EXIT_INVALID
            try:
                events = _load_funding(args.funding)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return EXIT_INVALID
            clusters = detect_stealth_clusters(events)
            payload = [
                {
                    "members": c.members,
                    "funders": c.funders,
                    "signals": c.signals,
                    "evidence": c.evidence,
                }
                for c in clusters
            ]
            if args.json:
                print(json.dumps(payload, indent=2))
                return EXIT_OK
            if not clusters:
                print("No stealth clusters found in the supplied funding data.")
                return EXIT_OK
            for c in clusters:
                print(f"Cluster ({len(c.members)} wallets, signals: {', '.join(c.signals)}):")
                for ev in c.evidence:
                    print(f"  - {ev}")
            return EXIT_OK

        # --track
        if not args.verdict or not args.outcome or not args.mint:
            print("ERROR: --track needs --verdict, --outcome and --mint.", file=sys.stderr)
            return EXIT_INVALID
        path = args.file or str(Path(__file__).resolve().parent.parent / "data" / "outcomes.json")
        try:
            entry = record_outcome(path, args.mint, args.verdict, args.outcome, args.collapse)
            stats = outcome_accuracy(load_outcomes(path))
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return EXIT_INVALID
        if args.json:
            print(json.dumps({"recorded": entry, "accuracy": stats}, indent=2))
            return EXIT_OK
        print(f"Recorded: {args.mint} — {args.verdict} / {args.outcome}")
        print(f"Tracked: {stats['tracked']} known outcomes, precision {stats['precision']}, recall {stats['recall']}")
        return EXIT_OK
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
