"""CORS headers on the public API (v2.18 fix).

The Solana OS calls the hosted API straight from the browser.
Without CORS headers browsers refuse to read the replies, so the
OS reported "unreachable from this browser". The API is public
and read-only with no credentials involved, so any origin is fine.
"""

from fastapi.testclient import TestClient

from src.api import app


def test_cors_header_on_health():
    client = TestClient(app)
    resp = client.get("/health", headers={"Origin": "https://muse.ai"})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"


def test_cors_preflight_allows_get():
    client = TestClient(app)
    resp = client.options(
        "/registry",
        headers={
            "Origin": "https://muse.ai",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == "*"
    assert "GET" in resp.headers.get("access-control-allow-methods", "")
