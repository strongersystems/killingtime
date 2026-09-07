"""Shared fixtures: a synthetic two-tier guild history served by fake WCL / Raider.IO clients."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest

from killingtime.config import Settings
from killingtime.db import connect
from killingtime.sync import run_sync

DAY = 86_400_000


def ms(date: str, hour: int = 19) -> int:
    return int(datetime.fromisoformat(f"{date}T{hour:02d}:00:00+00:00").timestamp() * 1000)


def iso(t: int) -> str:
    return datetime.fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


# --- world data ------------------------------------------------------------------------------
ZONES = [
    {"id": 44, "name": "Manaforge Omega", "frozen": True, "expansion": {"id": 6},
     "encounters": [{"id": 3129, "name": "Plexus Sentinel"}, {"id": 3131, "name": "Loom'ithar"}, {"id": 3133, "name": "Dimensius"}]},
    {"id": 46, "name": "The Venomous Abyss", "frozen": False, "expansion": {"id": 7},
     "encounters": [{"id": 3201, "name": "Nek'zali the Soulcoiler"}, {"id": 3202, "name": "Entombed Sentinels"}, {"id": 3203, "name": "Ulatek"}]},
    # A Mythic+ season: Warcraft Logs lists it as a zone, but it is not a raid and must be skipped (with its reports).
    {"id": 47, "name": "Mythic+ Season 1", "frozen": False, "expansion": {"id": 7}, "difficulties": [{"id": 10, "name": "Dungeon", "sizes": [5]}],
     "encounters": [{"id": 12000, "name": "Ara-Kara"}, {"id": 12001, "name": "The Dawnbreaker"}]},
]
EXPANSIONS = [{"id": 7, "name": "Midnight"}, {"id": 6, "name": "The War Within"}]


def make_report(code: str, zone_id: int, date: str, pulls: list[tuple[int, int, bool, float]], offset_s: int = 0) -> tuple[dict, list[dict]]:
    """pulls: (encounter_id, difficulty, kill, fight_pct). Fights are 5 minutes each, back to back.
    ``offset_s`` shifts the whole report, to fake a second person logging the same raid."""
    start = ms(date) + offset_s * 1000
    fights = []
    t = 0
    for i, (enc, diff, kill, pct) in enumerate(pulls, start=1):
        fights.append({"id": i, "encounterID": enc, "name": "x", "difficulty": diff, "kill": kill,
                       "startTime": t, "endTime": t + 300_000, "bossPercentage": pct, "fightPercentage": pct,
                       "lastPhase": 1, "size": 20, "averageItemLevel": 300.0})
        t += 360_000
    # a trash fight that must be ignored
    fights.append({"id": 99, "encounterID": 0, "name": "trash", "difficulty": None, "kill": None,
                   "startTime": t, "endTime": t + 1000, "bossPercentage": None, "fightPercentage": None})
    report = {"code": code, "title": f"Raid {date}", "startTime": start, "endTime": start + t + 1000,
              "zone": {"id": zone_id, "name": "z"}, "owner": {"name": "Matt"}}
    return report, fights


# Previous tier (44): mythic boss 1 killed in 4 pulls night 1, boss 2 killed night 2 after 6 pulls, boss 3 killed night 3 (10 pulls total across 2 nights).
PREV = [
    make_report("P1", 44, "2026-05-05", [(3129, 5, False, 40.0), (3129, 5, False, 12.5), (3129, 5, False, 3.0), (3129, 5, True, 0.0)]),
    make_report("P2", 44, "2026-05-07", [(3131, 5, False, 70.0)] * 5 + [(3131, 5, True, 0.0)]),
    make_report("P3", 44, "2026-05-12", [(3133, 5, False, 55.0)] * 6),
    make_report("P4", 44, "2026-05-14", [(3133, 5, False, 20.0)] * 3 + [(3133, 5, True, 0.0), (3133, 5, True, 0.0)]),
]
# Current tier (46): heroic clear + mythic boss 1 killed, boss 2 in progress.
CUR = [
    make_report("C1", 46, "2026-08-23", [(3201, 4, True, 0.0), (3202, 4, False, 30.0), (3202, 4, True, 0.0), (3203, 4, True, 0.0)]),
    make_report("C2", 46, "2026-08-30", [(3201, 5, False, 60.0), (3201, 5, False, 25.0), (3201, 5, True, 0.0), (3202, 5, False, 80.0)]),
    make_report("C3", 46, "2026-09-02", [(3202, 5, False, 66.0), (3202, 5, False, 41.2), (3202, 5, False, 38.0)]),
    # A second logger's copy of C2 (clock 20 s behind, one wipe cut short): every pull is a duplicate and must not count twice.
    make_report("C2B", 46, "2026-08-30", [(3201, 5, False, 60.0), (3201, 5, False, 25.0), (3201, 5, True, 0.0), (3202, 5, False, 80.0)], offset_s=-20),
]
REPORTS = {r["code"]: r for r, _ in PREV + CUR}
FIGHTS = {r["code"]: f for r, f in PREV + CUR}
# Rosters (see the settings fixture): CE Team = Tagrik, Elelena; 6 Hour Team = Bubonic, Sixer.
# C1 (heroic clear) is the 6 Hour Team's raid, C2/C3 (mythic) are the CE Team's.
ATTENDANCE = {
    46: [{"code": "C1", "startTime": ms("2026-08-23"), "zone": {"id": 46},
          "players": [{"name": "Tagrik", "type": "Warrior", "presence": 1}, {"name": "Bubonic", "type": "DeathKnight", "presence": 1},
                      {"name": "Sixer", "type": "Priest", "presence": 1}]},
         {"code": "C2", "startTime": ms("2026-08-30"), "zone": {"id": 46},
          "players": [{"name": "Tagrik", "type": "Warrior", "presence": 1}, {"name": "Elelena", "type": "Mage", "presence": 1}]},
         {"code": "C2B", "startTime": ms("2026-08-30") - 20_000, "zone": {"id": 46},
          "players": [{"name": "Tagrik", "type": "Warrior", "presence": 1}, {"name": "Elelena", "type": "Mage", "presence": 1}]},
         {"code": "C3", "startTime": ms("2026-09-02"), "zone": {"id": 46},
          "players": [{"name": "Tagrik", "type": "Warrior", "presence": 1}, {"name": "Elelena", "type": "Mage", "presence": 1},
                      {"name": "Bubonic", "type": "DeathKnight", "presence": 2}]}],
}


def fake_rankings(code: str, metric: str) -> list[dict]:
    """Parses for every kill fight in a report: two guild members and a pug per role bucket."""
    out = []
    for f in FIGHTS.get(code, []):
        if not f.get("kill") or not f.get("encounterID"):
            continue
        srv = lambda n: {"id": 1, "name": n, "region": "EU"}  # noqa: E731
        if metric == "dps":
            roles = {
                "tanks": {"characters": [{"name": "Bubonic", "server": srv("Draenor"), "class": "DeathKnight", "spec": "Blood", "amount": 90000.0, "rankPercent": 50, "bracketPercent": 55}]},
                "healers": {"characters": [{"name": "Sixer", "server": srv("Draenor"), "class": "Priest", "spec": "Holy", "amount": 1000.0, "rankPercent": 1, "bracketPercent": 1}]},
                "dps": {"characters": [
                    {"name": "Tagrik", "server": srv("Draenor"), "class": "Warrior", "spec": "Arms", "amount": 180000.0, "rankPercent": 80, "bracketPercent": 85},
                    {"name": "Elelena", "server": srv("Draenor"), "class": "Mage", "spec": "Frost", "amount": 170000.0, "rankPercent": 60, "bracketPercent": 62},
                    {"name": "Puggy", "server": srv("Silvermoon"), "class": "Hunter", "spec": "Marksmanship", "amount": 200000.0, "rankPercent": 95, "bracketPercent": 96},
                ]},
            }
        else:
            roles = {
                "tanks": {"characters": []},
                "healers": {"characters": [{"name": "Sixer", "server": srv("Draenor"), "class": "Priest", "spec": "Holy", "amount": 150000.0, "rankPercent": 70, "bracketPercent": 72}]},
                "dps": {"characters": []},
            }
        out.append({"fightID": f["id"], "encounter": {"id": f["encounterID"], "name": "x"}, "difficulty": f["difficulty"], "kill": True, "roles": roles})
    return out


class FakeWCL:
    def __init__(self) -> None:
        self.queries_made = 0
        self.fight_batches: list[list[str]] = []

    def expansions(self):
        self.queries_made += 1
        return EXPANSIONS

    def zones(self, expansion_id):
        self.queries_made += 1
        return [z for z in ZONES if z["expansion"]["id"] == expansion_id]

    def guild(self, name, server_slug, server_region):
        self.queries_made += 1
        if name.lower() != "killing time":
            return None
        return {"id": 637454, "name": "Killing Time", "faction": {"name": "Horde"},
                "server": {"name": "Draenor", "slug": "draenor", "region": {"compactName": "EU", "slug": "eu"}}}

    def all_reports(self, guild_id, start_time=None, zone_id=None):
        self.queries_made += 1
        reps = [r for r in REPORTS.values() if start_time is None or r["endTime"] >= start_time]
        # an untracked dungeon report should be ignored, and so should one for a Mythic+ season zone we know about
        reps.append({"code": "DUN", "title": "M+", "startTime": ms("2026-09-01"), "endTime": ms("2026-09-01") + 1, "zone": {"id": 999, "name": "Dungeons"}, "owner": None})
        reps.append({"code": "DUN2", "title": "M+ keys", "startTime": ms("2026-09-01"), "endTime": ms("2026-09-01") + 1, "zone": {"id": 47, "name": "Mythic+ Season 1"}, "owner": None})
        return reps

    def report_fights(self, codes, batch=8):
        self.queries_made += 1
        self.fight_batches.append(list(codes))
        return {c: FIGHTS[c] for c in codes}

    def guild_zone_ranking(self, guild_id, zone_id, difficulty=5):
        self.queries_made += 1
        if zone_id == 44:
            from killingtime.wcl import WCLError
            raise WCLError("GraphQL error: zone is frozen")
        return {"progress": {"worldRank": {"number": 890, "percentile": 91.2}, "regionRank": {"number": 424, "percentile": 90.0}, "serverRank": {"number": 35, "percentile": 80.0}},
                "speed": None, "completeRaidSpeed": {"worldRank": {"number": 1200, "percentile": 50.0}, "regionRank": {"number": 600, "percentile": 50.0}, "serverRank": {"number": 40, "percentile": 50.0}}}

    def all_attendance(self, guild_id, zone_id=None, max_pages=20):
        self.queries_made += 1
        return ATTENDANCE.get(zone_id, [])

    def report_rankings(self, code, metric="dps"):
        self.queries_made += 1
        return fake_rankings(code, metric)

    def rate_limit(self):
        self.queries_made += 1
        return {"limitPerHour": 3600, "pointsSpentThisHour": 42.5, "pointsResetIn": 1800}


RIO_STATIC = {
    11: {"raids": [
        {"id": 16915, "slug": "the-venomous-abyss", "name": "The Venomous Abyss",
         "encounters": [{"slug": "nekzali-the-soulcoiler", "name": "Nek'zali the Soulcoiler"}, {"slug": "entombed-sentinels", "name": "Entombed Sentinels"}, {"slug": "ulatek", "name": "Ulatek"}]},
        {"id": 8062, "slug": "sporefall", "name": "Sporefall", "encounters": [{"slug": "rotmire", "name": "Rotmire"}]},
    ]},
    10: {"raids": [
        {"id": 16178, "slug": "manaforge-omega", "name": "Manaforge Omega",
         "encounters": [{"slug": "plexus-sentinel", "name": "Plexus Sentinel"}, {"slug": "loomithar", "name": "Loom'ithar"}, {"slug": "dimensius", "name": "Dimensius"}]},
    ]},
}


def rio_guild(name: str, gid: int, faction: str = "horde", realm: str = "draenor") -> dict:
    return {"id": gid, "name": name, "faction": faction, "realm": {"slug": realm, "name": realm.title()}, "region": {"slug": "eu", "name": "Europe"}}


def rio_entry(rank: int, name: str, gid: int, kills: list[tuple[str, str]], pulled: list[tuple[str, int, float, bool]]) -> dict:
    return {
        "rank": rank, "regionRank": rank * 12, "guild": rio_guild(name, gid),
        "encountersDefeated": [{"slug": s, "firstDefeated": d, "lastDefeated": d} for s, d in kills],
        "encountersPulled": [{"id": 1, "slug": s, "numPulls": n, "bestPercent": p, "isDefeated": k} for s, n, p, k in pulled],
    }


RIO_RANKINGS = {
    ("the-venomous-abyss", "mythic"): [
        rio_entry(1, "Internet Diff", 1, [("nekzali-the-soulcoiler", "2026-08-20T10:35:18.000Z"), ("entombed-sentinels", "2026-08-20T10:51:36.000Z"), ("ulatek", "2026-08-23T13:16:14.000Z")],
                  [("nekzali-the-soulcoiler", 2, 0, True), ("entombed-sentinels", 5, 0, True), ("ulatek", 40, 0, True)]),
        rio_entry(2, "Advance", 2, [("nekzali-the-soulcoiler", "2026-08-21T19:00:00.000Z"), ("entombed-sentinels", "2026-08-25T19:00:00.000Z")],
                  [("nekzali-the-soulcoiler", 3, 0, True), ("entombed-sentinels", 12, 0, True), ("ulatek", 30, 45.5, False)]),
        rio_entry(3, "Killing Time", 637454, [("nekzali-the-soulcoiler", "2026-08-30T19:20:00.000Z")],
                  [("nekzali-the-soulcoiler", 3, 0, True), ("entombed-sentinels", 4, 38.0, False)]),
    ],
    ("the-venomous-abyss", "heroic"): [
        rio_entry(1, "Internet Diff", 1, [("nekzali-the-soulcoiler", "2026-08-20T10:00:00.000Z"), ("entombed-sentinels", "2026-08-20T10:20:00.000Z"), ("ulatek", "2026-08-20T11:00:00.000Z")], []),
        rio_entry(2, "Killing Time", 637454, [("nekzali-the-soulcoiler", "2026-08-23T19:00:00.000Z"), ("entombed-sentinels", "2026-08-23T19:30:00.000Z"), ("ulatek", "2026-08-23T20:00:00.000Z")], []),
    ],
    ("the-venomous-abyss", "normal"): [],
    ("manaforge-omega", "mythic"): [rio_entry(1, "Killing Time", 637454, [("plexus-sentinel", "2026-05-05T19:30:00.000Z"), ("loomithar", "2026-05-07T19:40:00.000Z"), ("dimensius", "2026-05-14T19:30:00.000Z")], [])],
    ("manaforge-omega", "heroic"): [],
    ("manaforge-omega", "normal"): [],
}

RIO_PROFILES = {
    "killing time": {"name": "Killing Time", "faction": "horde", "region": "eu", "realm": "Draenor",
                     "raid_rankings": {"the-venomous-abyss": {"normal": {"world": 0, "region": 0, "realm": 0}, "heroic": {"world": 1904, "region": 893, "realm": 61}, "mythic": {"world": 2500, "region": 1200, "realm": 3}},
                                       "manaforge-omega": {"normal": {"world": 0, "region": 0, "realm": 0}, "heroic": {"world": 100, "region": 50, "realm": 2}, "mythic": {"world": 890, "region": 424, "realm": 1}},
                                       "sporefall": {"normal": {"world": 0, "region": 0, "realm": 0}, "heroic": {"world": 0, "region": 0, "realm": 0}, "mythic": {"world": 0, "region": 0, "realm": 0}}},
                     "raid_progression": {"the-venomous-abyss": {"summary": "3/3 H", "total_bosses": 3, "normal_bosses_killed": 3, "heroic_bosses_killed": 3, "mythic_bosses_killed": 1},
                                          "manaforge-omega": {"summary": "3/3 M", "total_bosses": 3, "normal_bosses_killed": 3, "heroic_bosses_killed": 3, "mythic_bosses_killed": 3},
                                          "sporefall": {"summary": "1/1 M", "total_bosses": 1, "normal_bosses_killed": 0, "heroic_bosses_killed": 1, "mythic_bosses_killed": 1}}},
    "internet diff": {"name": "Internet Diff", "faction": "horde", "region": "eu", "realm": "Draenor",
                      "raid_rankings": {"the-venomous-abyss": {"normal": {"world": 0, "region": 0, "realm": 0}, "heroic": {"world": 50, "region": 20, "realm": 1}, "mythic": {"world": 300, "region": 100, "realm": 1}}},
                      "raid_progression": {"the-venomous-abyss": {"summary": "3/3 M", "total_bosses": 3, "normal_bosses_killed": 3, "heroic_bosses_killed": 3, "mythic_bosses_killed": 3}}},
}


class FakeRIO:
    def __init__(self) -> None:
        self.requests_made = 0
        self.calls: list[tuple[str, Any]] = []

    def guild_profile(self, region, realm, name, fields=""):
        self.requests_made += 1
        self.calls.append(("profile", name))
        from killingtime.raiderio import RaiderIOError
        try:
            return RIO_PROFILES[name.lower()]
        except KeyError:
            raise RaiderIOError(f"guild {name} not found") from None

    def static_data(self, expansion_id):
        self.requests_made += 1
        from killingtime.raiderio import RaiderIOError
        if expansion_id not in RIO_STATIC:
            raise RaiderIOError("bad expansion")
        return RIO_STATIC[expansion_id]

    def raid_rankings(self, raid, difficulty, region, realm=None, page=0, limit=100):
        self.requests_made += 1
        self.calls.append(("rankings", (raid, difficulty, realm, page)))
        if page > 0:
            return []
        return RIO_RANKINGS.get((raid, difficulty), [])


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        wcl_client_id="id", wcl_client_secret="secret",
        guild_name="Killing Time", guild_realm="Draenor", guild_region="EU",
        rival_guilds="Internet Diff@draenor/eu; Nope Guild@draenor/eu",
        raid_teams="CE Team: Tagrik, Elelena; 6 Hour Team: Bubonic, Sixer",
        rio_realm_scan_pages=1, sync_expansions=2,
        anthropic_api_key="test-key",
        kt_db_path=str(tmp_path / "kt.db"),
    )


@pytest.fixture
def synced(settings) -> tuple[sqlite3.Connection, FakeWCL, FakeRIO, Settings]:
    conn = connect(settings.kt_db_path)
    wcl, rio = FakeWCL(), FakeRIO()
    run_sync(conn, settings, wcl, rio, full=True, progress=lambda m: None)
    return conn, wcl, rio, settings
