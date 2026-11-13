"""Real-time watcher daemon (v2.14 "Pro").

The registry used to check new coins once a day. This module
watches continuously: a polling loop (default every 60s) that
fetches new creations, labels each one, and alerts on verified
flags — the same day, not the next morning.

Two modes, stated in the log on every cycle:
- **Helius mode** (``HELIUS_API_KEY`` set): new creations come
  from the pump.fun program via RPC, outcomes from DexScreener,
  and the dev wallet is known — so the full verdict path runs.
- **DexScreener-only mode** (no key): creations come from the
  latest DexScreener boosts, no dev wallet is known, and the
  watcher only produces watch labels — never a rug verdict.

Verdicts reuse the existing path, not a new one: each new mint is
labelled by ``src.labeller`` on its outcome evidence, and the
flag decision is ``src.agents.VerdictAgent`` — the >= 2 rugged
mints + evidence rule. The watcher never flags on its own, never
invents a wallet, and never re-alerts a mint it has already seen:
a mint in the ``mints`` table is skipped, and an alert is written
at most once per (mint, verdict) in the ``alerts`` table.

Alerts go to the SQLite database and to
``data/alerts/live-YYYY-MM-DD.md`` (a separate file from the
daily scan's ``YYYY-MM-DD.md`` — the watcher appends nothing to
the daily scan's file and the daily scan never touches the live
file). The watcher never mutates ``data/registry.json`` — adding
a flagged wallet to the registry stays the daily scan's job.

The loop stops cleanly on SIGINT / SIGTERM (the stop flag is
checked between 1-second sleeps, so it exits within a second of
the signal). ``--once`` runs a single cycle — for tests and CI.

    python -m src.live_watch [--interval 60] [--once] [--db <path>]
    python -m src.live_watch --once --limit 10

Exit codes: 0 = ok, 1 = a cycle failed (only with --once),
2 = invalid args. A failed poll inside the loop is logged and
the loop continues — one bad response must not kill the daemon.
Stdlib + httpx only, no wallet, no funds moved.
"""
from __future__ import annotations

import argparse
import signal
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import db as db_mod
from .agents import VerdictAgent
from .indexer import HeliusIndexer, MintOutcome
from .labeller import MintEvent, label_outcome
from .registry import (
    MIN_RUGGED_MINTS_TO_FLAG,
    get_wallet,
    is_expired,
    is_valid_solana_address,
    load_registry,
    rugged_mint_count,
)

EXIT_OK = 0
EXIT_CYCLE_FAILED = 1
EXIT_INVALID = 2

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "rugfarm.db"
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "registry.json"
DEFAULT_ALERTS_DIR = PROJECT_ROOT / "data" / "alerts"
DEFAULT_INTERVAL = 60
DEFAULT_LIMIT = 50


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> date:
    return date.today()


def _outcome_event(outcome: MintOutcome) -> MintEvent:
    return MintEvent(
        peak_market_cap_usd=outcome.peak_market_cap_usd,
        current_market_cap_usd=outcome.current_market_cap_usd,
        lp_removed=outcome.lp_removed,
        dev_sold_pct=outcome.dev_sold_pct,
        time_to_collapse_minutes=outcome.time_to_collapse_minutes,
    )


def _event_from_watch_entry(entry: dict) -> MintEvent:
    """A MintEvent from a DexScreener watch entry (no-key mode).

    Peak is estimated the same way the indexer does it
    (peak ~= current / (1 + 24h%) when 24h is negative); LP
    removal is inferred from liquidity < $1,000; dev sell is
    0.0 — DexScreener does not prove one.
    """
    from .indexer import LP_DRAINED_USD

    current = entry.get("market_cap_usd")
    change = entry.get("change_24h_pct")
    peak = None
    if current is not None and isinstance(change, (int, float)):
        if change < 0:
            peak = current / max(0.01, 1.0 + change / 100.0)
        else:
            peak = current
    liquidity = entry.get("liquidity_usd")
    return MintEvent(
        peak_market_cap_usd=peak,
        current_market_cap_usd=current,
        lp_removed=bool(liquidity is not None and liquidity < LP_DRAINED_USD),
        dev_sold_pct=0.0,
        time_to_collapse_minutes=None,
    )


class LiveWatcher:
    """One watcher: fetch candidates, label them, alert on flags.

    ``indexer`` is a ``HeliusIndexer`` (real or fake in tests);
    ``watch`` is a ``PumpWatch`` (real or fake). ``registry_data``
    is the loaded JSON registry dict. ``log_lines`` collects every
    log line (tests read it; the daemon also prints it).
    """

    def __init__(
        self,
        db_path: str | Path = DEFAULT_DB_PATH,
        registry_path: str | Path = DEFAULT_REGISTRY_PATH,
        alerts_dir: str | Path = DEFAULT_ALERTS_DIR,
        indexer: HeliusIndexer | None = None,
        watch=None,
        registry_data: dict | None = None,
        today: date | None = None,
    ):
        self.db_path = Path(db_path)
        self.registry_path = Path(registry_path)
        self.alerts_dir = Path(alerts_dir)
        self._indexer = indexer
        self._watch = watch
        self._registry_data = registry_data
        self.today = today or _today()
        self.log_lines: list[str] = []
        self._stop = False

    # -- logging / lifecycle -------------------------------------------
    def log(self, message: str) -> None:
        line = f"[{_now_iso()}] {message}"
        self.log_lines.append(line)
        print(line, flush=True)

    def request_stop(self) -> None:
        self._stop = True

    @property
    def stopped(self) -> bool:
        return self._stop

    # -- backends (lazy, so the module stays importable without a key) -
    def _backend_indexer(self) -> HeliusIndexer:
        if self._indexer is not None:
            return self._indexer
        self._indexer = HeliusIndexer()
        return self._indexer

    def _backend_watch(self):
        if self._watch is not None:
            return self._watch
        from .pump_watch import PumpWatch

        self._watch = PumpWatch()
        return self._watch

    def _registry(self) -> dict:
        if self._registry_data is not None:
            return self._registry_data
        if self.registry_path.is_file():
            try:
                return load_registry(self.registry_path)
            except (ValueError, OSError):
                return {"sample_data": True, "entries": []}
        return {"sample_data": True, "entries": []}

    @property
    def helius_mode(self) -> bool:
        return self._backend_indexer().enabled

    # -- one cycle ------------------------------------------------------
    def fetch_candidates(self, limit: int = DEFAULT_LIMIT) -> list[dict]:
        """New creations to check, newest first.

        Helius mode: pump.fun creations with dev wallets.
        DexScreener-only mode: boosted tokens, no dev wallet.
        """
        if self.helius_mode:
            creations = self._backend_indexer().fetch_new_mints(limit=limit)
            return [
                {
                    "mint": c.mint,
                    "dev_wallet": c.dev_wallet,
                    "source": "helius",
                    "created_at": c.timestamp,
                }
                for c in creations
            ]
        entries = self._backend_watch().watch(limit=limit)
        return [
            {
                "mint": e["mint"],
                "dev_wallet": None,
                "source": "dexscreener",
                "watch_entry": e,
            }
            for e in entries
        ]

    def _check_one(
        self, candidate: dict, registry_data: dict
    ) -> dict:
        """Label one candidate; returns its outcome + verdict dict."""
        mint = candidate["mint"]
        dev = candidate.get("dev_wallet")
        if self.helius_mode:
            outcome = self._backend_indexer().fetch_mint_outcome(mint, dev)
            event = _outcome_event(outcome)
            watch_entry = None
        else:
            entry = candidate.get("watch_entry") or {}
            watch_entry = {
                "mint": mint,
                "watch_label": entry.get("watch_label", "unknown"),
                "change_24h_pct": entry.get("change_24h_pct"),
            }
            event = _event_from_watch_entry(entry)
        label = label_outcome(event)
        if dev:
            verdict = VerdictAgent(registry_data, today=self.today).decide(
                dev_wallet=dev, mint=mint, candidate=watch_entry
            )
            # The verdict above reads the registry as it stands. The
            # alert decision must also count THIS mint: a dev with 1
            # prior rugged mint in the registry plus this newly
            # labelled rugged mint has now met the >= 2 rule. The
            # registry itself is not mutated here — adding the wallet
            # stays the daily scan's job — but the alert must not
            # miss the crossing. An expired registry entry counts
            # for nothing: its rugs have lapsed.
            if label == "rugged" and not verdict.get("flagged"):
                entry = get_wallet(registry_data, dev)
                prior = 0
                if entry is not None and not is_expired(entry, self.today):
                    prior = rugged_mint_count(entry)
                total = prior + 1
                if total >= MIN_RUGGED_MINTS_TO_FLAG:
                    verdict = {
                        **verdict,
                        "verdict": "flagged",
                        "flagged": True,
                        "exit_code": 1,
                        "rugged_mint_count": total,
                        "reasons": [
                            f"Flagged serial rugger: {prior} rugged mint(s) "
                            f"in the registry + this newly labelled rugged "
                            f"mint = {total} with on-chain evidence."
                        ],
                    }
                elif total == 1:
                    verdict = {
                        **verdict,
                        "verdict": "under_review",
                        "flagged": False,
                        "rugged_mint_count": total,
                        "reasons": [
                            "Under review: 1 rugged mint — below the "
                            f"{MIN_RUGGED_MINTS_TO_FLAG}-rug threshold to flag."
                        ],
                    }
        else:
            verdict = {
                "verdict": "unknown",
                "flagged": False,
                "rugged_mint_count": 0,
                "reasons": [
                    "No dev wallet known (DexScreener-only mode) — "
                    "a price drop alone is not a rug verdict."
                ],
            }
        return {"mint": mint, "dev_wallet": dev, "label": label, "verdict": verdict}

    def run_once(
        self, limit: int = DEFAULT_LIMIT, conn=None
    ) -> dict:
        """One poll cycle. Returns the cycle report (and logs it)."""
        mode = "helius" if self.helius_mode else "dexscreener-only"
        self.log(f"Poll cycle starting (mode: {mode}).")
        if mode == "dexscreener-only":
            self.log(
                "DexScreener-only mode: no HELIUS_API_KEY — creations "
                "come from boosts, no dev wallets, watch labels only, "
                "never a rug verdict."
            )
        own_conn = conn is None
        conn = conn or db_mod.init_db(self.db_path)
        registry_data = self._registry()
        report = {
            "cycle_at": _now_iso(),
            "mode": mode,
            "candidates": 0,
            "new_mints": 0,
            "skipped_seen": 0,
            "checked": 0,
            "outcomes": {},
            "flagged": [],
            "under_review": [],
            "alerts_written": [],
            "error": None,
        }
        try:
            candidates = self.fetch_candidates(limit=limit)
        except Exception as exc:  # noqa: BLE001 — log, don't crash
            report["error"] = str(exc)
            self.log(f"Poll cycle failed fetching candidates: {exc}")
            if own_conn:
                conn.close()
            return report
        report["candidates"] = len(candidates)
        for candidate in candidates:
            mint = candidate["mint"]
            if not is_valid_solana_address(mint):
                continue
            if db_mod.get_mint(conn, mint) is not None:
                report["skipped_seen"] += 1
                continue
            try:
                checked = self._check_one(candidate, registry_data)
            except Exception as exc:  # noqa: BLE001 — one bad mint, not the cycle
                self.log(f"Skipping {mint}: check failed: {exc}")
                continue
            db_mod.upsert_mint(
                conn,
                mint,
                creator=checked["dev_wallet"],
                created_at=candidate.get("created_at"),
            )
            if checked["dev_wallet"]:
                db_mod.upsert_dev(conn, checked["dev_wallet"])
            db_mod.record_outcome(conn, mint, checked["label"])
            report["new_mints"] += 1
            report["checked"] += 1
            report["outcomes"][mint] = checked["label"]
            verdict = checked["verdict"]
            if checked["label"] == "rugged":
                if verdict.get("flagged"):
                    report["flagged"].append(mint)
                    if db_mod.record_alert(
                        conn, mint, checked["dev_wallet"], "flagged"
                    ):
                        report["alerts_written"].append(mint)
                elif verdict.get("verdict") == "under_review":
                    report["under_review"].append(mint)
        if report["alerts_written"]:
            self.write_live_alerts(conn)
        self.log(
            f"Poll cycle done: {report['candidates']} candidate(s), "
            f"{report['new_mints']} new, {report['skipped_seen']} already "
            f"seen, {len(report['flagged'])} flagged, "
            f"{len(report['under_review'])} under review, "
            f"{len(report['alerts_written'])} alert(s) written."
        )
        if own_conn:
            conn.close()
        return report

    def write_live_alerts(self, conn) -> Path:
        """Regenerate today's live-alert file from the DB alerts.

        The file is ``live-YYYY-MM-DD.md`` — separate from the
        daily scan's file, so neither overwrites the other.
        """
        today_str = self.today.isoformat()
        alerts = [
            a
            for a in db_mod.list_alerts(conn, limit=200)
            if str(a.get("created_at", "")).startswith(today_str)
        ]
        lines = [f"# Live watch alerts — {today_str}", ""]
        if not alerts:
            lines.append("No live alerts today.")
            lines.append("")
        for alert in alerts:
            lines.append(
                f"- {alert['verdict']}: mint `{alert['mint']}`"
                + (f" — dev `{alert['dev']}`" if alert.get("dev") else "")
            )
        lines.append("")
        lines.append(
            "Alerts are verified flags only (≥ 2 rugged mints + "
            "evidence). A single rugged mint is under review, never "
            "flagged. Verify every evidence link yourself before "
            "acting on a flag."
        )
        lines.append("")
        self.alerts_dir.mkdir(parents=True, exist_ok=True)
        path = self.alerts_dir / f"live-{today_str}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    # -- daemon loop ----------------------------------------------------
    def run(
        self, interval: int = DEFAULT_INTERVAL, max_cycles: int | None = None
    ) -> int:
        """Poll forever (or for ``max_cycles`` cycles in tests).

        Returns 0 on a clean stop, 1 when a cycle failed and
        ``max_cycles`` is set (the --once path).
        """
        try:
            signal.signal(signal.SIGINT, lambda *_: self.request_stop())
            signal.signal(signal.SIGTERM, lambda *_: self.request_stop())
        except (ValueError, OSError):
            # Signals can only be set from the main thread; in a
            # test thread the daemon just runs until max_cycles.
            pass
        cycles = 0
        last_error = False
        while not self._stop:
            report = self.run_once()
            last_error = report.get("error") is not None
            cycles += 1
            if max_cycles is not None and cycles >= max_cycles:
                break
            waited = 0
            while waited < interval and not self._stop:
                time.sleep(1)
                waited += 1
        self.log("Live watch stopped.")
        if max_cycles is not None and last_error:
            return EXIT_CYCLE_FAILED
        return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Live watch CLI.

    ``python -m src.live_watch [--interval 60] [--once] [--db <path>]``
    Exit 0 = ok, 1 = a cycle failed (with --once), 2 = invalid args.
    """
    parser = argparse.ArgumentParser(
        description="Rug-Farm live watch: poll for new pump.fun creations and alert on verified flags."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL,
        help=f"Seconds between polls (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--once", action="store_true", help="Run a single poll cycle and exit"
    )
    parser.add_argument(
        "--db", type=str, default=str(DEFAULT_DB_PATH), help="SQLite database path"
    )
    parser.add_argument(
        "--registry",
        type=str,
        default=str(DEFAULT_REGISTRY_PATH),
        help="Registry JSON path",
    )
    parser.add_argument(
        "--alerts-dir",
        type=str,
        default=str(DEFAULT_ALERTS_DIR),
        help="Directory for live alert markdown files",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Max creations per poll (default: 50)",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Stop after this many cycles (testing)",
    )
    args = parser.parse_args(argv)

    if args.interval <= 0:
        print("ERROR: --interval must be a positive integer.", file=sys.stderr)
        return EXIT_INVALID
    if args.limit <= 0:
        print("ERROR: --limit must be a positive integer.", file=sys.stderr)
        return EXIT_INVALID

    watcher = LiveWatcher(
        db_path=args.db,
        registry_path=args.registry,
        alerts_dir=args.alerts_dir,
    )
    if args.once:
        report = watcher.run_once(limit=args.limit)
        if report.get("error") is not None:
            return EXIT_CYCLE_FAILED
        return EXIT_OK
    return watcher.run(interval=args.interval, max_cycles=args.max_cycles)


if __name__ == "__main__":
    raise SystemExit(main())
