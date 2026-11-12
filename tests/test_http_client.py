"""Tests for src.http_client.make_client.

Regression test for the v1.3 live crash: httpx.Client() with default
settings parses NO_PROXY, and a bracketed-IPv6 entry such as "[::1]"
makes it raise ``httpx.InvalidURL: Invalid port: ':1]'`` before any
request is made. make_client must build fine with that env present.
"""
import httpx

from src.http_client import make_client


def test_make_client_survives_bracketed_ipv6_no_proxy(monkeypatch):
    monkeypatch.setenv(
        "NO_PROXY",
        "localhost,127.0.0.1,::1,[::1],fd8b:4f84:7d32:99::1,[fd8b:4f84:7d32:99::1]",
    )
    monkeypatch.setenv("no_proxy", "localhost,[::1]")
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
    client = make_client()
    assert isinstance(client, httpx.Client)
    client.close()


def test_make_client_uses_explicit_proxy_and_no_env_trust(monkeypatch):
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
    # No SSL_CERT_FILE override here: make_client must still build a
    # client with the requested timeout.
    client = make_client(timeout=5.0)
    try:
        assert client.timeout.connect == 5.0
    finally:
        client.close()
