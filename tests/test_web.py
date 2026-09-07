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
    home = client.get("/").text  # no cookie yet -> team chooser
    assert "Which team?" in home and "CE Team" in home and "Whole guild" in home
    for path in ["/teams", "/t/guild/", "/t/guild/?tier=44&d=5", "/t/guild/history", "/t/guild/history?d=4", "/t/guild/realm",
                 "/t/guild/realm?raid=the-venomous-abyss&d=4", "/t/guild/roster", "/t/guild/nights", "/t/guild/peers", "/ask", "/status"]:
        r = client.get(path)
        assert r.status_code == 200, path
    guild = client.get("/t/guild/").text
    assert "The Venomous Abyss" in guild and "Entombed Sentinels" in guild
    tiers = client.get("/t/guild/history").text
    assert "Manaforge Omega" in tiers
    rivals = client.get("/t/guild/realm").text
    assert "Internet Diff" in rivals and "Realm standings" in rivals
    # old flat URLs redirect to the team layout
    r = client.get("/tiers?difficulty=4&team=CE+Team", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/t/ce-team/history?d=4"
    assert client.get("/t/nope/").status_code == 404


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
