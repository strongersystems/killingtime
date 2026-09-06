"""Web routes render against the synced fixture database."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from killingtime.db import connect
from killingtime.web.app import create_app


@pytest.fixture
def client(synced):
    conn, *_, settings = synced
    app = create_app(settings, conn)
    return TestClient(app)


def test_pages_render(client):
    for path in ["/", "/?zone=44&difficulty=5", "/tiers", "/tiers?difficulty=4", "/rivals",
                 "/rivals?raid=the-venomous-abyss&difficulty=4", "/attendance", "/nights", "/ask", "/status"]:
        r = client.get(path)
        assert r.status_code == 200, path
    home = client.get("/").text
    assert "The Venomous Abyss" in home and "Entombed Sentinels" in home
    tiers = client.get("/tiers").text
    assert "Manaforge Omega" in tiers
    rivals = client.get("/rivals").text
    assert "Internet Diff" in rivals and "Realm standings" in rivals


def test_api_endpoints(client):
    ov = client.get("/api/overview").json()
    assert ov["current_tier"]["id"] == 46
    tier = client.get("/api/tier/44?difficulty=5").json()
    assert tier["killed"] == 3
    riv = client.get("/api/rivals/the-venomous-abyss?difficulty=5").json()
    assert riv["guilds"][0]["is_home"] is True
    assert client.get("/healthz").json()["ok"] is True


def test_ask_requires_key(synced, tmp_path):
    conn, *_, settings = synced
    app = create_app(settings.model_copy(update={"anthropic_api_key": ""}), conn)
    r = TestClient(app).post("/api/ask", json={"question": "hello there"})
    assert r.status_code == 503


def test_empty_database_shows_onboarding(tmp_path, settings):
    conn = connect(str(tmp_path / "empty.db"))
    app = create_app(settings, conn)
    r = TestClient(app).get("/")
    assert r.status_code == 200 and "No raid data yet" in r.text
