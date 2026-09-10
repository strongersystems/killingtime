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
    assert tier["killed"] == 2 and tier["killed_all_time"] == 3  # Dimensius was killed after the season cut-off
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


def test_performance_page_covers_every_team_and_filter(client):
    """The whole point is one place to compare teams, so the filters have to stack and the page has to render."""
    from killingtime import metrics

    conn = client.app.state.conn
    everyone = metrics.team_performance(conn)
    assert everyone["parses"] and everyone["players"] and everyone["bosses"]
    top = everyone["players"][0]
    assert 0 <= top["median"] <= 100 and top["best"] >= top["median"]
    assert top["bosses"], "a raider needs their per-boss breakdown, that is the Warcraft Logs view"

    # Filters narrow rather than widen, and stack.
    one_boss = everyone["bosses"][0]
    just_that = metrics.team_performance(conn, encounter_id=one_boss["id"])
    assert [b["id"] for b in just_that["bosses"]] == [one_boss["id"]]
    assert just_that["parses"] <= everyone["parses"]
    mythic = metrics.team_performance(conn, difficulty=5)
    assert mythic["parses"] <= everyone["parses"]

    opts = metrics.performance_filters(conn)
    assert opts["tiers"] and opts["bosses"]

    r = client.get("/t/guild/performance")
    assert r.status_code == 200 and "Performance" in r.text
    r = client.get(f"/t/guild/performance?d=5&boss={one_boss['id']}&m=median")
    assert r.status_code == 200
    assert client.get("/api/team-performance?difficulty=5").status_code == 200


def test_patch_filter_appears_only_once_partitions_are_synced(client):
    """A patch filter has to come from Warcraft Logs partitions, never from a guess about dates."""
    from killingtime import metrics

    conn = client.app.state.conn
    # The sync records the partitions the zone actually has.
    assert conn.execute("SELECT COUNT(*) c FROM zone_partitions").fetchone()["c"] > 0

    # No parse carries one yet, so no patch is offered and the select is not on the page.
    conn.execute("UPDATE parses SET partition = NULL")
    conn.commit()
    assert metrics.performance_filters(conn)["patches"] == []
    assert 'name="p"' not in client.get("/t/guild/performance").text

    # Tag one tier's parses and the filter appears, named from that zone's partition list.
    zone = conn.execute("SELECT zone_id FROM v_parses WHERE rank_percent IS NOT NULL LIMIT 1").fetchone()["zone_id"]
    conn.execute("UPDATE parses SET partition = 2 WHERE encounter_id IN "
                 "(SELECT id FROM encounters WHERE zone_id = ?)", (zone,))
    conn.commit()
    patches = metrics.performance_filters(conn)["patches"]
    assert [p["name"] for p in patches] == ["12.1"] and patches[0]["parses"] > 0

    page = client.get("/t/guild/performance").text
    assert 'name="p"' in page and "12.1" in page

    # The option count is every parse in that partition; the page drops non-raiders, so compare like with like.
    assert metrics.team_performance(conn, partition=2, include_pugs=True)["parses"] == patches[0]["parses"]
    assert 0 < metrics.team_performance(conn, partition=2)["parses"] <= patches[0]["parses"]
    assert metrics.team_performance(conn, partition=99)["parses"] == 0
    assert client.get("/t/guild/performance?p=2").status_code == 200
    assert client.get("/api/team-performance?patch=2").status_code == 200
