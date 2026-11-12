"""Shared httpx client construction for live (non-test) calls.

Why this exists: ``httpx.Client()`` with default settings reads the
process proxy environment. In some environments ``NO_PROXY`` contains
bracketed IPv6 entries (e.g. ``[::1]``, ``[fd8b:...::1]``) which
httpx's proxy-pattern parser rejects with
``httpx.InvalidURL: Invalid port: ':1]'`` — so a live DexScreener /
Helius call crashes before any request is made, even though the
proxy itself works. Tests pass because they inject a MockTransport
client and never build a real one.

The fix: build the real client with ``trust_env=False`` (so httpx
never parses ``NO_PROXY``), pass the proxy explicitly from
``https_proxy`` / ``http_proxy``, and pass the TLS bundle explicitly
from ``SSL_CERT_FILE`` when set (the egress proxy presents a
local CA, so the default certifi bundle alone fails verification).
Injected clients are untouched — tests still use MockTransport.
"""
from __future__ import annotations

import os

import httpx


def make_client(timeout: float = 30.0) -> httpx.Client:
    """A live httpx client that survives bracketed-IPv6 NO_PROXY values."""
    proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    verify = os.environ.get("SSL_CERT_FILE") or True
    return httpx.Client(timeout=timeout, trust_env=False, proxy=proxy, verify=verify)
