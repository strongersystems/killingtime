"""Comical bios and portrait prompts for the Meet the Team page.

Everything here is generated from real data - attendance, parses, spec, gear - so the jokes are about what a
raider actually did, not invented. Picks are seeded by name so a player's bio stays the same between page loads,
and the tone stays fond rather than pointed: the whole guild reads this page.
"""

from __future__ import annotations

import hashlib
from typing import Any

RAID_STYLE = (
    "painterly World of Warcraft splash-art, dramatic rim lighting, 3/4 view, full body, "
    "high detail on armour and weapons, cinematic raid lighting, no text, no watermark"
)


def _article(word: str) -> str:
    return "an" if word[:1].lower() in "aeiou" else "a"


def _pick(options: list[str], name: str, salt: str = "") -> str:
    """Stable choice: the same player always gets the same line."""
    if not options:
        return ""
    digest = hashlib.sha256(f"{name}|{salt}".encode()).digest()
    return options[digest[0] % len(options)]


def _attendance_line(p: dict[str, Any]) -> str:
    pct, raids = p.get("pct"), p.get("raids") or 0
    if pct is None:
        return "Turns up in the logs without ever appearing on the attendance sheet, which is its own talent."
    if pct >= 95:
        return _pick([
            f"Has been at {raids} of the tier's raids and looks personally offended by the concept of a night off.",
            f"{raids} raids out of {raids}. We have stopped asking whether they have other hobbies.",
        ], p["player"], "att")
    if pct >= 80:
        return _pick([
            f"Shows up to {pct}% of raids, which is more reliable than most raid timers.",
            f"{raids} raids in and still answering the invite before the whisper finishes sending.",
        ], p["player"], "att")
    if pct >= 50:
        return _pick([
            f"Attends {pct}% of the time: regular enough to be a fixture, rare enough to keep an air of mystery.",
            f"Present for {raids} raids, absent for exactly the ones with the loot they wanted.",
        ], p["player"], "att")
    return _pick([
        f"A {pct}% attendance rare spawn. Sightings are usually on progression night, never on farm.",
        f"Turns up for {raids} raids a tier, all of them somehow the important ones.",
    ], p["player"], "att")


def _parse_line(p: dict[str, Any]) -> str:
    avg, kills = p.get("avg"), p.get("kills") or 0
    if not avg or not kills:
        return "Has no parses on record, which they insist is a deliberate lifestyle choice."
    if avg >= 80:
        return _pick([
            f"Averages a {round(avg)} percentile across {kills} kills and would like everyone to know that.",
            f"A {round(avg)} average parse. Mentions it roughly once per pull, entirely unprompted.",
        ], p["player"], "parse")
    if avg >= 60:
        return _pick([
            f"Sits at a {round(avg)} average parse: quietly good, which is somehow more irritating than loud and good.",
            f"{round(avg)} percentile on average, achieved with the calm of someone who has read the log first.",
        ], p["player"], "parse")
    if avg >= 35:
        return _pick([
            f"A steady {round(avg)} percentile. Not the biggest number on the meter, still standing at the end of it.",
            f"Averages {round(avg)}, and has never once let a good parse get in the way of doing a mechanic.",
        ], p["player"], "parse")
    return _pick([
        f"Averages {round(avg)} and measures success in survival rather than percentiles. Frankly, fair.",
        f"A {round(avg)} average parse and a flawless record of being alive to complain about it.",
    ], p["player"], "parse")


ROLE_LINES = {
    "tanks": [
        "Professional wall. Takes the enormous hits so a mage can stand still and criticise the positioning.",
        "Job description: be hit by the largest thing in the room, on purpose, repeatedly, without comment.",
    ],
    "healers": [
        "Keeps the raid alive in spite of its life choices, then gets told the healing was low.",
        "Spends the fight watching twenty health bars and one very specific person who stands in things.",
    ],
    "dps": [
        "Presses buttons, produces numbers, blames the healers. The circle of raiding.",
        "Contributes damage, opinions about damage, and a running commentary on everyone else's damage.",
    ],
}


def _role_line(p: dict[str, Any]) -> str:
    return _pick(ROLE_LINES.get(p.get("role") or "dps", ROLE_LINES["dps"]), p["player"], "role")


def _highlight_line(p: dict[str, Any]) -> str:
    best, worst = p.get("best_boss"), p.get("worst_boss")
    bits = []
    if best and best.get("pct") is not None:
        bits.append(_pick([
            f"Peaked at {round(best['pct'])} on {best['boss']}, a screenshot that still exists somewhere.",
            f"Career highlight: {round(best['pct'])} percentile on {best['boss']}, brought up at every opportunity.",
        ], p["player"], "best"))
    if worst and worst.get("pct") is not None and (not best or worst["boss"] != best["boss"]):
        bits.append(_pick([
            f"Has an ongoing disagreement with {worst['boss']}, who is winning.",
            f"{worst['boss']} remains the nemesis, and the logs are not kind about it.",
        ], p["player"], "worst"))
    return " ".join(bits)


def _tenure_line(p: dict[str, Any]) -> str:
    h = p.get("history") or {}
    tiers, total, first = h.get("tier_count") or 0, h.get("total_raids") or 0, h.get("first_tier")
    if tiers >= 3 and first:
        return _pick([
            f"Has been turning up since {first}: {tiers} tiers and {total} raid nights of accumulated grievance.",
            f"{total} raid nights across {tiers} tiers, dating back to {first}. Nobody has told them they can leave.",
        ], p["player"], "tenure")
    if tiers == 2 and first:
        return _pick([
            f"Second tier with us, {total} nights in, and has learned exactly which mechanics we ignore.",
            f"{total} nights over two tiers since {first}: officially past the trial and into the resentment.",
        ], p["player"], "tenure")
    if total >= 4:
        return f"New enough to still be enthusiastic, {total} raid nights in."
    return "Freshly arrived, and has not yet learned what we are like on a Wednesday."


def _progress_line(p: dict[str, Any]) -> str:
    prog = p.get("personal_progress") or {}
    summary = prog.get("summary")
    if not summary:
        return ""
    return _pick([
        f"Personal raid record for this tier reads {summary}, which they will happily talk you through.",
        f"Carries a {summary} on the armory, and has opinions about every one of those bosses.",
    ], p["player"], "prog")


def _mplus_line(p: dict[str, Any]) -> str:
    score, best = p.get("mplus_score"), (p.get("mplus_best") or [])
    if not score:
        return _pick([
            "Does not do Mythic+, and treats the subject the way one treats an unpaid parking fine.",
            "Has no Mythic+ score, a decision they describe as 'lifestyle' and everyone else describes as 'avoidance'.",
        ], p["player"], "mplus")
    top = best[0] if best else None
    if top and top.get("level"):
        return _pick([
            f"Mythic+ score of {round(score)}, topping out at a +{top['level']} {top['dungeon']} that is still discussed.",
            f"{round(score)} Mythic+ score and a +{top['level']} {top['dungeon']} best, achieved with only mild shouting.",
        ], p["player"], "mplus")
    return f"Mythic+ score of {round(score)}, earned quietly and mentioned constantly."


def _alt_line(p: dict[str, Any]) -> str:
    alts = p.get("alts") or []
    if not alts:
        return ""
    if len(alts) == 1:
        return f"Also answers to {alts[0]}, usually when the raid needs the other armour type."
    return f"Keeps {len(alts)} alts on standby ({', '.join(alts)}), one of which is definitely better geared than the main."


def bio(player: dict[str, Any]) -> list[str]:
    """Four to seven short, mildly comical sentences, every one of them built from the player's own numbers."""
    lines = [
        _role_line(player),
        _tenure_line(player),
        _attendance_line(player),
        _parse_line(player),
        _highlight_line(player),
        _progress_line(player),
        _mplus_line(player),
        _alt_line(player),
    ]
    return [line for line in lines if line]


def stats(p: dict[str, Any]) -> list[dict[str, Any]]:
    """The numbers strip on a card: what they did this tier, and what they have done overall."""
    h = p.get("history") or {}
    best = (p.get("mplus_best") or [None])[0]
    out = [
        {"label": "Raids this tier", "value": p.get("raids") or 0},
        {"label": "Attendance", "value": f"{p['pct']}%" if p.get("pct") is not None else "—"},
        {"label": "Avg parse", "value": round(p["avg"]) if p.get("avg") is not None else "—"},
        {"label": "Best parse", "value": round(p["best"]) if p.get("best") is not None else "—"},
        {"label": "Career raids", "value": h.get("total_raids") or 0},
        {"label": "Tiers", "value": h.get("tier_count") or 0},
        {"label": "M+ score", "value": round(p["mplus_score"]) if p.get("mplus_score") else "—"},
        {"label": "Best key", "value": f"+{best['level']}" if best and best.get("level") else "—"},
    ]
    if p.get("item_level"):
        out.append({"label": "Item level", "value": round(p["item_level"])})
    if p.get("achievement_points"):
        out.append({"label": "Achievements", "value": f"{p['achievement_points']:,}"})
    return out


# Frame 1 is the hero shot; the rest are the animation, in order, so the loop tells a small joke.
FRAMES = [
    ("At arms", "standing at arms, weapons drawn and ready for battle, feet planted, heroic front-facing stance, "
                "the shot you would put on a guild recruitment poster"),
    ("Signature move", "mid-ability, caught at the exact frame their signature spell or swing goes off, "
                       "light and effects trailing from the weapons"),
    ("Victory", "standing on a defeated boss's severed hand, weapons raised, celebrating a kill they contributed "
                "to in a way that is being generously described as 'meaningful'"),
    ("The mechanic", "a fraction of a second too late, mid-air, backlit by the mechanic that is about to remove "
                     "them from the fight, expression of dawning realisation"),
    ("Waiting", "leaning on their weapon looking profoundly bored, waiting for the raid leader to finish "
                "explaining the pull for the fourth time"),
]

PROPS = [
    "a half-eaten feast on the floor nearby",
    "a summoning stone glowing hopefully in the background",
    "a repair bill fluttering past on the wind",
    "a tiny pet mimicking the pose perfectly",
    "a pile of empty flasks arranged with unsettling care",
]


def _comic_detail(p: dict[str, Any]) -> str:
    """One funny, true-to-the-data touch for the picture."""
    if (p.get("pct") or 0) >= 95:
        return "the expression of somebody who has not missed a raid all tier and wants it noticed"
    if (p.get("avg") or 0) >= 80:
        return "a smug, well-earned half-smile aimed directly at the damage meter"
    worst = p.get("worst_boss")
    if worst and worst.get("boss"):
        return f"the faint, haunted look of someone who has wiped to {worst['boss']} more times than they will admit"
    if (p.get("raids") or 0) <= 2:
        return "the sheepish air of a rare spawn who has just been spotted"
    return "an expression of profound patience, the kind earned over many, many pulls"


def _subject(p: dict[str, Any], guild: str, realm: str) -> list[str]:
    """The part of the prompt that makes the picture look like them: race, spec, transmog, real weapons."""
    who = " ".join(x for x in (p.get("gender"), p.get("race"), p.get("spec"), p.get("class")) if x) or "raider"
    parts = [f"{_article(who).capitalize()} {who} named {p['player']}, of the guild {guild} on {realm}."]
    gear = p.get("gear") or {}
    weapons = [gear[s]["name"] for s in ("mainhand", "offhand") if gear.get(s) and gear[s].get("name")]
    parts.append("Wielding " + " and ".join(weapons) + ", drawn and clearly visible."
                 if weapons else "Weapons drawn and clearly visible, matching their class and spec.")
    armour = [gear[s]["name"] for s in ("head", "shoulder", "chest", "back") if gear.get(s) and gear[s].get("name")]
    parts.append("Wearing their current transmog" + (": " + ", ".join(armour) if armour else ", raid-worn and battered") + ".")
    if p.get("item_level"):
        parts.append(f"Gear reads as roughly item level {round(p['item_level'])}: worn, used, clearly raided in.")
    return parts


def portrait_prompts(p: dict[str, Any], guild: str, realm: str, raid: str | None = None) -> list[dict[str, str]]:
    """Five prompts for one character: the hero shot first, then four frames that loop as a short animation.

    Keep the subject text identical across frames and change only the pose, so an image generator returns five
    pictures of the same character rather than five different people."""
    subject = _subject(p, guild, realm)
    setting = ""
    if raid:
        where = raid[4:] if raid.lower().startswith("the ") else raid
        setting = f"Setting: the {where} raid floor, debris and spell effects in the air."
    out = []
    for i, (label, pose) in enumerate(FRAMES):
        parts = [f"Full-body World of Warcraft character portrait, frame {i + 1} of 5 of the same character."]
        parts += subject
        parts.append("Pose: " + pose + ".")
        if i == 0:
            parts.append("Expression: " + _comic_detail(p) + ".")
            if setting:
                parts.append(setting)
            parts.append("Include " + _pick(PROPS, p["player"], "prop") + " somewhere for comedy.")
        elif setting:
            parts.append(setting)
        parts.append("Same character, same armour, same weapons, same camera distance and framing as the other frames.")
        parts.append("Style: " + RAID_STYLE + ".")
        out.append({"label": label, "prompt": " ".join(parts)})
    return out


def portrait_prompt(p: dict[str, Any], guild: str, realm: str, raid: str | None = None) -> str:
    """The hero shot: standing at arms in their transmog, weapons drawn."""
    return portrait_prompts(p, guild, realm, raid)[0]["prompt"]
