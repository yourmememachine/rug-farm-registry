"""Registry loading, validation and flagging rules.

Core rule (from the research): a wallet is flagged ONLY when it has
>= 2 rugged mints, each with on-chain evidence, and its entry has not
expired (last_seen + ttl_days). One rug = under_review, never flagged.
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

BASE58_ALPHABET = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
MIN_RUGGED_MINTS_TO_FLAG = 2
DEFAULT_TTL_DAYS = 180


def is_valid_solana_address(address: object) -> bool:
    """Solana addresses are 32-44 base58 characters (no 0, O, I, l)."""
    if not isinstance(address, str):
        return False
    return 32 <= len(address) <= 44 and all(c in BASE58_ALPHABET for c in address)


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def validate_mint(mint: object, prefix: str = "rugged_mints[]") -> list[str]:
    errors: list[str] = []
    if not isinstance(mint, dict):
        return [f"{prefix}: must be an object"]
    if not is_valid_solana_address(mint.get("mint")):
        errors.append(f"{prefix}: 'mint' is not a valid Solana address")
    if mint.get("outcome") not in ("rugged", "failed_but_honest", "inconclusive"):
        errors.append(f"{prefix}: 'outcome' must be rugged / failed_but_honest / inconclusive")
    for field in ("peak_market_cap_usd", "current_market_cap_usd"):
        if not isinstance(mint.get(field), (int, float)) or mint[field] < 0:
            errors.append(f"{prefix}: '{field}' must be a non-negative number")
    evidence = mint.get("evidence")
    if not isinstance(evidence, dict) or not evidence:
        errors.append(f"{prefix}: 'evidence' is required (at least one link)")
    else:
        for key, url in evidence.items():
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                errors.append(f"{prefix}: evidence '{key}' must be an http(s) URL")
    return errors


def validate_entry(entry: object) -> list[str]:
    """Return a list of validation errors; empty list means valid."""
    errors: list[str] = []
    if not isinstance(entry, dict):
        return ["entry: must be an object"]
    wallet = entry.get("wallet")
    if not is_valid_solana_address(wallet):
        errors.append("wallet: not a valid Solana address")
    if not isinstance(entry.get("cluster_id"), str) or not entry.get("cluster_id"):
        errors.append("cluster_id: required non-empty string")
    if entry.get("status") not in ("flagged", "under_review", "expired", "disputed"):
        errors.append("status: must be flagged / under_review / expired / disputed")
    if _parse_date(entry.get("first_seen")) is None:
        errors.append("first_seen: must be a YYYY-MM-DD date")
    if _parse_date(entry.get("last_seen")) is None:
        errors.append("last_seen: must be a YYYY-MM-DD date")
    ttl = entry.get("ttl_days")
    if not isinstance(ttl, int) or ttl <= 0:
        errors.append("ttl_days: must be a positive integer")
    mints = entry.get("rugged_mints")
    if not isinstance(mints, list) or not mints:
        errors.append("rugged_mints: at least one mint with evidence is required")
        return errors
    for i, mint in enumerate(mints):
        errors.extend(validate_mint(mint, f"rugged_mints[{i}]"))
    # The >=2 rule: a 'flagged' entry needs at least 2 rugged mints.
    rugged = [m for m in mints if isinstance(m, dict) and m.get("outcome") == "rugged"]
    if entry.get("status") == "flagged" and len(rugged) < MIN_RUGGED_MINTS_TO_FLAG:
        errors.append(
            f"flagged entries need >= {MIN_RUGGED_MINTS_TO_FLAG} rugged mints, got {len(rugged)}"
        )
    return errors


def validate_registry(data: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["registry: must be a JSON object"]
    if data.get("min_rugged_mints_to_flag", MIN_RUGGED_MINTS_TO_FLAG) != MIN_RUGGED_MINTS_TO_FLAG:
        errors.append("min_rugged_mints_to_flag: must be 2")
    entries = data.get("entries")
    if not isinstance(entries, list):
        return errors + ["entries: must be a list"]
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        for err in validate_entry(entry):
            errors.append(f"entries[{i}]: {err}")
        wallet = entry.get("wallet") if isinstance(entry, dict) else None
        if isinstance(wallet, str):
            if wallet in seen:
                errors.append(f"entries[{i}]: duplicate wallet {wallet}")
            seen.add(wallet)
    return errors


def load_registry(path: str | Path) -> dict:
    """Load and validate the registry file. Raises ValueError on bad data."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    errors = validate_registry(data)
    if errors:
        raise ValueError("Invalid registry:\n" + "\n".join(errors))
    return data


def is_expired(entry: dict, today: date | None = None) -> bool:
    today = today or date.today()
    last_seen = _parse_date(entry.get("last_seen"))
    ttl = entry.get("ttl_days", DEFAULT_TTL_DAYS)
    if last_seen is None or not isinstance(ttl, int):
        return True
    return today > last_seen + timedelta(days=ttl)


def rugged_mint_count(entry: dict) -> int:
    return sum(
        1
        for m in entry.get("rugged_mints", [])
        if isinstance(m, dict) and m.get("outcome") == "rugged"
    )


def is_flagged(entry: dict, today: date | None = None) -> bool:
    """Flagged = status flagged + >=2 rugged mints + evidence + not expired."""
    if entry.get("status") != "flagged":
        return False
    if rugged_mint_count(entry) < MIN_RUGGED_MINTS_TO_FLAG:
        return False
    if validate_entry(entry):
        return False
    return not is_expired(entry, today)


def get_wallet(data: dict, address: str) -> dict | None:
    for entry in data.get("entries", []):
        if entry.get("wallet") == address:
            return entry
    return None


REAL_DISCLAIMER = (
    "Contains legacy SAMPLE entries (wallet/mint addresses starting "
    "SAMPLE...) plus real verified entries added by the daily scan. "
    "SAMPLE entries are synthetic and are NOT accusations. Real entries "
    "were added only with >= 2 rugged mints, each with on-chain evidence."
)


def add_or_update_entry(
    data: dict, entry: dict, today: date | None = None
) -> tuple[bool, str]:
    """Add a new entry, or merge rugged mints into an existing wallet.

    Returns (changed, reason). Never adds a flagged entry with < 2
    rugged mints, never adds a mint without evidence, never duplicates
    a wallet or a mint. Merging promotes under_review -> flagged when
    the merged rugged count reaches 2. Adding the first real (non-
    SAMPLE) entry flips ``sample_data`` to False and updates the
    disclaimer so real entries are not mistaken for sample data.
    """
    today = today or date.today()
    errors = validate_entry(entry)
    if errors:
        return False, "invalid entry: " + "; ".join(errors)
    wallet = entry["wallet"]
    existing = get_wallet(data, wallet)
    if existing is None:
        data.setdefault("entries", []).append(entry)
    else:
        known = {m.get("mint") for m in existing.get("rugged_mints", [])}
        for mint in entry.get("rugged_mints", []):
            if mint.get("mint") not in known:
                existing.setdefault("rugged_mints", []).append(mint)
                known.add(mint.get("mint"))
        existing["last_seen"] = max(
            existing.get("last_seen", ""), entry.get("last_seen", "")
        )
        if rugged_mint_count(existing) >= MIN_RUGGED_MINTS_TO_FLAG:
            existing["status"] = "flagged"
        errors = validate_entry(existing)
        if errors:
            return False, "merged entry invalid: " + "; ".join(errors)
    if not str(wallet).startswith("SAMPLE"):
        data["sample_data"] = False
        data["disclaimer"] = REAL_DISCLAIMER
    return True, "added" if existing is None else "updated"


def save_registry(data: dict, path: str | Path, today: date | None = None) -> None:
    """Write the registry back as sorted, indented JSON."""
    today = today or date.today()
    data["updated_at"] = today.isoformat()
    errors = validate_registry(data)
    if errors:
        raise ValueError("Invalid registry, refusing to save:\n" + "\n".join(errors))
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")


def list_flagged(data: dict, today: date | None = None) -> list[dict]:
    return [e for e in data.get("entries", []) if is_flagged(e, today)]


def check_wallet(
    data: dict, dev_wallet: str, mint: str | None = None, today: date | None = None
) -> dict:
    """Pre-trade check for a dev wallet (and optionally a specific mint).

    Returns a JSON-serialisable dict with a risk level, reasons and the
    evidence behind the call — never just a bare boolean.
    """
    entry = get_wallet(data, dev_wallet)
    if entry is None:
        return {
            "dev_wallet": dev_wallet,
            "mint": mint,
            "flagged": False,
            "risk_level": "unknown",
            "rugged_mint_count": 0,
            "cluster_id": None,
            "reasons": ["No record for this dev wallet in the registry."],
            "evidence": [],
        }
    count = rugged_mint_count(entry)
    evidence = [m.get("evidence", {}) for m in entry.get("rugged_mints", [])]
    if is_expired(entry, today):
        return {
            "dev_wallet": dev_wallet,
            "mint": mint,
            "flagged": False,
            "risk_level": "expired",
            "rugged_mint_count": count,
            "cluster_id": entry.get("cluster_id"),
            "reasons": [
                f"Entry expired (last seen {entry.get('last_seen')}, "
                f"TTL {entry.get('ttl_days')} days). Re-verify before trusting it."
            ],
            "evidence": evidence,
        }
    if is_flagged(entry, today):
        return {
            "dev_wallet": dev_wallet,
            "mint": mint,
            "flagged": True,
            "risk_level": "high",
            "rugged_mint_count": count,
            "cluster_id": entry.get("cluster_id"),
            "reasons": [
                f"Flagged serial rugger: {count} rugged mints with on-chain evidence.",
                f"Cluster: {entry.get('cluster_id')}.",
            ],
            "evidence": evidence,
        }
    return {
        "dev_wallet": dev_wallet,
        "mint": mint,
        "flagged": False,
        "risk_level": "watch",
        "rugged_mint_count": count,
        "cluster_id": entry.get("cluster_id"),
        "reasons": [
            f"Under review: {count} rugged mint(s) — below the "
            f"{MIN_RUGGED_MINTS_TO_FLAG}-rug threshold to flag."
        ],
        "evidence": evidence,
    }
