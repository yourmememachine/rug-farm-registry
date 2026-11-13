"""Creativity agents for rug-check findings — v1.8.

Four small, deterministic, stdlib-only agents that present
rug-check findings in original, human-readable ways. They do
not fetch anything, do not invent data, and never call a token
"rugged" without a flagged verdict — they only re-present
numbers and verdicts the caller already has.

    NameAgent   — original nickname ideas for a watchlist entry
    StoryAgent  — a 2-3 sentence plain-English story of a verdict
    VisualAgent — a coin-card badge spec + ASCII sparkline
    PostAgent   — a ready-to-post X draft (<= 280 chars)

    python -m src.creativity --verdict flagged --symbol XYZ \\
        --mc 1000000 --change -80

Exit codes: 0 = success, 2 = invalid args.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys

EXIT_OK = 0
EXIT_INVALID = 2

REPO_URL = "https://github.com/yourmememachine/rug-farm-registry"
HASHTAGS = "#memes #rug #pumpfun"
VERDICTS = (
    "flagged",
    "under_review",
    "not_flagged",
    "unknown",
    "collapsed",
    "thin_liquidity",
    "active",
)
WATCH_LABELS = ("collapsed", "thin_liquidity", "active", "unknown")

# Small profanity blocklist: generated names never contain these,
# and input words matching them are dropped from nicknames.
PROFANITY = frozenset(
    {
        "damn",
        "hell",
        "shit",
        "fuck",
        "bitch",
        "bastard",
        "ass",
        "crap",
        "piss",
        "dick",
        "cock",
        "slut",
        "whore",
        "wank",
    }
)

_ADJECTIVES = (
    "Sleepy",
    "Moonlit",
    "Wobbly",
    "Brave",
    "Quiet",
    "Cosmic",
    "Peppy",
    "Mellow",
    "Jolly",
    "Dizzy",
    "Sneaky",
    "Sunny",
    "Frosty",
    "Bouncy",
    "Gentle",
    "Zesty",
)

_NOUNS = (
    "Capybara",
    "Penguin",
    "Otter",
    "Fox",
    "Bear",
    "Owl",
    "Koala",
    "Panda",
    "Badger",
    "Mole",
    "Heron",
    "Llama",
    "Seal",
    "Raccoon",
    "Turtle",
    "Moth",
)

_SPARK_CHARS = "▁▂▃▄▅▆▇█"


def _seed_int(text: str) -> int:
    """Deterministic integer from text (sha256, first 8 bytes)."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _clean_word(word: str) -> str:
    word = re.sub(r"[^A-Za-z]", "", word or "")
    return "" if word.lower() in PROFANITY else word


def format_money(value: float | None) -> str:
    """Short money string from a USD value, or 'n/a' when unknown."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:,.0f}"


class NameAgent:
    """Original nickname ideas for a watchlist entry.

    Deterministic from (name, ticker, idea, seed): the same
    inputs always give the same 3 nicknames. Nicknames are
    adjective + noun pairs from fixed word banks, always
    distinct from the input name and from each other, and
    never contain a blocklisted word.
    """

    def nicknames(
        self,
        name: str = "",
        ticker: str = "",
        idea: str = "",
        seed: int = 0,
        count: int = 3,
    ) -> list[str]:
        if count < 1:
            raise ValueError("count must be at least 1")
        base = _seed_int(f"{name}|{ticker}|{idea}|{seed}")
        banned = {w.strip().lower() for w in (name, ticker, idea) if w.strip()}
        banned |= {w.lower() for w in _clean_word(name).split()}
        seen: set[str] = set()
        out: list[str] = []
        step = 0
        while len(out) < count and step < 200:
            roll = base + step * 7919
            adj = _ADJECTIVES[roll % len(_ADJECTIVES)]
            noun = _NOUNS[(roll // len(_ADJECTIVES)) % len(_NOUNS)]
            step += 1
            nick = f"{adj} {noun}"
            if nick.lower() in seen:
                continue
            if nick.lower() in banned:
                continue
            if _clean_word(adj) == "" or _clean_word(noun) == "":
                continue
            seen.add(nick.lower())
            out.append(nick)
        return out


class StoryAgent:
    """A short plain-English story of a verdict (2-3 sentences).

    Never claims a rug without a flagged verdict. For any
    other verdict the story says, in plain words, that the
    coin is not proven a rug.
    """

    def story(self, verdict: dict) -> str:
        symbol = verdict.get("symbol") or verdict.get("name") or "This coin"
        status = verdict.get("verdict", "unknown")
        mc = format_money(verdict.get("market_cap_usd"))
        change = verdict.get("change_24h_pct")
        liq = format_money(verdict.get("liquidity_usd"))
        evidence = verdict.get("evidence_links")
        n_links = len(evidence) if isinstance(evidence, list) else 0

        if change is None:
            move = f"is at a {mc} market cap"
        else:
            move = f"is {'down' if change < 0 else 'up'} {abs(change):.1f}% in 24h to a {mc} market cap"

        if status == "flagged":
            middle = (
                "This dev is flagged as a serial rugger: "
                f"{verdict.get('rugged_mint_count', 2)} rugged mints with "
                "on-chain evidence behind them."
            )
        elif status == "under_review":
            middle = (
                "One rugged mint is linked to this dev, so it is under "
                "review — not proven a rug, and not proven a serial "
                "rugger yet."
            )
        else:
            middle = (
                "This is not proven a rug — no verified rugged mints link "
                "this dev in the registry."
            )

        if n_links:
            watch = (
                f"Watch its liquidity ({liq}) and the dev's next mints, "
                f"and check the {n_links} evidence link(s) before buying."
            )
        else:
            watch = (
                f"Watch its liquidity ({liq}) and the dev's next mints, "
                "and check the evidence links before buying."
            )
        return f"{symbol} {move}. {middle} {watch}"


# label -> (colour name, emoji)
_BADGES = {
    "collapsed": ("red", "🚨"),
    "thin_liquidity": ("amber", "⚠️"),
    "active": ("green", "✅"),
    "flagged": ("red", "🚩"),
    "under_review": ("amber", "👀"),
    "not_flagged": ("green", "✅"),
    "unknown": ("grey", "🔍"),
}


class VisualAgent:
    """A coin-card badge spec plus an ASCII sparkline.

    Colours are names only (red / amber / green / grey) — no
    hex, no invented styling. The sparkline is drawn from the
    caller's own price-change numbers, never invented.
    """

    def badge(self, label: str, symbol: str = "", change: float | None = None) -> dict:
        colour, emoji = _BADGES.get(label, ("grey", "🔍"))
        if change is None:
            summary = f"{symbol or 'Coin'}: {label.replace('_', ' ')}"
        else:
            summary = (
                f"{symbol or 'Coin'}: {label.replace('_', ' ')} "
                f"({change:+.1f}% 24h)"
            )
        return {
            "label": label,
            "colour": colour,
            "emoji": emoji,
            "summary": summary,
        }

    def sparkline(self, changes: list[float]) -> str:
        """ASCII sparkline from price-change numbers, e.g. '▁▃▆█'.

        Returns '(no data)' for an empty list; a flat series
        draws the middle block for every point.
        """
        if not changes:
            return "(no data)"
        lo, hi = min(changes), max(changes)
        if hi == lo:
            return _SPARK_CHARS[3] * len(changes)
        out = []
        for value in changes:
            ratio = (value - lo) / (hi - lo)
            out.append(_SPARK_CHARS[min(7, int(ratio * 8))])
        return "".join(out)


class PostAgent:
    """A ready-to-post X draft, always <= 280 chars.

    Always includes #memes #rug #pumpfun and the repo link.
    Never invents a price or a mint: only the market cap and
    24h change the caller passes in are used, and both are
    omitted when unknown.
    """

    def draft(
        self,
        verdict: str,
        symbol: str = "",
        market_cap_usd: float | None = None,
        change_24h_pct: float | None = None,
    ) -> str:
        symbol = (symbol or "this coin").strip()[:10]
        mc = format_money(market_cap_usd)
        tail = f"{REPO_URL} {HASHTAGS}"
        if verdict == "flagged":
            body = f"🚩 ${symbol} flagged: serial rugger — 2+ rugged mints with evidence. Check before you buy:"
        elif verdict in ("collapsed", "thin_liquidity", "under_review"):
            if change_24h_pct is not None:
                body = (
                    f"👀 ${symbol} {change_24h_pct:+.1f}% 24h — watch only, "
                    "not proven a rug. Check the dev before you buy:"
                )
            else:
                body = (
                    f"👀 ${symbol} — watch only, not proven a rug. "
                    "Check the dev before you buy:"
                )
        else:
            if market_cap_usd is not None and change_24h_pct is not None:
                body = (
                    f"✅ ${symbol} active: {mc} MC, {change_24h_pct:+.1f}% 24h. "
                    "Still check the dev first:"
                )
            elif market_cap_usd is not None:
                body = f"✅ ${symbol} active: {mc} MC. Still check the dev first:"
            else:
                body = f"✅ ${symbol} active. Still check the dev first:"
        post = f"{body} {tail}"
        if len(post) <= 280:
            return post
        # Trim the body (never the link or hashtags) to fit.
        room = 280 - len(tail) - 1
        return f"{body[:room].rstrip()} {tail}"


def creative_check(
    verdict: dict,
    name: str = "",
    ticker: str = "",
    idea: str = "",
    seed: int = 0,
    changes: list[float] | None = None,
) -> dict:
    """Run all four creativity agents over one verdict dict."""
    status = verdict.get("verdict", "unknown")
    symbol = verdict.get("symbol") or ticker or name or ""
    badge_label = status if status in _BADGES else verdict.get("watch_label", "unknown")
    return {
        "nicknames": NameAgent().nicknames(name=name, ticker=ticker, idea=idea, seed=seed),
        "story": StoryAgent().story(verdict),
        "badge": VisualAgent().badge(badge_label, symbol=symbol, change=verdict.get("change_24h_pct")),
        "sparkline": VisualAgent().sparkline(changes or []),
        "post": PostAgent().draft(
            status,
            symbol=symbol,
            market_cap_usd=verdict.get("market_cap_usd"),
            change_24h_pct=verdict.get("change_24h_pct"),
        ),
    }


def _parse_changes(text: str | None) -> list[float]:
    if not text:
        return []
    try:
        return [float(part) for part in text.split(",") if part.strip()]
    except ValueError:
        raise ValueError(f"--changes must be comma-separated numbers, got: {text!r}")


def _join_negative_values(argv: list[str]) -> list[str]:
    """Rewrite '--opt -12.3' as '--opt=-12.3' for numeric options."""
    numeric_opts = {"--mc", "--change", "--liquidity", "--changes"}
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


def main(argv: list[str] | None = None) -> int:
    """Demo CLI.

    ``python -m src.creativity --verdict flagged --symbol XYZ --mc 1000000 --change -80``
    Modes: --story / --post / --badge / --names (default: all).
    Exit 0 = success, 2 = invalid args.
    """
    parser = argparse.ArgumentParser(
        description="Rug-Farm creativity agents: names, story, badge, post."
    )
    parser.add_argument("--verdict", type=str, default="unknown",
                        help="Verdict: flagged / under_review / not_flagged / unknown")
    parser.add_argument("--symbol", type=str, default="", help="Coin symbol")
    parser.add_argument("--name", type=str, default="", help="Coin name / idea")
    parser.add_argument("--mc", type=float, default=None, help="Market cap in USD")
    parser.add_argument("--change", type=float, default=None, help="24h change in %")
    parser.add_argument("--liquidity", type=float, default=None, help="Liquidity in USD")
    parser.add_argument("--seed", type=int, default=0, help="Nickname seed (default: 0)")
    parser.add_argument("--changes", type=str, default=None,
                        help="Comma-separated price changes for the sparkline")
    parser.add_argument("--story", action="store_true", help="Print the story only")
    parser.add_argument("--post", action="store_true", help="Print the post only")
    parser.add_argument("--badge", action="store_true", help="Print the badge only")
    parser.add_argument("--names", action="store_true", help="Print nicknames only")
    parser.add_argument("--json", action="store_true", help="Print the result as JSON")
    # argparse treats a leading-dash value (e.g. --change -80) as a
    # new option, so join numeric options with a following negative
    # number into --opt=value form before parsing.
    argv = _join_negative_values(list(argv) if argv is not None else sys.argv[1:])
    args = parser.parse_args(argv)

    if args.verdict not in VERDICTS:
        print(f"ERROR: --verdict must be one of {', '.join(VERDICTS)}.", file=sys.stderr)
        return EXIT_INVALID
    if args.symbol and not re.fullmatch(r"[A-Za-z0-9]{1,10}", args.symbol):
        print("ERROR: --symbol must be 1-10 letters/digits.", file=sys.stderr)
        return EXIT_INVALID
    if args.mc is not None and (args.mc < 0 or math.isnan(args.mc)):
        print("ERROR: --mc must be a non-negative number.", file=sys.stderr)
        return EXIT_INVALID
    if args.change is not None and math.isnan(args.change):
        print("ERROR: --change must be a number.", file=sys.stderr)
        return EXIT_INVALID
    try:
        changes = _parse_changes(args.changes)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_INVALID

    verdict = {
        "verdict": args.verdict,
        "symbol": args.symbol,
        "market_cap_usd": args.mc,
        "change_24h_pct": args.change,
        "liquidity_usd": args.liquidity,
    }
    result = creative_check(
        verdict, name=args.name, ticker=args.symbol, seed=args.seed, changes=changes
    )

    only = args.story or args.post or args.badge or args.names
    if args.json:
        print(json.dumps(result, indent=2))
        return EXIT_OK
    if only:
        if args.names:
            print("\n".join(result["nicknames"]))
        if args.story:
            print(result["story"])
        if args.badge:
            b = result["badge"]
            print(f"{b['emoji']} {b['summary']} [{b['colour']}]")
            if changes:
                print(f"Sparkline: {result['sparkline']}")
        if args.post:
            print(result["post"])
        return EXIT_OK
    print("Nicknames:")
    for nick in result["nicknames"]:
        print(f"  - {nick}")
    print("")
    print(f"Story: {result['story']}")
    print("")
    b = result["badge"]
    print(f"Badge: {b['emoji']} {b['summary']} [{b['colour']}]")
    if changes:
        print(f"Sparkline: {result['sparkline']}")
    print("")
    print(f"Post ({len(result['post'])} chars): {result['post']}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
