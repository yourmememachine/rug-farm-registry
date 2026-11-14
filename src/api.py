"""FastAPI app for the Open Rug-Farm Registry.

Endpoints (JSON only):
    GET /health
    GET /registry
    GET /wallets/{address}
    GET /check?mint=<mint>&dev=<dev_wallet>
    GET /clusters/{cluster_id}

Run:  uvicorn src.api:app --reload --port 8000
Registry path can be overridden with the REGISTRY_PATH env var.

Pay-per-check (v2.18, src/x402.py): when RUGFARM_X402_PAYEE is
set, GET /check and GET /wallets/{address} cost $0.001 USDC
(Solana mainnet) via the x402 facilitator — no payee means no
gating, and the API behaves exactly as before, free.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware

from .cluster import build_clusters  # noqa: F401  (re-exported for callers)
from .registry import (
    check_wallet,
    get_wallet,
    is_flagged,
    list_flagged,
    load_registry,
)
from .x402 import X402Gate

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_REGISTRY_PATH = PROJECT_ROOT / "data" / "registry.json"

app = FastAPI(
    title="Open Rug-Farm Registry",
    description=(
        "Public, git-versioned registry of serial-rugger dev wallets "
        "and funding-linked clusters. SAMPLE DATA ONLY in this MVP — "
        "no real wallet is accused."
    ),
    version="0.1.0",
)

# CORS: the API is public and read-only, and the Solana OS calls it
# straight from the browser. Without these headers browsers refuse
# to read the replies (the OS reported "unreachable from this
# browser"). No credentials are ever involved, so any origin is fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)

# x402 pay-per-check gate, built from the environment at import.
# No RUGFARM_X402_PAYEE configured -> disabled -> the API stays
# free. Tests replace this object with a gate wired to a mocked
# facilitator and a temporary database.
x402_gate = X402Gate.from_env()


def require_payment(request: Request) -> dict:
    """FastAPI dependency gating one paid endpoint (no-op when free)."""
    return x402_gate.require_payment(request)


def _registry_path() -> Path:
    return Path(os.environ.get("REGISTRY_PATH", DEFAULT_REGISTRY_PATH))


def _load() -> dict:
    return load_registry(_registry_path())


@app.get("/health")
def health() -> dict:
    data = _load()
    return {
        "status": "ok",
        "version": data.get("version"),
        "sample_data": bool(data.get("sample_data")),
        "entries": len(data.get("entries", [])),
        "flagged": len(list_flagged(data)),
    }


@app.get("/")
def index() -> dict:
    return {
        "name": "Open Rug-Farm Registry",
        "version": "0.1.0",
        "sample_data": True,
        "endpoints": ["/health", "/registry", "/wallets/{address}",
                      "/check?dev=<wallet>&mint=<mint>", "/clusters/{cluster_id}"],
    }


@app.get("/registry")
def registry() -> dict:
    return _load()


@app.get("/wallets/{address}")
def wallet(address: str, _payment: dict = Depends(require_payment)) -> dict:
    data = _load()
    entry = get_wallet(data, address)
    if entry is None:
        raise HTTPException(status_code=404, detail="Wallet not in registry")
    return {**entry, "currently_flagged": is_flagged(entry)}


@app.get("/check")
def check(
    dev: str = Query(..., description="Dev wallet address to check"),
    mint: str | None = Query(None, description="Mint address (optional)"),
    _payment: dict = Depends(require_payment),
) -> dict:
    data = _load()
    return check_wallet(data, dev, mint=mint)


@app.get("/clusters/{cluster_id}")
def cluster(cluster_id: str) -> dict:
    data = _load()
    members = [e for e in data.get("entries", []) if e.get("cluster_id") == cluster_id]
    if not members:
        raise HTTPException(status_code=404, detail="Cluster not found")
    return {
        "cluster_id": cluster_id,
        "wallets": [e["wallet"] for e in members],
        "flagged_wallets": [e["wallet"] for e in members if is_flagged(e)],
        "entries": members,
    }
