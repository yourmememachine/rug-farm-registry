"""Keyless public-Solana-RPC access for the deep check (v2.16).

The deep check used to need two keys to work at all: DexScreener
for the coin half and Helius for the dev half. A mint with no
DexScreener pair (e.g. BagsBack, never listed) returned no coin
card, and without ``HELIUS_API_KEY`` the whole dev half was
skipped. This module is the keyless fallback that makes the mint
check work every time — no key, no account, just the public
Solana RPC:

- ``PublicRpcClient`` — JSON-RPC over the public endpoints
  (api.mainnet-beta.solana.com, fallback solana-rpc.publicnode.com),
  paced, with 429 retries honouring Retry-After and endpoint
  fallback. A method that 429s everywhere after full retries is
  marked dead for the rest of the run, so one throttled method
  cannot burn the whole budget. ``client`` is an injectable
  ``httpx.Client`` for tests (MockTransport); injected clients
  skip pacing/sleeps.
- ``fetch_rpc_coin`` — the coin card when DexScreener has no
  pair: ``getTokenSupply`` (supply), ``getTokenLargestAccounts``
  (top holders / top1 / top10 shares, with a ``getProgramAccounts``
  holder scan as fallback when largest-accounts is throttled),
  ``getAccountInfo`` on the mint (decimals, token program,
  mint/freeze authority — revoked or not), and the Metaplex
  metadata account (name, symbol, creators). There is no price
  on-chain — a card built this way has no price, market cap or
  volume, and says so. Nothing is invented: a missing piece
  stays missing and is counted in the note.
- ``fetch_metadata`` / ``parse_metadata`` — the mint's Metaplex
  metadata account, found with a ``getProgramAccounts`` memcmp
  filter on the mint field (offset 33), so no PDA derivation is
  needed. The creators array is parsed Borsh-style (name /
  symbol / uri are length-prefixed strings, then the creators
  vector). A malformed account parses to None, never a guess.
- ``PublicRpcIndexer`` — a keyless stand-in for
  ``HeliusIndexer`` with the same surface the deep check needs:
  ``get_signatures``, ``get_transaction``,
  ``fetch_funding_transfers`` (parsed system transfers, via
  ``src.chain_index.parse_funding_transfers``) and
  ``fetch_mint_outcome`` (DexScreener-based, no key — delegated
  to a keyless ``HeliusIndexer``). ``_rpc`` answers ``getAsset``
  from the Metaplex metadata account (its creators array) and
  raises for every other DAS method: the public RPC has no DAS,
  so creator resolution goes through the metadata account
  instead. The deep check's ``DevHistoryBuilder`` works with
  this adapter unchanged (it only needs ``enabled`` /
  ``get_signatures`` / ``get_transaction`` /
  ``fetch_mint_outcome``).

Honest limits, stated wherever the data is used:
- The public endpoints rate-limit by IP. Calls are paced
  (default 0.5 s apart) and retried; a method that stays
  throttled is reported as missing, never faked.
- The funding scan is bounded (the dev's last 100 signatures,
  20 per other wallet by default) — funding from long ago can
  be missed, and the report says so.
- ``getProgramAccounts`` holder scans return every token
  account for the mint; for a mint with very many holders that
  is a heavy response, and a failed scan leaves the holder
  count missing rather than guessed.

Stdlib + httpx only, no wallet, no funds moved.
"""
from __future__ import annotations

import base64
import time

import httpx

PUBLIC_RPC_ENDPOINTS = (
    "https://api.mainnet-beta.solana.com",
    "https://solana-rpc.publicnode.com",
)
METADATA_PROGRAM = "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s"
TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# Verified live 2026-11-13 via getAccountInfo (executable, BPF-loader
# owned). Do NOT "correct" this to the commonly misremembered tail
# ...EPF3SfwV1CZ5LwU4MsS — that address holds NO account.
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

DEFAULT_RPC_DELAY_SECONDS = 0.5
DEFAULT_MAX_RETRIES = 3
# Backoff between 429 retries. Small on purpose: a method that
# 429s on both endpoints is marked dead for the run anyway, so
# there is no point burning minutes on it — the old 5s base
# turned one throttled method into a ~150s stall on a live check.
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 30.0
# The holder scan is the heaviest call in the keyless path; a mint
# with more accounts than this still scans (the RPC returns what it
# returns) — the bound is documented, not enforced mid-response.
DEFAULT_SIGNATURE_LIMIT = 100

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


class RpcError(RuntimeError):
    """A public-RPC call failed after retries / endpoint fallback."""


def b58encode(data: bytes) -> str:
    """Base58-encode bytes (Bitcoin alphabet, leading zero bytes -> '1')."""
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    leading = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading + out


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


class PublicRpcClient:
    """JSON-RPC over the public Solana RPC endpoints.

    Paces calls, retries 429s honouring Retry-After, falls back
    to the next endpoint when one is exhausted, and marks a
    method dead for the rest of the run when it 429s everywhere
    after full retries. ``client`` is an injectable httpx.Client
    for tests (MockTransport); injected clients skip pacing and
    sleeps.
    """

    def __init__(
        self,
        endpoints: tuple[str, ...] | None = None,
        client: httpx.Client | None = None,
        delay: float = DEFAULT_RPC_DELAY_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ):
        self.endpoints = list(endpoints or PUBLIC_RPC_ENDPOINTS)
        self._client = client
        self.delay = delay
        self.max_retries = max_retries
        self.calls = 0
        self.dead_methods: set[str] = set()
        self._last_call = 0.0

    def _sleep(self, seconds: float) -> None:
        if self._client is not None:
            return  # tests: no real waiting
        if seconds > 0:
            time.sleep(seconds)

    def _pace(self) -> None:
        if self._client is not None:
            return
        now = time.monotonic()
        wait = self.delay - (now - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * (2**attempt))

    def _post(self, endpoint: str, method: str, params: list) -> httpx.Response:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        if self._client is not None:
            return self._client.post(endpoint, json=payload)
        # Local import, not module-level: tests monkeypatch
        # src.http_client.make_client and the patch must be seen here.
        from .http_client import make_client

        with make_client(timeout=60.0) as client:
            return client.post(endpoint, json=payload)

    def rpc(self, method: str, params: list):
        """One JSON-RPC call. Returns the ``result`` field.

        Raises RpcError when every endpoint fails after retries.
        An RPC-level ``error`` in the response is deterministic
        (e.g. an unknown mint) and raises immediately without
        retrying.
        """
        if method in self.dead_methods:
            raise RpcError(f"{method} is marked dead for this run (throttled).")
        errors: list[str] = []
        for endpoint in self.endpoints:
            attempt = 0
            while attempt <= self.max_retries:
                self._pace()
                try:
                    resp = self._post(endpoint, method, params)
                except httpx.HTTPError as exc:
                    errors.append(f"{endpoint}: transport {exc}")
                    self._sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                if resp.status_code == 429:
                    wait = _retry_after_seconds(resp) or self._backoff(attempt)
                    errors.append(f"{endpoint}: 429")
                    self._sleep(wait)
                    attempt += 1
                    continue
                if resp.status_code != 200:
                    errors.append(f"{endpoint}: HTTP {resp.status_code}")
                    break  # try the next endpoint
                try:
                    data = resp.json()
                except ValueError:
                    errors.append(f"{endpoint}: non-JSON response")
                    break
                if isinstance(data, dict) and "error" in data:
                    raise RpcError(f"{endpoint}: RPC error {data['error']}")
                self.calls += 1
                return data.get("result") if isinstance(data, dict) else None
            # retries exhausted on this endpoint — try the next one
        if any("429" in e for e in errors):
            self.dead_methods.add(method)
        raise RpcError(f"{method} failed on all endpoints: " + "; ".join(errors))

    def probe(self) -> bool:
        """getSlot on the first working endpoint; False when none works."""
        try:
            self.rpc("getSlot", [])
            return True
        except RpcError:
            return False


# ======================================================================
# Raw RPC fetch helpers (parsed data or None; RpcError on failure)
# ======================================================================
def get_mint_account(rpc: PublicRpcClient, mint: str) -> dict | None:
    """getAccountInfo (jsonParsed) on a mint.

    Returns {"owner": token program, "decimals": int,
    "mint_authority": address | "revoked" | "unknown",
    "freeze_authority": address | "revoked" | "unknown"} or None
    when the account is missing / unparsable. An authority of
    None in the parsed info means revoked — that is a real fact,
    not a missing value.
    """
    result = rpc.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, dict):
        return None
    owner = value.get("owner")
    data = value.get("data") or {}
    info = (data.get("parsed") or {}).get("info") or {}

    def _authority(field: str) -> str:
        raw = info.get(field)
        if raw is None:
            return "revoked"
        return raw if isinstance(raw, str) else "unknown"

    decimals = info.get("decimals")
    return {
        "owner": owner if isinstance(owner, str) else None,
        "decimals": decimals if isinstance(decimals, int) else None,
        "mint_authority": _authority("mintAuthority"),
        "freeze_authority": _authority("freezeAuthority"),
    }


def get_token_supply(rpc: PublicRpcClient, mint: str) -> float | None:
    """getTokenSupply -> total supply in UI units, or None."""
    result = rpc.rpc("getTokenSupply", [mint])
    if not isinstance(result, dict):
        return None
    value = result.get("value") or {}
    amount = value.get("uiAmount")
    return float(amount) if isinstance(amount, (int, float)) else None


def get_largest_accounts(rpc: PublicRpcClient, mint: str) -> list[dict] | None:
    """getTokenLargestAccounts -> [{address, amount}] or None.

    Amounts are UI units. Returns None when the call returns no
    usable list (the caller then falls back to the holder scan).
    """
    result = rpc.rpc("getTokenLargestAccounts", [mint])
    if not isinstance(result, dict):
        return None
    value = result.get("value")
    if not isinstance(value, list):
        return None
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        amount = item.get("uiAmount")
        if isinstance(amount, (int, float)) and item.get("address"):
            out.append({"address": item["address"], "amount": float(amount)})
    return out or None


def get_holder_accounts(
    rpc: PublicRpcClient, program: str, mint: str, decimals: int
) -> list[dict] | None:
    """getProgramAccounts holder scan for one mint.

    One contiguous dataSlice (offset 32, length 40) returns the
    owner (32 bytes) + amount (u64 LE) per token account, so the
    response stays small. NO dataSize filter: Token-2022 accounts
    with extensions are larger than 165 bytes, so a dataSize 165
    filter misses almost all of them. Returns [{"owner", "amount"}]
    in UI units, largest first, or None when the program rejects
    the call. Accounts with a zero balance are included — the
    caller counts holders as the ones with amount > 0.
    """
    result = rpc.rpc(
        "getProgramAccounts",
        [
            program,
            {
                "filters": [{"memcmp": {"offset": 0, "bytes": mint}}],
                "dataSlice": {"offset": 32, "length": 40},
                "encoding": "base64",
            },
        ],
    )
    if not isinstance(result, list):
        return None
    accounts: list[dict] = []
    for item in result:
        if not isinstance(item, dict):
            continue
        account = item.get("account") or {}
        data = account.get("data")
        if not isinstance(data, list) or not data:
            continue
        try:
            raw = base64.b64decode(data[0], validate=True)
        except Exception:
            continue
        if len(raw) != 40:
            continue
        owner = b58encode(raw[:32])
        amount = int.from_bytes(raw[32:40], "little") / (10**decimals)
        accounts.append({"owner": owner, "amount": amount})
    accounts.sort(key=lambda a: a["amount"], reverse=True)
    return accounts


def get_metadata_account(rpc: PublicRpcClient, mint: str) -> bytes | None:
    """The mint's Metaplex metadata account, raw bytes, or None.

    Found with a getProgramAccounts memcmp filter on the mint
    field (byte offset 33 in the metadata account) — no PDA
    derivation needed. None when no metadata account exists.
    """
    result = rpc.rpc(
        "getProgramAccounts",
        [
            METADATA_PROGRAM,
            {
                "filters": [{"memcmp": {"offset": 33, "bytes": mint}}],
                "encoding": "base64",
            },
        ],
    )
    if not isinstance(result, list) or not result:
        return None
    first = result[0]
    if not isinstance(first, dict):
        return None
    account = first.get("account") or {}
    data = account.get("data")
    if not isinstance(data, list) or not data:
        return None
    try:
        raw = base64.b64decode(data[0], validate=True)
    except Exception:
        return None
    return raw or None


def parse_metadata(data: bytes) -> dict | None:
    """Parse Metaplex metadata account bytes.

    Layout (Borsh): key u8 (4 = MetadataV1), update_authority
    32 bytes, mint 32 bytes, then length-prefixed name / symbol /
    uri strings, seller_fee_basis_points u16, creators option
    (u8 0/1, then u32 count, then per creator: address 32 bytes,
    verified u8, share u8). Returns {"name", "symbol", "uri",
    "creators": [{"address", "verified", "share"}],
    "update_authority"} or None when the account is not a
    parseable MetadataV1 account. A creators section that is
    truncated or absurd parses to creators=[] — the name and
    symbol are still returned.
    """
    if not isinstance(data, bytes) or len(data) < 66 or data[0] != 4:
        return None
    update_authority = b58encode(data[1:33])
    pos = 65

    def _read_str(pos: int) -> tuple[str | None, int]:
        if pos + 4 > len(data):
            return None, pos
        n = int.from_bytes(data[pos : pos + 4], "little")
        pos += 4
        if n > 512 or pos + n > len(data):
            return None, pos
        try:
            text = data[pos : pos + n].decode("utf-8")
        except UnicodeDecodeError:
            text = data[pos : pos + n].decode("utf-8", "replace")
        return text, pos + n

    name, pos = _read_str(pos)
    if name is None:
        return None
    symbol, pos = _read_str(pos)
    if symbol is None:
        return None
    uri, pos = _read_str(pos)
    if uri is None:
        return None
    result: dict = {
        "name": name,
        "symbol": symbol,
        "uri": uri,
        "creators": [],
        "update_authority": update_authority,
    }
    if pos + 2 > len(data):
        return result
    pos += 2  # seller_fee_basis_points
    if pos >= len(data):
        return result
    has_creators = data[pos]
    pos += 1
    if has_creators != 1:
        return result
    if pos + 4 > len(data):
        return result
    count = int.from_bytes(data[pos : pos + 4], "little")
    pos += 4
    if count > 20:
        return result
    creators: list[dict] = []
    for _ in range(count):
        if pos + 34 > len(data):
            break
        address = b58encode(data[pos : pos + 32])
        pos += 32
        verified = data[pos] == 1
        pos += 1
        share = data[pos]
        pos += 1
        creators.append(
            {"address": address, "verified": verified, "share": share}
        )
    result["creators"] = creators
    return result


def fetch_metadata(rpc: PublicRpcClient, mint: str) -> dict | None:
    """Parsed Metaplex metadata for a mint, or None when there is
    no metadata account or it does not parse."""
    raw = get_metadata_account(rpc, mint)
    if raw is None:
        return None
    return parse_metadata(raw)


def best_metadata_creator(metadata: dict | None) -> str | None:
    """The metadata account's creator: verified wins ties, then
    highest share — the same selection rule as the DAS path.
    None when the metadata has no creators array."""
    creators = [
        c
        for c in ((metadata or {}).get("creators") or [])
        if isinstance(c, dict) and c.get("address")
    ]
    if not creators:
        return None
    best = max(
        creators,
        key=lambda c: (bool(c.get("verified")), c.get("share") or 0),
    )
    return best["address"]


def get_signatures(
    rpc: PublicRpcClient,
    address: str,
    limit: int = DEFAULT_SIGNATURE_LIMIT,
    before: str | None = None,
) -> list[dict]:
    """getSignaturesForAddress, newest first. [] when none."""
    opts: dict = {"limit": limit}
    if before:
        opts["before"] = before
    result = rpc.rpc("getSignaturesForAddress", [address, opts])
    if not isinstance(result, list):
        return []
    return [e for e in result if isinstance(e, dict)]


def get_transaction(rpc: PublicRpcClient, signature: str) -> dict | None:
    """One jsonParsed transaction, or None when unavailable.

    maxSupportedTransactionVersion 1: version-0-only requests
    fail on v1 transactions with -32015 on the public node.
    """
    result = rpc.rpc(
        "getTransaction",
        [
            signature,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 1},
        ],
    )
    return result if isinstance(result, dict) else None


# ======================================================================
# Keyless coin card (DexScreener-miss fallback)
# ======================================================================
def fetch_rpc_coin(
    rpc: PublicRpcClient, mint: str
) -> tuple[dict | None, str, dict | None]:
    """Coin card from the public RPC alone.

    Returns ``(coin_or_None, note, metadata_or_None)``. ``coin``
    is None only when the mint account, the supply, the metadata
    and the holder data are ALL missing — i.e. the mint has no
    on-chain trace at all. There is no price on-chain: a card
    built this way has no price, market cap or volume, and the
    note says so. Every failed lookup is recorded in the note
    instead of being silently skipped.
    """
    notes: list[str] = []
    mint_acct = None
    try:
        mint_acct = get_mint_account(rpc, mint)
    except RpcError as exc:
        notes.append(f"mint account lookup failed: {exc}")
    supply = None
    try:
        supply = get_token_supply(rpc, mint)
    except RpcError as exc:
        notes.append(f"supply lookup failed: {exc}")
    metadata = None
    try:
        metadata = fetch_metadata(rpc, mint)
    except RpcError as exc:
        notes.append(f"metadata lookup failed: {exc}")
    largest = None
    try:
        largest = get_largest_accounts(rpc, mint)
    except RpcError as exc:
        notes.append(f"largest-accounts lookup failed: {exc}")

    program = (mint_acct or {}).get("owner")
    decimals = (mint_acct or {}).get("decimals")
    holder_count = None
    top1_share = None
    top10_share = None
    top10_shares: list[float] = []
    top_holders: list[dict] = []
    if largest and supply:
        top_holders = [
            {
                "address": a["address"],
                "amount": a["amount"],
                "share": a["amount"] / supply,
            }
            for a in largest[:10]
        ]
        top1_share = largest[0]["amount"] / supply
        top10_share = sum(a["amount"] for a in largest[:10]) / supply
        top10_shares = [a["amount"] / supply for a in largest[:10]]
    # Holder count + fuller shares via the program scan (best
    # effort — a failed scan leaves the count missing).
    if program in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM) and decimals is not None:
        try:
            accounts = get_holder_accounts(rpc, program, mint, decimals)
        except RpcError as exc:
            notes.append(f"holder scan failed: {exc}")
            accounts = None
        if accounts is not None:
            holder_count = len([a for a in accounts if a["amount"] > 0])
            # Zero-balance token accounts are real accounts but
            # not holders — keep them out of the top-holder list
            # (and out of the side-wallet funding scan).
            top = [a for a in accounts if a["amount"] > 0][:10]
            if supply:
                if top:
                    top1_share = top[0]["amount"] / supply
                    top10_share = sum(a["amount"] for a in top) / supply
                    top10_shares = [a["amount"] / supply for a in top]
                    top_holders = [
                        {
                            "address": a["owner"],
                            "amount": a["amount"],
                            "share": a["amount"] / supply,
                        }
                        for a in top
                    ]

    found = (
        mint_acct is not None
        or supply is not None
        or metadata is not None
        or largest is not None
    )
    if not found:
        return (
            None,
            "no on-chain mint data found for this mint on the public Solana RPC.",
            metadata,
        )
    if mint_acct is None:
        notes.append("mint account not readable — authority status unknown.")
    notes.append("No price, market cap or volume: the public RPC has no market data.")
    coin = {
        "mint": mint,
        "name": (metadata or {}).get("name"),
        "symbol": (metadata or {}).get("symbol") or "?",
        "source": "public_rpc",
        "supply": supply,
        "decimals": decimals,
        "token_program": program,
        "mint_authority": (
            (mint_acct or {}).get("mint_authority", "unknown")
        ),
        "freeze_authority": (
            (mint_acct or {}).get("freeze_authority", "unknown")
        ),
        "holder_count": holder_count,
        "top1_share": top1_share,
        "top10_share": top10_share,
        "top10_shares": top10_shares,
        "top_holders": top_holders,
        # No market fields on-chain — explicit Nones, never guessed.
        "price_usd": None,
        "market_cap_usd": None,
        "liquidity_usd": None,
        "volume_24h_usd": None,
        "change_24h_pct": None,
        "txns_24h_buys": None,
        "txns_24h_sells": None,
        "watch_label": "unknown",
        "outcome": "insufficient_data",
        "pair_created_at": None,
        "pair_age_minutes": None,
        "dexscreener_url": f"https://dexscreener.com/solana/{mint}",
    }
    return coin, "; ".join(notes), metadata


# ======================================================================
# Keyless indexer adapter (HeliusIndexer surface, public RPC)
# ======================================================================
class PublicRpcIndexer:
    """Keyless stand-in for ``HeliusIndexer``.

    Same surface the deep check needs: ``enabled``,
    ``get_signatures``, ``get_transaction``,
    ``fetch_funding_transfers`` and ``fetch_mint_outcome``.
    ``_rpc`` answers ``getAsset`` from the Metaplex metadata
    account (its creators array) and raises for every other DAS
    method — the public RPC has no DAS.
    ``fetch_mint_outcome`` is DexScreener-based, exactly like
    the keyed path, delegated to a keyless ``HeliusIndexer``.
    """

    def __init__(self, rpc: PublicRpcClient | None = None):
        self.rpc = rpc if rpc is not None else PublicRpcClient()
        self.enabled = True

    def _rpc(self, method: str, params):
        """DAS, keyless-style.

        The public RPC has no DAS, but ``getAsset`` is the one DAS
        method the deep check needs, and its creators answer lives
        in the Metaplex metadata account. So ``getAsset`` is
        answered from that account (``{"creators": [...]}``, or
        ``{}`` when there is no metadata account) and every other
        DAS method raises — callers that need a real DAS method
        must use the keyed indexer.
        """
        if method == "getAsset":
            mint = None
            if isinstance(params, dict):
                mint = params.get("id")
            elif isinstance(params, list) and params:
                mint = params[0]
            if not mint:
                return {}
            metadata = fetch_metadata(self.rpc, mint)
            if metadata is None:
                return {}
            return {"creators": metadata.get("creators") or []}
        raise RuntimeError(
            f"public RPC has no DAS method {method!r} — use the "
            "Metaplex metadata account instead"
        )

    def get_signatures(
        self, address: str, limit: int = 50, before: str | None = None
    ) -> list[dict]:
        return get_signatures(self.rpc, address, limit=limit, before=before)

    def get_transaction(self, signature: str) -> dict | None:
        return get_transaction(self.rpc, signature)

    def fetch_funding_transfers(
        self, wallets: list[str], limit_per_wallet: int = 20
    ) -> list[dict]:
        """Native SOL transfers involving the given wallets.

        Same shape as ``HeliusIndexer.fetch_funding_transfers``:
        [{"from", "to", "amount_sol", "signature"}]. Best-effort:
        any wallet whose history fails is skipped.
        """
        from .chain_index import parse_funding_transfers

        transfers: list[dict] = []
        wanted = set(wallets)
        for wallet in wallets:
            try:
                sigs = get_signatures(self.rpc, wallet, limit=limit_per_wallet)
            except RpcError:
                continue
            for entry in sigs:
                sig = entry.get("signature") if isinstance(entry, dict) else None
                if not sig:
                    continue
                try:
                    tx = get_transaction(self.rpc, sig)
                except RpcError:
                    continue
                if not tx:
                    continue
                for edge in parse_funding_transfers(tx):
                    src, dst = edge.get("from"), edge.get("to")
                    if (
                        src
                        and dst
                        and src != dst
                        and (src in wanted or dst in wanted)
                    ):
                        transfers.append(
                            {
                                "from": src,
                                "to": dst,
                                "amount_sol": (edge.get("amount_lamports") or 0)
                                / 1e9,
                                "signature": edge.get("signature"),
                            }
                        )
        return transfers

    def fetch_mint_outcome(self, mint: str, dev_wallet: str | None = None):
        """Outcome evidence for one mint from DexScreener (public).

        Delegated to a keyless ``HeliusIndexer`` — its
        ``fetch_mint_outcome`` is DexScreener-only and never
        touches Helius, so the empty key is never used for a
        live call. Same logic as the keyed path.
        """
        from .indexer import HeliusIndexer

        return HeliusIndexer(api_key="").fetch_mint_outcome(
            mint, dev_wallet=dev_wallet
        )
