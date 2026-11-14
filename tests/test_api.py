from fastapi.testclient import TestClient

from src.api import app

client = TestClient(app)

FLAGGED = "SAMPLEDEV11111111111111111111111111111111"
UNKNOWN = "Unknown111111111111111111111111111111"


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["sample_data"] is True
    assert body["entries"] == 4
    assert body["flagged"] == 2


def test_registry():
    r = client.get("/registry")
    assert r.status_code == 200
    assert r.json()["sample_data"] is True


def test_wallet_found_and_not_found():
    assert client.get(f"/wallets/{FLAGGED}").status_code == 200
    assert client.get(f"/wallets/{FLAGGED}").json()["currently_flagged"] is True
    assert client.get(f"/wallets/{UNKNOWN}").status_code == 404


def test_check_flagged_and_unknown():
    r = client.get("/check", params={"dev": FLAGGED})
    assert r.status_code == 200
    assert r.json()["flagged"] is True
    assert r.json()["risk_level"] == "high"

    r = client.get("/check", params={"dev": UNKNOWN, "mint": "SAMPLEMNT1111111111111111111111111111111"})
    assert r.status_code == 200
    assert r.json()["flagged"] is False
    assert r.json()["risk_level"] == "unknown"


def test_check_requires_dev():
    assert client.get("/check").status_code == 422


def test_check_and_wallet_carry_chain_field():
    r = client.get("/check", params={"dev": FLAGGED})
    assert r.status_code == 200
    # Registry verdict data is Solana-only; the field defaults to solana.
    assert r.json()["chain"] == "solana"

    r = client.get(f"/wallets/{FLAGGED}")
    assert r.status_code == 200
    assert r.json()["chain"] == "solana"


def test_networks_lists_machine_as_local_testnet():
    r = client.get("/networks")
    assert r.status_code == 200
    networks = {n["chain"]: n for n in r.json()["networks"]}
    assert set(networks) == {"solana", "base", "machine"}
    machine = networks["machine"]
    assert machine["status"] == "local testnet"
    assert machine["registry_coverage"] is False
    assert machine["probe"] == "unavailable"
    assert machine["genesis_hash"] == "32W8ouqrHbuhE3PM6ZsJrJcDbP7Tx9aC2zRvrEabkQSb"
    assert networks["solana"]["registry_coverage"] is True

    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["chain"] == "solana"
    assert r.json()["networks"] == ["base", "machine", "solana"]


def test_cluster_found_and_not_found():
    r = client.get("/clusters/cluster-sample-a1")
    assert r.status_code == 200
    assert len(r.json()["wallets"]) == 2
    assert client.get("/clusters/cluster-nope").status_code == 404
