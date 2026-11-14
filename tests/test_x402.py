"""x402 pay-per-check tests — the facilitator is mocked, no real payment.

The gate is exercised two ways: directly (unit level) and through
the FastAPI app with TestClient (wiring level). The mocked
facilitator counts its /verify and /settle calls, so replay
protection ("settle called once") is asserted, not assumed.
"""
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import src.api as api_module
import src.db as db
from src.api import app
from src.x402 import USDC_MINT, X402Gate, usdc_to_base_units

PAYEE = "TestPayee111111111111111111111111111111111"
OTHER_PAYEE = "OtherPayee1111111111111111111111111111111"
FLAGGED = "SAMPLEDEV11111111111111111111111111111111"
SIGNATURE = "sig-abc-123-def456"


def payment_header(signature=SIGNATURE, **extra):
    """An X-Payment header value carrying a signed-payment payload."""
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "solana",
        "payload": {"signature": signature, **extra},
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


class FakeFacilitator:
    """Mocked facilitator.x402.rs — counts calls, answers canned."""

    def __init__(
        self,
        *,
        verify_valid=True,
        verify_reason=None,
        settle_ok=True,
        settle_reason=None,
        fail_verify=False,
        fail_settle=False,
    ):
        self.verify_calls = 0
        self.settle_calls = 0
        self.verify_valid = verify_valid
        self.verify_reason = verify_reason
        self.settle_ok = settle_ok
        self.settle_reason = settle_reason
        self.fail_verify = fail_verify
        self.fail_settle = fail_settle

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/verify":
            self.verify_calls += 1
            if self.fail_verify:
                raise httpx.ConnectError("facilitator down", request=request)
            body = {"isValid": self.verify_valid, "payer": "Payer111111111111111111111111111111111"}
            if self.verify_reason:
                body["invalidReason"] = self.verify_reason
            return httpx.Response(200, json=body)
        if request.url.path == "/settle":
            self.settle_calls += 1
            if self.fail_settle:
                raise httpx.ConnectError("facilitator down", request=request)
            body = {"success": self.settle_ok, "transaction": SIGNATURE, "network": "solana"}
            if self.settle_reason:
                body["errorReason"] = self.settle_reason
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"error": "not found"})


@pytest.fixture
def fake():
    return FakeFacilitator()


@pytest.fixture
def paid_gate(tmp_path, fake):
    transport = httpx.MockTransport(fake.handler)
    gate = X402Gate(
        payee=PAYEE,
        db_path=tmp_path / "test.db",
        http_client=httpx.Client(transport=transport),
    )
    return gate


@pytest.fixture
def paid_client(paid_gate, monkeypatch):
    monkeypatch.setattr(api_module, "x402_gate", paid_gate)
    return TestClient(app)


def _payment_record(tmp_path_db):
    with db.connect(tmp_path_db) as conn:
        return db.get_payment(conn, SIGNATURE)


# -- config ------------------------------------------------------------

def test_amount_conversion():
    assert usdc_to_base_units("0.001") == 1000
    assert usdc_to_base_units("0.01") == 10000
    assert usdc_to_base_units("1") == 1000000
    assert usdc_to_base_units("0.000001") == 1
    for bad in ("0", "-0.001", "abc", "", "0.0000001"):
        with pytest.raises(ValueError):
            usdc_to_base_units(bad)


def test_gate_disabled_without_payee():
    gate = X402Gate()
    assert gate.enabled is False
    assert gate.authorize({}, "/check") == {
        "paid": False,
        "endpoint": "/check",
        "signature": None,
    }


def test_requirement_shape(paid_gate):
    req = paid_gate.requirement("/check")
    assert req["scheme"] == "exact"
    assert req["network"] == "solana"
    assert req["amount"] == "1000"
    assert req["asset"] == USDC_MINT
    assert req["payTo"] == PAYEE
    assert req["payee"] == PAYEE
    assert req["maxTimeoutSeconds"] == 60
    assert "/check" in req["description"]


# -- free mode (no payee): behaviour identical to before v2.18 --------

def test_free_mode_check_and_wallet_serve():
    client = TestClient(app)
    assert client.get("/check", params={"dev": FLAGGED}).status_code == 200
    assert client.get(f"/wallets/{FLAGGED}").status_code == 200
    assert client.get("/health").status_code == 200


# -- paid mode: 402 without payment ------------------------------------

def test_paid_check_without_header_is_402(paid_client):
    r = paid_client.get("/check", params={"dev": FLAGGED})
    assert r.status_code == 402
    body = r.json()["detail"]
    assert body["x402Version"] == 1
    assert body["accepts"][0]["amount"] == "1000"
    assert body["accepts"][0]["asset"] == USDC_MINT
    assert body["accepts"][0]["payee"] == PAYEE
    assert "/check" in body["accepts"][0]["description"]


def test_paid_wallet_without_header_is_402(paid_client):
    r = paid_client.get(f"/wallets/{FLAGGED}")
    assert r.status_code == 402
    body = r.json()["detail"]
    assert f"/wallets/{FLAGGED}" in body["accepts"][0]["description"]


def test_paid_free_endpoints_stay_free(paid_client):
    assert paid_client.get("/health").status_code == 200
    assert paid_client.get("/registry").status_code == 200
    assert paid_client.get("/clusters/cluster-sample-a1").status_code == 200
    assert paid_client.get("/").status_code == 200


def test_bad_payment_header_is_402(paid_client):
    r = paid_client.get(
        "/check", params={"dev": FLAGGED}, headers={"X-Payment": "not-json!!!"}
    )
    assert r.status_code == 402
    assert "Invalid X-Payment header" in r.json()["detail"]["error"]


# -- paid mode: verify / settle ----------------------------------------

def test_verify_invalid_is_402(paid_client, fake):
    fake.verify_valid = False
    fake.verify_reason = "bad signature"
    r = paid_client.get(
        "/check",
        params={"dev": FLAGGED},
        headers={"X-Payment": payment_header()},
    )
    assert r.status_code == 402
    assert "bad signature" in r.json()["detail"]["error"]
    assert fake.settle_calls == 0  # never settled an unverified payment


def test_verify_ok_settle_ok_serves_and_records(paid_client, paid_gate, fake):
    r = paid_client.get(
        "/check",
        params={"dev": FLAGGED},
        headers={"X-Payment": payment_header()},
    )
    assert r.status_code == 200
    assert r.json()["flagged"] is True
    assert fake.verify_calls == 1
    assert fake.settle_calls == 1
    record = _payment_record(paid_gate.db_path)
    assert record is not None
    assert record["signature"] == SIGNATURE
    assert record["amount_base_units"] == 1000
    assert record["endpoint"] == "/check"
    assert record["payer"] is not None


def test_same_signature_twice_settles_once(paid_client, fake):
    headers = {"X-Payment": payment_header()}
    assert paid_client.get("/check", params={"dev": FLAGGED}, headers=headers).status_code == 200
    assert paid_client.get("/check", params={"dev": FLAGGED}, headers=headers).status_code == 200
    assert fake.settle_calls == 1
    assert fake.verify_calls == 1  # second request served from the DB record


def test_replay_on_other_endpoint_is_not_free(paid_client, fake):
    headers = {"X-Payment": payment_header()}
    assert paid_client.get("/check", params={"dev": FLAGGED}, headers=headers).status_code == 200
    # Same payment on /wallets must go through verify/settle again.
    assert paid_client.get(f"/wallets/{FLAGGED}", headers=headers).status_code == 200
    assert fake.settle_calls == 2
    assert fake.verify_calls == 2


def test_settle_fails_is_402(paid_client, fake):
    fake.settle_ok = False
    fake.settle_reason = "insufficient funds"
    r = paid_client.get(
        "/check",
        params={"dev": FLAGGED},
        headers={"X-Payment": payment_header()},
    )
    assert r.status_code == 402
    assert "insufficient funds" in r.json()["detail"]["error"]


# -- paid mode: payload must match the requirement ----------------------

def test_wrong_amount_is_402(paid_client):
    r = paid_client.get(
        "/check",
        params={"dev": FLAGGED},
        headers={"X-Payment": payment_header(amount="999")},
    )
    assert r.status_code == 402
    assert "amount" in r.json()["detail"]["error"]


def test_wrong_asset_is_402(paid_client):
    r = paid_client.get(
        "/check",
        params={"dev": FLAGGED},
        headers={"X-Payment": payment_header(asset="OtherMint1111111111111111111111111111111")},
    )
    assert r.status_code == 402
    assert "asset" in r.json()["detail"]["error"]


def test_wrong_payee_is_402(paid_client):
    r = paid_client.get(
        "/check",
        params={"dev": FLAGGED},
        headers={"X-Payment": payment_header(payTo=OTHER_PAYEE)},
    )
    assert r.status_code == 402
    assert "payee" in r.json()["detail"]["error"]


def test_wrong_network_is_402(paid_client):
    payload = {
        "x402Version": 1,
        "scheme": "exact",
        "network": "solana-devnet",
        "payload": {"signature": SIGNATURE},
    }
    header = base64.b64encode(json.dumps(payload).encode()).decode()
    r = paid_client.get(
        "/check", params={"dev": FLAGGED}, headers={"X-Payment": header}
    )
    assert r.status_code == 402
    assert "network" in r.json()["detail"]["error"]


# -- paid mode: facilitator outage --------------------------------------

def test_facilitator_down_on_verify_is_402(paid_client, fake):
    fake.fail_verify = True
    r = paid_client.get(
        "/check",
        params={"dev": FLAGGED},
        headers={"X-Payment": payment_header()},
    )
    assert r.status_code == 402
    assert "payment verification unavailable" in r.json()["detail"]["error"]


def test_facilitator_down_on_settle_is_402(paid_client, fake):
    fake.fail_settle = True
    r = paid_client.get(
        "/check",
        params={"dev": FLAGGED},
        headers={"X-Payment": payment_header()},
    )
    assert r.status_code == 402
    assert "payment settlement unavailable" in r.json()["detail"]["error"]


# -- db: payments table -------------------------------------------------

def test_payments_table_dedups(tmp_path):
    conn = db.init_db(tmp_path / "pay.db")
    assert db.record_payment(conn, "sig1", 1000, "/check") is True
    assert db.record_payment(conn, "sig1", 1000, "/check") is False
    assert db.get_payment(conn, "sig1")["endpoint"] == "/check"
    assert db.get_payment(conn, "nope") is None
    assert db.count_table(conn, "payments") == 1
    assert db.schema_version(conn) == db.SCHEMA_VERSION
