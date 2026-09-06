"""Warcraft Logs client against a mocked HTTP transport."""

from __future__ import annotations

import json

import httpx
import pytest

from killingtime.wcl import WCLClient, WCLError


def make_client(handler, tmp_path):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return WCLClient("cid", "csecret", token_url="https://wcl.test/oauth/token", api_url="https://wcl.test/api/v2/client",
                     token_cache_path=str(tmp_path / "tok.json"), http=http)


def test_token_flow_and_query(tmp_path):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/oauth/token":
            assert request.headers["authorization"].startswith("Basic ")
            assert b"grant_type=client_credentials" in request.content
            return httpx.Response(200, json={"access_token": "tok123", "expires_in": 3600, "token_type": "Bearer"})
        assert request.headers["authorization"] == "Bearer tok123"
        body = json.loads(request.content)
        assert "rateLimitData" in body["query"]
        return httpx.Response(200, json={"data": {"rateLimitData": {"limitPerHour": 3600, "pointsSpentThisHour": 1.5, "pointsResetIn": 100}}})

    c = make_client(handler, tmp_path)
    rl = c.rate_limit()
    assert rl["limitPerHour"] == 3600
    # second query reuses the cached token (no second token request)
    c.rate_limit()
    assert sum(1 for r in seen if r.url.path == "/oauth/token") == 1
    # token cache was written and is reused by a fresh client
    c2 = make_client(handler, tmp_path)
    c2.rate_limit()
    assert sum(1 for r in seen if r.url.path == "/oauth/token") == 1


def test_bad_credentials(tmp_path):
    def handler(request):
        return httpx.Response(401, json={"error": "invalid_client"})

    with pytest.raises(WCLError, match="token request failed"):
        make_client(handler, tmp_path).rate_limit()


def test_graphql_error_and_rate_limit(tmp_path):
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json={"errors": [{"message": "Cannot query field foo"}]})
        return httpx.Response(429, text="Too Many Attempts.")

    c = make_client(handler, tmp_path)
    with pytest.raises(WCLError, match="Cannot query field"):
        c.query("{ foo }")
    with pytest.raises(WCLError, match="rate limit"):
        c.query("{ foo }")


def test_report_fights_batches_with_aliases(tmp_path):
    def handler(request):
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        q = json.loads(request.content)["query"]
        assert 'r0: report(code: "AAA")' in q and 'r1: report(code: "BBB")' in q
        assert "killType: Encounters" in q
        return httpx.Response(200, json={"data": {"reportData": {
            "r0": {"code": "AAA", "fights": [{"id": 1, "encounterID": 5, "kill": True}]},
            "r1": None}}})

    out = make_client(handler, tmp_path).report_fights(["AAA", "BBB"])
    assert out["AAA"][0]["encounterID"] == 5
    assert out["BBB"] == []


def test_missing_credentials():
    with pytest.raises(WCLError):
        WCLClient("", "")
