"""Dev Watchtower — v2.3.

Watch dev wallets and alert only when a watched dev launches a
NEW mint. A price drop alone never alerts, and a mint already seen
never alerts twice. The verdict comes from ``src.registry`` only:
a flagged dev relaunching is urgent, an under-review dev is a
warning, anything else is info.

    Watchlist   — add/remove/list dev wallets (in-memory + JSON
                  file at a caller-given path, never the registry)
    Watchtower  — check watched devs against the registry, using
                  an injected fetcher for recent mints (mockable,
                  no network in tests)
    format_alert — one-line alert per event, with links + timestamp

    python -m src.watchtower --watch <wallet> [--check] [--mint ...]
    python -m src.watchtower --check [--dev <wallet>] [--mint ...]
    python -m src.watchtower --list | --remove <wallet>

Exit codes: 0 = ok / no urgent alert, 1 = urgent alert found,
2 = invalid address/args.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from src import registry as registry_mod

EXIT_OK = 0
EXIT_URGENT = 1
EXIT_INVALID = 2

LEVELS = ("info", "warning", "urgent")


def _default_registry() -> dict:
    """Load data/registry.json, or an empty registry when missing."""
    path = Path(__file__).resolve().parent.parent / "data" / "registry.json"
    if path.is_file():
        try:
            return registry_mod.load_registry(path)
        except (ValueError, OSError):
            return {"sample_data": True, "entries": []}
    return {"sample_data": True, "entries": []}


def _default_watchlist_path() -> Path:
    return Path(__file__).resolve().parent.parent / "data" / "watchlist.json"


def _mint_urls(mint: str) -> dict:
    return {
        "dexscreener": f"https://dexscreener.com/solana/{mint}",
        "solscan": f"https://solscan.io/token/{mint}",
        "pump_fun": f"https://pump.fun/coin/{mint}",
    }


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Watchlist:
    """Dev wallets to watch, kept in-memory and in a JSON file.

    The file is at a caller-given path and is never the registry.
    File format: ``{"wallets": [{"wallet": ..., "added_at": ...,
    "seen_mints": [...]}, ...]}``. ``added_at`` is caller-supplied
    (UTC ISO string) or None — never a fake date.
    """

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._wallets: dict[str, dict] = {}
        if self.path is not None and self.path.is_file():
            self._load_file()

    def _load_file(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        for item in data.get("wallets", []):
            if not isinstance(item, dict):
                continue
            wallet = item.get("wallet")
            if registry_mod.is_valid_solana_address(wallet):
                self._wallets[wallet] = {
                    "wallet": wallet,
                    "added_at": item.get("added_at"),
                    "seen_mints": [
                        m for m in item.get("seen_mints", []) if isinstance(m, str)
                    ],
                }

    def save(self) -> None:
        """Write the watchlist to its file. No-op without a path."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"wallets": self.list()}, f, indent=2, sort_keys=True)
            f.write("\n")

    def add(self, wallet: str, added_at: str | None = None) -> tuple[bool, str]:
        """Add a dev wallet. Returns (added, reason)."""
        if not registry_mod.is_valid_solana_address(wallet):
            return False, f"{wallet} is not a valid Solana address."
        if wallet in self._wallets:
            return False, f"{wallet} is already watched."
        self._wallets[wallet] = {
            "wallet": wallet,
            "added_at": added_at,
            "seen_mints": [],
        }
        return True, f"Watching {wallet}."

    def remove(self, wallet: str) -> tuple[bool, str]:
        """Remove a dev wallet. Returns (removed, reason)."""
        if wallet not in self._wallets:
            return False, f"{wallet} is not watched."
        del self._wallets[wallet]
        return True, f"Stopped watching {wallet}."

    def contains(self, wallet: str) -> bool:
        return wallet in self._wallets

    def list(self) -> list[dict]:
        return [dict(self._wallets[w]) for w in sorted(self._wallets)]

    def seen_mints(self, wallet: str) -> list[str]:
        entry = self._wallets.get(wallet)
        return list(entry["seen_mints"]) if entry else []

    def mark_seen(self, wallet: str, mint: str) -> None:
        entry = self._wallets.get(wallet)
        if entry is not None and mint not in entry["seen_mints"]:
            entry["seen_mints"].append(mint)


class Watchtower:
    """Alert on NEW mints from watched devs, using the registry rule.

    ``fetcher`` is any object with ``recent_mints(dev_wallet)``
    returning a list of ``{"mint": ..., "symbol": ...}`` dicts — a
    real Helius/DexScreener client in production, a stub in tests.
    With no fetcher, no recent mints are known and no alert fires.
    ``recent_mints`` passed to :meth:`check` overrides the fetcher
    for that call (used by the CLI, where the caller supplies the
    dev's recent mints).
    """

    def __init__(
        self,
        registry_data: dict | None = None,
        fetcher: object | None = None,
        today=None,
    ):
        self.registry_data = registry_data if registry_data is not None else _default_registry()
        self.fetcher = fetcher
        self.today = today

    def _recent_mints(self, dev_wallet: str) -> list[dict]:
        if self.fetcher is None:
            return []
        try:
            mints = self.fetcher.recent_mints(dev_wallet)
        except Exception:
            return []
        return [m for m in mints if isinstance(m, dict) and m.get("mint")]

    def check(
        self,
        dev_wallet: str,
        watchlist: Watchlist | None = None,
        now: str | None = None,
        recent_mints: list[dict] | None = None,
    ) -> list[dict]:
        """Check one dev for new mints. Returns alert dicts.

        Only mints not already in the watchlist's seen set alert,
        and every alerting mint is marked seen so it never alerts
        twice. A dev not in the watchlist is still checked when
        called directly — the watchlist only gates :meth:`check_all`.
        """
        if not registry_mod.is_valid_solana_address(dev_wallet):
            return []
        mints = recent_mints if recent_mints is not None else self._recent_mints(dev_wallet)
        seen = set(watchlist.seen_mints(dev_wallet)) if watchlist is not None else set()
        result = registry_mod.check_wallet(self.registry_data, dev_wallet, today=self.today)
        alerts: list[dict] = []
        for item in mints:
            mint = item.get("mint")
            if not isinstance(mint, str) or mint in seen:
                continue
            if result["risk_level"] == "high":
                level = "urgent"
            elif result["risk_level"] == "watch":
                level = "warning"
            else:
                level = "info"
            alerts.append(
                {
                    "level": level,
                    "dev_wallet": dev_wallet,
                    "mint": mint,
                    "symbol": item.get("symbol"),
                    "risk_level": result["risk_level"],
                    "rugged_mint_count": result["rugged_mint_count"],
                    "reasons": result["reasons"],
                    "links": _mint_urls(mint),
                    "timestamp": now or _utc_now(),
                }
            )
            seen.add(mint)
            if watchlist is not None:
                watchlist.mark_seen(dev_wallet, mint)
        return alerts

    def check_all(
        self,
        watchlist: Watchlist,
        now: str | None = None,
        recent_mints_by_dev: dict | None = None,
    ) -> list[dict]:
        """Check every watched dev. Returns all alert dicts."""
        alerts: list[dict] = []
        for entry in watchlist.list():
            dev = entry["wallet"]
            mints = None
            if recent_mints_by_dev is not None:
                mints = recent_mints_by_dev.get(dev)
            alerts.extend(self.check(dev, watchlist, now=now, recent_mints=mints))
        return alerts


def format_alert(alert: dict) -> str:
    """One-line alert: level, timestamp, dev, mint, verdict, link."""
    symbol = alert.get("symbol") or "?"
    count = alert.get("rugged_mint_count", 0)
    history = f"{count} rugged mint(s) in registry" if count else "no rugged mints in registry"
    return (
        f"[{alert['level'].upper()}] {alert['timestamp']} — dev "
        f"{alert['dev_wallet']} launched {symbol} ({alert['mint']}) — "
        f"{alert['risk_level']} ({history}) — "
        f"{alert['links']['dexscreener']}"
    )


def _load_watchlist(path: str | Path | None) -> Watchlist:
    return Watchlist(path if path is not None else _default_watchlist_path())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="src.watchtower")
    parser.add_argument("--watch", metavar="WALLET", help="add a dev wallet to the watchlist")
    parser.add_argument("--remove", metavar="WALLET", help="remove a dev wallet from the watchlist")
    parser.add_argument("--list", action="store_true", help="list watched dev wallets")
    parser.add_argument("--check", action="store_true", help="check watched devs for new mints")
    parser.add_argument("--dev", metavar="WALLET", help="check only this dev wallet")
    parser.add_argument("--mint", action="append", default=[], metavar="MINT",
                        help="a recent mint of the dev (repeatable; caller-supplied)")
    parser.add_argument("--file", metavar="PATH", help="watchlist JSON file (default data/watchlist.json)")
    parser.add_argument("--json", action="store_true", help="print alerts as JSON")
    parser.add_argument("--now", metavar="TIMESTAMP", help="UTC timestamp for alerts (default: now)")
    args = parser.parse_args(argv)

    if not (args.watch or args.remove or args.list or args.check):
        parser.print_usage(sys.stderr)
        return EXIT_INVALID

    watchlist = _load_watchlist(args.file)
    changed = False

    if args.watch:
        ok, reason = watchlist.add(args.watch, added_at=args.now)
        print(reason)
        if not ok:
            return EXIT_INVALID
        changed = True
    if args.remove:
        ok, reason = watchlist.remove(args.remove)
        print(reason)
        if not ok:
            return EXIT_INVALID
        changed = True
    if args.list:
        for entry in watchlist.list():
            print(f"{entry['wallet']} (seen {len(entry['seen_mints'])} mint(s))")
    if args.check:
        devs = [args.dev] if args.dev else [e["wallet"] for e in watchlist.list()]
        if args.dev and not registry_mod.is_valid_solana_address(args.dev):
            print(f"{args.dev} is not a valid Solana address.", file=sys.stderr)
            return EXIT_INVALID
        if args.dev and args.dev not in [e["wallet"] for e in watchlist.list()]:
            # Check-on-demand: add the dev to the in-memory list only.
            watchlist.add(args.dev, added_at=args.now)
            changed = True
        tower = Watchtower(today=None)
        recent = [{"mint": m} for m in args.mint]
        alerts: list[dict] = []
        for dev in devs:
            alerts.extend(tower.check(dev, watchlist, now=args.now, recent_mints=recent))
        if args.json:
            print(json.dumps(alerts, indent=2))
        else:
            for alert in alerts:
                print(format_alert(alert))
            if not alerts:
                print("No new mints from watched devs.")
        if changed:
            watchlist.save()
        return EXIT_URGENT if any(a["level"] == "urgent" for a in alerts) else EXIT_OK
    if changed:
        watchlist.save()
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
