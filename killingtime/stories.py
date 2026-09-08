"""Short comic stories about each raider, written by Claude from that raider's own record.

The generated bios in :mod:`flavour` are assembled from templates, which is fine for a line or two but reads like
a form letter across sixty cards: every one is "did X on Y, peaked at Z". A story is a different thing. It takes
the same facts - what they play, what they wear, how often they turn up, how they parse, which boss beat them up -
and writes something a guild-mate would actually read out loud.

Stories are written once and stored. The facts a story was written from are hashed with it, so a raider whose
numbers have not meaningfully moved keeps the story the guild has already read, and one who has changed gets a new
one on the next run. No API key means no stories, and the templated bio stands in.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any

from .config import Settings
from .db import transaction

# Each raider is assigned one of these, stably, so the page is not sixty variations on one joke. The model is told
# to write in the assigned shape; the shapes are deliberately different lengths and voices.
SHAPES = [
    ("anecdote", "A single specific incident from a raid night, told in three or four sentences, with a beginning "
                 "and a punchline. Past tense."),
    ("field-guide", "A naturalist's field-guide entry for this raider as if they were a species: habitat, diet, "
                    "call, behaviour when threatened. Deadpan scientific register."),
    ("rumour", "What the rest of the guild says about them when they are not in the Discord channel. Gossip, "
               "hedged and second-hand, ending with something that is obviously true."),
    ("incident-report", "A terse officer's incident report about one raid night: numbered facts, a finding and a "
                        "recommendation nobody will act on."),
    ("legend", "A short heroic legend, told the way a bard would tell it, about something extremely mundane."),
    ("interview", "Two or three lines of a post-raid interview: a question from an unseen interviewer and their "
                  "answer, which does not quite address the question."),
    ("advert", "A small ad placed by the raider offering their services, in the voice of someone overselling."),
    ("obituary", "A premature and affectionate obituary for the raider, who is fine, written after one bad pull."),
    ("recipe", "A recipe whose method is really a description of how this raider plays. Ingredients, then steps."),
    ("weather", "A shipping-forecast or weather report for the area around this raider during a raid night."),
    ("cv", "Two or three lines from their CV, with one entry that gives the game away."),
    ("nature-doc", "A wildlife-documentary voiceover of them doing something ordinary, in a hushed, reverent tone."),
]

SYSTEM = """You write short, funny character pieces about members of a World of Warcraft raiding guild, for the
guild's own public website. The guild reads these; they should make a raider laugh and want to send it to a friend,
never wince.

Rules:
- Write about the CHARACTER and the player's role in the raid. You may invent freely: incidents, opinions, habits,
  running jokes. It is understood to be fiction.
- Use the supplied facts as raw material and colour, not as a list to recite. Never write "averages a 62 parse" or
  "has 87% attendance"; if a number earns its place, work it in naturally, and at most once.
- British English. Dry, specific, affectionate. Understatement over exclamation.
- No emoji, no exclamation marks, no headings, no bullet lists unless the assigned shape asks for them.
- Never these tics: a three-item list with a longer wry item on the end; "not X, but Y"; "part A, part B";
  starting with "Every guild has"; ending on a neat aphorism that restates the joke.
- Nothing about real-world identity, appearance, nationality, employment, relationships or health. The joke is
  always about raiding.
- 45 to 90 words. One paragraph unless the shape says otherwise. No title. No sign-off.
"""


def _facts(card: dict[str, Any]) -> dict[str, Any]:
    """The raw material a story is written from - and the thing we hash to decide if it needs rewriting."""
    hist = card.get("history") or {}
    best, worst = card.get("best_boss") or {}, card.get("worst_boss") or {}
    return {
        "name": card["player"],
        "who": " ".join(x for x in (card.get("gender"), card.get("race"), card.get("spec"), card.get("class")) if x),
        "role": {"tanks": "tank", "healers": "healer", "dps": "damage"}.get(card.get("role") or "", "damage"),
        "team": card.get("team"),
        "weapons": card.get("weapons") or [],
        "wearing": [v.get("name") for k, v in (card.get("gear") or {}).items()
                    if k in ("head", "shoulder", "chest", "back") and isinstance(v, dict) and v.get("name")],
        "item_level": round(card["item_level"]) if card.get("item_level") else None,
        "raids_this_tier": card.get("raids"),
        "attendance_pct": round(card["pct"]) if card.get("pct") is not None else None,
        "average_parse": round(card["avg"]) if card.get("avg") is not None else None,
        "best_parse": round(card["best"]) if card.get("best") is not None else None,
        "best_boss": {"boss": best.get("boss"), "pct": round(best["pct"])} if best.get("pct") is not None else None,
        "nemesis": {"boss": worst.get("boss"), "pct": round(worst["pct"])} if worst.get("pct") is not None else None,
        "career_nights": hist.get("total_raids"),
        "tiers_raided": hist.get("tier_count"),
        "first_tier": hist.get("first_tier"),
        "mplus_score": round(card["mplus_score"]) if card.get("mplus_score") else None,
        "best_key": (card.get("mplus_best") or [{}])[0].get("level"),
        "alts": card.get("alts") or [],
        "tier_progress": (card.get("personal_progress") or {}).get("summary"),
    }


def _shape(name: str) -> tuple[str, str]:
    digest = hashlib.sha256(name.encode()).digest()
    return SHAPES[digest[1] % len(SHAPES)]


def _fingerprint(facts: dict[str, Any], shape: str) -> str:
    return hashlib.sha256(json.dumps([facts, shape], sort_keys=True, default=str).encode()).hexdigest()[:16]


SEED_FILE = "data/stories.json"


def seed(conn: sqlite3.Connection, progress: Any = None) -> int:
    """Load the stories that ship with the app for anyone who has none.

    Written by hand so the guild has something to read before anyone sets an Anthropic key.

    A story that is already on the page is left alone. Editing the bundled text does nothing on its own: a stored
    bio is only replaced when its entry's ``rev`` is raised above the one that was stored, which has to be done
    deliberately, one person at a time. That rule exists because the alternative - replacing every seeded bio whose
    text no longer matches the file - rewrites people nobody asked to have rewritten. A story Claude generated is
    never written back over at all.
    """
    from pathlib import Path

    path = Path(__file__).parent / SEED_FILE
    if not path.exists():
        return 0
    players = json.loads(path.read_text(encoding="utf-8")).get("players") or {}
    # The revision a stored bio came from lives in ``fingerprint`` as "seed:N"; the bare "seed" of older rows is 1.
    stored_rev = {r["player_name"]: _seed_rev(r["fingerprint"])
                  for r in conn.execute("SELECT player_name, fingerprint FROM bios WHERE model = 'seed'")}
    written = {r["player_name"] for r in conn.execute("SELECT player_name FROM bios")}
    # Only people this database has actually seen raid: the bundled set belongs to one guild, and another guild
    # running this app should not inherit our in-jokes.
    known = {r["player_name"] for r in conn.execute("SELECT DISTINCT player_name FROM v_attendance WHERE presence = 1")}
    added = changed = 0
    with transaction(conn):
        for name, entry in players.items():
            story = (entry.get("story") or "").strip()
            rev = int(entry.get("rev") or 1)
            if name not in known or not story:
                continue
            if name in stored_rev:
                if rev <= stored_rev[name]:
                    continue
                conn.execute(
                    "UPDATE bios SET story = ?, shape = ?, fingerprint = ?, written_at = ? WHERE player_name = ?",
                    (story, entry.get("shape"), f"seed:{rev}", int(time.time() * 1000), name),
                )
                changed += 1
            elif name not in written:
                conn.execute(
                    """INSERT INTO bios(player_name, story, shape, fingerprint, model, written_at)
                       VALUES (?, ?, ?, ?, 'seed', ?)""",
                    (name, story, entry.get("shape"), f"seed:{rev}", int(time.time() * 1000)),
                )
                added += 1
    if (added or changed) and progress:
        progress(f"stories: {added} seeded, {changed} revised from the bundled set")
    return added + changed


def _seed_rev(fingerprint: str | None) -> int:
    """Which revision of the bundled file a stored bio came from. Rows written before revisions existed are 1."""
    if fingerprint and fingerprint.startswith("seed:"):
        try:
            return int(fingerprint[5:])
        except ValueError:
            return 1
    return 1


def stored(conn: sqlite3.Connection) -> dict[str, str]:
    """Every story we hold, by player name."""
    return {r["player_name"]: r["story"] for r in conn.execute("SELECT player_name, story FROM bios")}


def write_stories(conn: sqlite3.Connection, cards: list[dict[str, Any]], settings: Settings,
                  client: Any = None, refresh: bool = False, limit: int = 200,
                  progress: Any = None) -> dict[str, int]:
    """Write a story for every raider whose facts have changed since the last one. Returns a small tally."""
    say = progress or (lambda _m: None)
    if not settings.ask_configured:
        say("stories: no Anthropic key, keeping the generated bios")
        return {"written": 0, "kept": 0, "failed": 0}
    import anthropic

    client = client or anthropic.Anthropic(api_key=settings.anthropic_api_key)
    have = {r["player_name"]: r["fingerprint"] for r in conn.execute("SELECT player_name, fingerprint FROM bios")}
    tally = {"written": 0, "kept": 0, "failed": 0}
    for card in cards[:limit]:
        facts = _facts(card)
        shape, instruction = _shape(card["player"])
        print_id = _fingerprint(facts, shape)
        if not refresh and have.get(card["player"]) == print_id:   # "seed" never matches, so seeds get rewritten
            tally["kept"] += 1
            continue
        prompt = (f"Shape to write in - {shape}: {instruction}\n\n"
                  f"The raider, as facts:\n{json.dumps(facts, indent=1, ensure_ascii=False)}\n\n"
                  "Write the piece. Output the piece only.")
        try:
            resp = client.messages.create(
                model=settings.ask_model, max_tokens=600, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            )
            story = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text").strip()
        except Exception as exc:  # noqa: BLE001 - one raider's story failing must not stop the rest
            tally["failed"] += 1
            say(f"story failed for {card['player']}: {exc}")
            continue
        if not story:
            tally["failed"] += 1
            continue
        with transaction(conn):
            conn.execute(
                """INSERT INTO bios(player_name, story, shape, fingerprint, model, written_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(player_name) DO UPDATE SET story = excluded.story, shape = excluded.shape,
                       fingerprint = excluded.fingerprint, model = excluded.model, written_at = excluded.written_at""",
                (card["player"], story, shape, print_id, settings.ask_model, int(time.time() * 1000)),
            )
        tally["written"] += 1
    say(f"stories: {tally['written']} written, {tally['kept']} unchanged, {tally['failed']} failed")
    return tally
