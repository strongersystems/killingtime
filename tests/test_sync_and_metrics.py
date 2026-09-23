"""End-to-end sync into SQLite from the fake clients, then check the derived metrics."""

from __future__ import annotations

from killingtime import metrics
from killingtime.config import parse_rival_guilds
from killingtime.sync import match_name, run_sync, slugify


def test_parse_rivals():
    refs = parse_rival_guilds("Internet Diff@draenor/eu; Advance@Twisting Nether/EU")
    assert refs[0].name == "Internet Diff" and refs[0].realm_slug == "draenor" and refs[0].region == "eu"
    assert refs[1].realm_slug == "twisting-nether"


def test_slug_matching():
    assert slugify("Nek'zali the Soulcoiler") == "nekzali-the-soulcoiler"
    cands = {"fallenking-salhadaar": "Fallen King Salhadaar", "vorasius": "Vorasius", "the-coiled-altar": "The Coiled Altar"}
    assert match_name("Fallen King Salhadaar", cands) == "fallenking-salhadaar"
    assert match_name("Coiled Altar", cands) == "the-coiled-altar"
    assert match_name("Totally Different Boss", cands) is None


def test_sync_populates_tables(synced):
    conn, wcl, rio, settings = synced
    assert conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM encounters").fetchone()[0] == 6
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 8  # dungeon report ignored, second logger kept
    # trash fights (encounterID 0) are dropped
    assert conn.execute("SELECT COUNT(*) FROM fights WHERE encounter_id = 0").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM fights").fetchone()[0] == 4 + 6 + 6 + 5 + 4 + 4 + 3 + 4
    # the second logger's four pulls are duplicates of C2's and are hidden from every metric
    assert conn.execute("SELECT COUNT(*) FROM fights WHERE canonical = 0").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM v_pulls").fetchone()[0] == 4 + 6 + 6 + 5 + 4 + 4 + 3
    assert conn.execute("SELECT COUNT(*) FROM v_pulls WHERE report_code IN ('C2', 'C2B')").fetchone()[0] == 4
    home = metrics.home_guild(conn)
    assert home["wcl_id"] == 637454 and home["faction"] == "horde" and home["is_home"] == 1
    # attendance rows (fixture only has attendance for the current tier)
    assert conn.execute("SELECT COUNT(*) FROM attendance").fetchone()[0] == 10
    # the Mythic+ season zone (47) and its report were skipped
    assert conn.execute("SELECT COUNT(*) FROM zones WHERE id = 47").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM reports WHERE code = 'DUN2'").fetchone()[0] == 0
    # zone rankings: frozen zone raised a WCLError and was skipped, current zone stored
    ranks = conn.execute("SELECT metric, world_rank FROM wcl_zone_rankings WHERE zone_id = 46 ORDER BY metric").fetchall()
    assert [(r["metric"], r["world_rank"]) for r in ranks] == [("completeRaidSpeed", 1200), ("progress", 890)]
    # raider.io: static data resolved by probing expansion ids, zones mapped by name
    assert conn.execute("SELECT rio_raid_slug FROM zones WHERE id = 46").fetchone()[0] == "the-venomous-abyss"
    assert conn.execute("SELECT rio_raid_slug FROM zones WHERE id = 44").fetchone()[0] == "manaforge-omega"
    assert conn.execute("SELECT COUNT(*) FROM encounters WHERE rio_encounter_slug IS NULL").fetchone()[0] == 0
    # rival flagged, missing rival produced a warning not a crash
    assert conn.execute("SELECT is_rival FROM guilds WHERE name = 'Internet Diff'").fetchone()[0] == 1
    # world boss raid (1 encounter) is not scanned
    scanned = {c[1][0] for c in rio.calls if c[0] == "rankings"}
    assert scanned == {"the-venomous-abyss", "manaforge-omega"}
    log = conn.execute("SELECT status FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    assert log["status"] == "ok"


def test_incremental_sync_is_idempotent(synced):
    conn, wcl, rio, settings = synced
    before = conn.execute("SELECT COUNT(*) FROM fights").fetchone()[0]
    stats = run_sync(conn, settings, wcl, rio, full=False, progress=lambda m: None)
    assert conn.execute("SELECT COUNT(*) FROM fights").fetchone()[0] == before
    assert conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 8
    assert stats.fights == 0  # nothing re-fetched: all reports already had fights_synced_at


def test_first_kills_view(synced):
    conn, *_ = synced
    rows = {r["encounter_name"]: dict(r) for r in conn.execute(
        "SELECT * FROM v_first_kills WHERE zone_id = 44 AND difficulty = 5")}
    assert rows["Plexus Sentinel"]["pulls_to_kill"] == 4
    assert rows["Plexus Sentinel"]["wipes_before_kill"] == 3
    assert rows["Plexus Sentinel"]["best_wipe_pct"] == 3.0
    assert rows["Loom'ithar"]["pulls_to_kill"] == 6 and rows["Loom'ithar"]["nights_to_kill"] == 1
    # Dimensius: 6 wipes night 1, 3 wipes + kill night 2 -> 10 pulls to first kill; the re-kill is excluded
    assert rows["Dimensius"]["pulls_to_kill"] == 10 and rows["Dimensius"]["nights_to_kill"] == 2
    assert rows["Dimensius"]["first_kill_date"] == "2026-05-14"
    cur = {r["encounter_name"]: dict(r) for r in conn.execute(
        "SELECT * FROM v_first_kills WHERE zone_id = 46 AND difficulty = 5")}
    assert cur["Nek'zali the Soulcoiler"]["killed"] == 1 and cur["Nek'zali the Soulcoiler"]["pulls_to_kill"] == 3
    assert cur["Entombed Sentinels"]["killed"] == 0 and cur["Entombed Sentinels"]["pulls_to_kill"] == 4
    assert cur["Entombed Sentinels"]["best_wipe_pct"] == 38.0
    assert "Ulatek" not in cur


def test_tier_metrics(synced):
    conn, *_ = synced
    tiers = metrics.tiers(conn)
    assert [t["id"] for t in tiers] == [46, 44]
    # Dimensius died on 2026-05-14, after the season cut-off (2026-05-10), so the tier stands at 2/3
    assert tiers[1]["kills"] == {5: 2} and tiers[1]["summary"] == "2/3 M"
    assert tiers[1]["cutoff_date"] == "2026-05-10"
    assert tiers[0]["kills"] == {5: 1, 4: 3}
    s = metrics.tier_summary(conn, 46, 5)
    assert s["killed"] == 1 and s["total_bosses"] == 3 and s["pulls"] == 7 and s["wipes"] == 6
    assert s["next_boss"]["name"] == "Entombed Sentinels" and s["next_boss"]["best_pct"] == 38.0
    assert s["nights"] == 2
    tl = metrics.progress_timeline(conn, 44, 5)
    assert [p["kills"] for p in tl] == [1, 2] and tl[-1]["day"] == 2.0  # the post-season kill is not on the timeline
    cmp = metrics.tier_comparison(conn, 5)
    prev = next(c for c in cmp if c["zone_id"] == 44)
    # Dimensius died post-season, so its pulls are not progression and do not add to the cumulative total
    assert [b["cum_pulls"] for b in prev["bosses"]] == [4, 10, 10]
    assert prev["days_to_latest_kill"] == 2.0  # the post-season kill does not extend the tier
    nights = metrics.raid_nights(conn, 44)
    # nights after the season cut-off (2026-05-10) are not part of the tier
    assert [n["pull_date"] for n in nights] == ["2026-05-07", "2026-05-05"]
    assert nights[0]["kills"] == 1 and nights[0]["wipes"] == 5
    att = metrics.attendance_summary(conn, 46)
    assert att["total_raids"] == 3
    assert att["players"][0]["player_name"] == "Tagrik" and att["players"][0]["pct"] == 100.0
    bub = next(p for p in att["players"] if p["player_name"] == "Bubonic")
    assert bub["raids"] == 1  # presence 2 (bench) does not count


def test_rival_metrics(synced):
    conn, *_ = synced
    raids = metrics.rio_raids_for_zone(conn)
    assert raids[0]["slug"] == "the-venomous-abyss" and raids[0]["zone_id"] == 46
    cmp = metrics.rival_comparison(conn, "the-venomous-abyss", 5)
    names = [g["name"] for g in cmp["guilds"]]
    assert names[0] == "Killing Time" and "Internet Diff" in names and "Advance" in names
    kt = cmp["guilds"][0]
    assert kt["killed"] == 1 and kt["realm_rank"] == 3 and kt["world_rank"] == 2500
    assert kt["bosses"][1]["best_pct"] == 38.0 and kt["bosses"][1]["pulls"] == 4
    idiff = next(g for g in cmp["guilds"] if g["name"] == "Internet Diff")
    assert idiff["killed"] == 3 and idiff["pulls"] == 47 and idiff["bosses"][2]["first_kill"] == "2026-08-23"
    standings = metrics.realm_standings(conn, "the-venomous-abyss", 5)
    assert [s["name"] for s in standings] == ["Internet Diff", "Advance", "Killing Time"]
    assert standings[1]["current_prog"].startswith("ulatek 45.5")
    race = metrics.race_timeline(conn, "the-venomous-abyss", 5)
    assert race[0]["is_home"] and race[0]["points"] == [{"date": "2026-08-30", "kills": 1}]
    ov = metrics.overview(conn)
    assert ov["current_tier"]["id"] == 46
    assert ov["raiderio"][0]["raid_slug"] == "the-venomous-abyss" and ov["raiderio"][0]["heroic_world"] == 1904


def test_partition_backfill_reruns_each_report_once(synced):
    """Parses fetched before partitions existed have to be revisited, but only ever once."""
    from killingtime.sync import SyncStats, sync_parses

    conn, wcl, _rio, settings = synced
    zone_ids = [r["id"] for r in conn.execute("SELECT DISTINCT zone_id AS id FROM reports WHERE zone_id IS NOT NULL")]

    # Pretend everything was synced before partitions were a thing.
    conn.execute("UPDATE parses SET partition = NULL")
    conn.execute("UPDATE reports SET partition_checked_at = NULL")
    conn.commit()
    stale = conn.execute(
        "SELECT COUNT(*) c FROM reports WHERE rankings_synced_at IS NOT NULL AND partition_checked_at IS NULL"
    ).fetchone()["c"]
    assert stale, "fixture has no already-synced reports to backfill"

    stats = SyncStats()
    sync_parses(conn, wcl, zone_ids, stats, lambda *_: None)
    checked = conn.execute("SELECT COUNT(*) c FROM reports WHERE partition_checked_at IS NOT NULL").fetchone()["c"]
    assert checked, "the backfill re-read nothing"

    # Every report that actually had partition-less parses has now been looked at. Reports with no parses at all
    # are left alone, because there is nothing in them to backfill.
    left = conn.execute(
        """SELECT COUNT(*) c FROM reports r WHERE r.rankings_synced_at IS NOT NULL
           AND r.partition_checked_at IS NULL
           AND EXISTS (SELECT 1 FROM parses p WHERE p.report_code = r.code AND p.partition IS NULL)"""
    ).fetchone()["c"]
    assert left == 0, "a report with partition-less parses was left unchecked"

    # And a second run costs nothing: the stamp stops it circling even if no partition ever came back.
    before = wcl.queries_made
    sync_parses(conn, wcl, zone_ids, stats, lambda *_: None)
    assert wcl.queries_made == before, "the backfill went round again"


def test_a_partial_sync_still_publishes(synced, monkeypatch):
    """A third party timing out must not leave the public site serving last week's data."""
    import pytest

    from killingtime import sync as sync_mod

    conn, wcl, rio, settings = synced
    published: list[str] = []
    monkeypatch.setattr(sync_mod, "publish", lambda *a, **k: published.append("shipped"))
    monkeypatch.setattr(sync_mod, "sync_raiderio", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("504 from raider.io")))

    with pytest.raises(RuntimeError, match="504 from raider.io"):
        run_sync(conn, settings, wcl, rio, full=False, progress=lambda m: None)

    assert published == ["shipped"], "the run failed before publishing what it had already gathered"
    row = conn.execute("SELECT status, detail FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "error" and "504 from raider.io" in row["detail"]


def test_world_rank_curve_is_rebuilt_from_the_leaderboard(synced):
    """Raider.IO only reports a rank as it stands now, so the curve has to be re-derived from kill times."""
    conn, _wcl, _rio, _settings = synced
    rows = {(r["guild"], r["x"]): r for r in conn.execute(
        """SELECT g.name AS guild, c.x, c.world_rank, c.tied, c.kills FROM world_rank_curve c JOIN guilds g ON g.id = c.guild_id
           WHERE c.raid_slug = 'the-venomous-abyss' AND c.difficulty = 5 AND c.axis = 'week'""")}
    assert rows, "no weekly curve was rebuilt"

    # Internet Diff cleared 3 on 2026-08-23; Advance had 2 by 08-25; we had 1 on 08-30. By the last week that is
    # exactly the world order, and each guild's rank must reflect only what it had killed by then.
    last = max(x for _g, x in rows)
    assert rows[("Internet Diff", last)]["world_rank"] == 1
    assert rows[("Advance", last)]["world_rank"] == 2
    assert rows[("Killing Time", last)]["world_rank"] == 3
    assert rows[("Killing Time", last)]["kills"] == 1

    # Before our first kill (2026-08-30) we are unranked, not last: Raider.IO does not list a guild with nothing down.
    early = [r for (g, _x), r in rows.items() if g == "Killing Time" and r["kills"] == 0]
    assert early and all(r["world_rank"] is None for r in early)

    # The band travels with the rank: at the end we are alone on 1 kill, Advance alone on 2, Internet Diff alone on 3.
    assert rows[("Killing Time", last)]["tied"] == 1

    scan = conn.execute("SELECT * FROM world_scan WHERE raid_slug = 'the-venomous-abyss' AND difficulty = 5").fetchone()
    assert scan["home_rank"] == 3 and scan["pool"] == 3

    # Normal is never scanned: nobody races it, and it would double the backfill for nothing.
    assert not conn.execute("SELECT 1 FROM world_scan WHERE difficulty < 4").fetchone()


def test_tier_race_series(synced):
    conn, *_ = synced
    race = metrics.tier_race(conn, "the-venomous-abyss", 5, "week")
    assert [s["name"] for s in race["series"]][0] == "Killing Time", "our own line comes first"
    assert {s["name"] for s in race["series"]} == {"Killing Time", "Internet Diff", "Advance"}
    # A colour slot per guild, stable across difficulties, and never past the palette's eight hues.
    idx = {s["name"]: s["color_index"] for s in race["series"]}
    assert len(set(idx.values())) == 3 and max(idx.values()) < 8
    heroic = {s["name"]: s["color_index"] for s in metrics.tier_race(conn, "the-venomous-abyss", 4, "week")["series"]}
    assert heroic and all(idx[name] == slot for name, slot in heroic.items() if name in idx), \
        "switching difficulty repainted a guild that appears in both"
    # Points only exist from a guild's first kill onwards.
    ours = next(s for s in race["series"] if s["is_home"])
    assert ours["points"] and all(p["y"] and p["kills"] >= 1 for p in ours["points"])

    by_boss = metrics.tier_race(conn, "the-venomous-abyss", 5, "boss")
    theirs = next(s for s in by_boss["series"] if s["name"] == "Internet Diff")
    assert [p["x"] for p in theirs["points"]] == sorted(p["x"] for p in theirs["points"])
    assert [p["kills"] for p in theirs["points"]] == [1, 2, 3], "each boss point counts what was down at that kill"


def test_far_off_guilds_start_hidden(synced):
    """A guild hundreds of places away flattens the axis until our own line is a straight edge at the bottom."""
    from killingtime.config import GuildRef
    from killingtime.sync import upsert_guild

    conn, *_ = synced
    ours = conn.execute(
        """SELECT axis, x, kills, at_ms, label FROM world_rank_curve c JOIN guilds g ON g.id = c.guild_id
           WHERE g.is_home = 1 AND c.raid_slug = 'the-venomous-abyss' AND c.difficulty = 5 AND c.world_rank IS NOT NULL"""
    ).fetchall()
    assert ours, "fixture has no home curve to mirror"

    # The fixture pool is three guilds, so give everyone realistic ranks: us deep in the field, one guild alongside
    # us, one up at the sharp end.
    conn.execute("""UPDATE world_rank_curve SET world_rank = 1413 WHERE guild_id =
                    (SELECT id FROM guilds WHERE is_home = 1) AND world_rank IS NOT NULL""")
    for name, rank in (("Next Door", 1360), ("Way Ahead", 8)):
        gid = upsert_guild(conn, GuildRef(name, "draenor", "eu"))
        for r in ours:
            conn.execute(
                """INSERT OR REPLACE INTO world_rank_curve(raid_slug, difficulty, guild_id, axis, x, world_rank, tied, kills, at_ms, label)
                   VALUES ('the-venomous-abyss', 5, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (gid, r["axis"], r["x"], rank, r["kills"], r["at_ms"], r["label"]))
    conn.commit()

    by_name = {s["name"]: s for s in metrics.tier_race(conn, "the-venomous-abyss", 5, "week")["series"]}
    assert by_name["Killing Time"]["default_on"], "our own line is always on"
    assert by_name["Next Door"]["default_on"], "#1,360 against our #1,413 is the same race"
    assert not by_name["Way Ahead"]["default_on"], "#8 against our #1,413 would flatten the axis"
    assert by_name["Way Ahead"]["points"], "still drawn, just switched off, so the legend can bring it back"


def test_a_raid_with_no_world_leaderboard_does_not_starve_the_queue(synced):
    """Raider.IO drops the world board for old raids. An unstamped raid sorts first, so one dead tier would be
    retried on every run and nothing behind it would ever be scanned."""
    from killingtime.sync import SyncStats, scan_world_ranks

    conn, _wcl, rio, settings = synced

    class NoBoard:
        requests_made = 0

        def raid_rankings(self, *a, **k):
            return []

    stats = SyncStats()
    ok = scan_world_ranks(conn, NoBoard(), "manaforge-omega", 5, settings.home_guild, stats, lambda *_: None)
    assert ok is False
    row = conn.execute("SELECT * FROM world_scan WHERE raid_slug = 'manaforge-omega' AND difficulty = 5").fetchone()
    assert row is not None, "a failed scan left no mark, so it will be retried first forever"
    assert row["pool"] == 0 and row["scanned_at"]
    assert any("no world leaderboard" in w for w in stats.warnings), "a silent no-op is invisible in the sync log"

    # Stamped, but not offered as a rebuilt curve on the page.
    offered = {r["slug"]: r["scans"] for r in metrics.race_raids(conn)}
    assert 5 not in offered.get("manaforge-omega", {})
