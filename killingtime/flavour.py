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


def _hash(name: str, salt: str = "") -> int:
    return hashlib.sha256(f"{name}|{salt}".encode()).digest()[0]


def _pick(options: list[str], name: str, salt: str = "") -> str:
    """Stable choice: the same player always gets the same line."""
    if not options:
        return ""
    return options[_hash(name, salt) % len(options)]


def _attendance_line(p: dict[str, Any]) -> str:
    pct, raids = p.get("pct"), p.get("raids") or 0
    pct = round(pct) if pct is not None else None   # "80%", never "80.0%"
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


# Several variants per role: with fifty-odd raiders on one page, two would read like a form letter.
ROLE_LINES = {
    "tanks": [
        "Professional wall. Takes the enormous hits so a mage can stand still and criticise the positioning.",
        "Job description: be hit by the largest thing in the room, on purpose, repeatedly, without comment.",
        "Walks backwards for a living and has strong opinions about where everyone else is standing.",
        "Holds the boss still. Gets thanked for it roughly once a tier, usually by accident.",
        "The first one in and, on a good night, the last one alive to say so.",
        "Spends the fight facing away from the raid, which is both a tanking requirement and a coping strategy.",
    ],
    "healers": [
        "Keeps the raid alive in spite of its life choices, then gets told the healing was low.",
        "Spends the fight watching twenty health bars and one very specific person who stands in things.",
        "Quietly undoes everyone else's mistakes and has never once been thanked in a timely fashion.",
        "Believes deeply that most deaths are preventable, and can name exactly whose fault each one was.",
        "Green numbers, grey hair. The two are related.",
        "The reason the pull lasted long enough for anyone to complain about their damage.",
    ],
    "dps": [
        "Presses buttons, produces numbers, blames the healers. The circle of raiding.",
        "Contributes damage, opinions about damage, and a running commentary on everyone else's damage.",
        "Exists to make a number go up, and to make sure everybody hears about the number.",
        "Fully committed to the damage meter, and only loosely committed to the mechanics around it.",
        "Would do more damage if they didn't keep having to move, as they will explain at length.",
        "Turns up, hits the boss, dies to something avoidable, links the meter anyway.",
    ],
}


def _role_line(p: dict[str, Any]) -> str:
    return _pick(ROLE_LINES.get(p.get("role") or "dps", ROLE_LINES["dps"]), p["player"], "role")


def _best_line(p: dict[str, Any]) -> str:
    best = p.get("best_boss")
    if not best or best.get("pct") is None:
        return ""
    pct, boss = round(best["pct"]), best["boss"]
    return _pick([
        f"Peaked at {pct} on {boss}, a screenshot that still exists somewhere.",
        f"Career highlight: {pct} percentile on {boss}, brought up at every opportunity.",
        f"Their best night is {boss} at {pct}, and they can tell you exactly what they pressed.",
        f"{boss} is where it clicks: {pct} percentile, achieved once, referenced forever.",
        f"Once put up a {pct} on {boss}. The guild has heard about it. Twice.",
        f"Owns exactly one {pct} parse, on {boss}, and has built a personality around it.",
    ], p["player"], "best")


def _worst_line(p: dict[str, Any]) -> str:
    """Only some cards carry a nemesis line - if everyone had one they would all read the same."""
    worst, best = p.get("worst_boss"), p.get("best_boss")
    if not worst or worst.get("pct") is None or (best and worst["boss"] == best["boss"]):
        return ""
    if _hash(p["player"], "worst-gate") % 3 == 0:   # roughly a third of the roster
        return ""
    boss, pct = worst["boss"], round(worst["pct"])
    return _pick([
        f"Has an ongoing disagreement with {boss}, who is winning.",
        f"{boss} remains the nemesis, and the logs are not kind about it.",
        f"Whatever {boss} does, it does it to them first: {pct} percentile and a lot of excuses.",
        f"Every guild has one boss they cannot parse on. Theirs is {boss}, at a stately {pct}.",
        f"Politely requests we skip {boss}, where the logs read {pct} and the memories read worse.",
        f"{boss} is the reason they do not link their own logs unprompted.",
    ], p["player"], "worst")


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
        f"The armory says {summary}. They would like to add context to that.",
        f"{summary} on the armory, every boss of it earned on a Wednesday night.",
        f"Sitting at {summary} this tier and counting, loudly.",
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


def _career_line(p: dict[str, Any]) -> str:
    """Their all-time peak, which is often not in this tier at all."""
    cb = p.get("career_best")
    if not cb or cb.get("pct") is None:
        return ""
    pct, boss, zone = round(cb["pct"]), cb["boss"], cb.get("zone") or "somewhere"
    diff = cb.get("difficulty") or ""
    where = f"{zone}" + (f", {diff}" if diff else "")
    best = p.get("best_boss") or {}
    if best.get("pct") is not None and round(best["pct"]) >= pct:
        return ""   # this tier is already their peak; _best_line has it covered
    return _pick([
        f"All-time peak: {pct} on {boss} back in {where}. They have not stopped chasing it since.",
        f"Their high-water mark is a {pct} on {boss} ({where}), and it comes up whenever the meter does.",
        f"Career best is still {boss} at {pct} percentile ({where}). Every tier since has been a rebuilding year.",
        f"Has, on record, a {pct} parse on {boss} ({where}). The rest is just consistency.",
    ], p["player"], "career")


def _armory_line(p: dict[str, Any]) -> str:
    """What Raider.IO says they have cleared across the expansion, not just this tier."""
    raids = [r for r in (p.get("armory_raids") or []) if r.get("summary")]
    if not raids:
        return ""
    myth = [r for r in raids if r["mythic"]]
    if myth:
        best = max(myth, key=lambda r: r["mythic"])
        return _pick([
            f"The armory carries {best['mythic']}/{best['total']} Mythic in {best['raid']}, across {len(raids)} raids of history.",
            f"{len(raids)} raids on the armory, the pick of them {best['mythic']}/{best['total']} Mythic in {best['raid']}.",
            f"Has {best['mythic']} Mythic bosses in {best['raid']} to their name, and the receipts to prove it.",
        ], p["player"], "armory")
    best = max(raids, key=lambda r: r["heroic"])
    return f"Armory history runs to {len(raids)} raids, topping out at {best['heroic']}/{best['total']} Heroic in {best['raid']}."


def _volume_line(p: dict[str, Any]) -> str:
    """How much of them is actually in the logs."""
    parses, bosses = p.get("career_parses") or 0, p.get("career_bosses") or 0
    if parses < 5:
        return ""
    return _pick([
        f"{parses} logged kills across {bosses} different bosses, every one of them a matter of public record.",
        f"The logs hold {parses} of their kills on {bosses} bosses. There is nowhere to hide.",
        f"{parses} parses deep on {bosses} bosses, which is either dedication or a lack of alternatives.",
    ], p["player"], "volume")


# Hand-written for the people the guild would riot about. Everything here is still theirs; it is just kinder.
def _findruid(p: dict[str, Any]) -> list[str]:
    h = p.get("history") or {}
    cb = p.get("career_best") or {}
    lines = [
        "S-tier. The rest of the roster is measured against Findruid, and the measurement is rarely flattering "
        "to the rest of the roster.",
        "The best player in Killing Time, and the least interested in saying so. Ask anyone who has raided behind "
        "them and watch the argument end before it starts.",
    ]
    if p.get("avg") is not None:
        lines.append(f"Averages a {round(p['avg'])} percentile while also doing the mechanic nobody else remembered, "
                     "which is the part the meter never shows.")
    if cb.get("pct") is not None:
        where = ", ".join(x for x in (cb.get("zone"), cb.get("difficulty")) if x)
        lines.append(f"Peak on record: {round(cb['pct'])} on {cb['boss']}{' (' + where + ')' if where else ''} - "
                     "a parse that other people screenshot, and Findruid has to be reminded happened.")
    if h.get("total_raids"):
        lines.append(f"{h['total_raids']} raid nights across {h.get('tier_count') or 0} tiers"
                     f"{', since ' + h['first_tier'] if h.get('first_tier') else ''}, and the answer to 'who covers "
                     "that?' has been the same the entire time.")
    if p.get("pct") is not None:
        lines.append(f"Attendance sits at {round(p['pct'])}%, because progression nights are simply easier when the "
                     "druid is there and everybody knows it.")
    lines.append("Moonfire, brainstem, immaculate cooldown usage, and the patience of somebody who has explained the "
                 "same soak three times without raising their voice. Guild treasure.")
    return lines


def _norman(p: dict[str, Any]) -> list[str]:
    """The raid leader asked for this. Every number in it is his own, which is the problem."""
    h = p.get("history") or {}
    worst, best = p.get("worst_boss") or {}, p.get("best_boss") or {}
    lines = ["Raid leader. Explains the mechanic in detail, at length, twice, and then dies to it."]
    if p.get("avg") is not None:
        lines.append(f"Averages a {round(p['avg'])} percentile. Runs the roster, writes the strat, calls the cooldowns, "
                     "and is out-parsed by most of the people he is calling them for.")
    if worst.get("pct") is not None:
        lines.append(f"Holds a {round(worst['pct'])} percentile on {worst['boss']}. Not a typo. "
                     f"{round(worst['pct'])}. Out of a hundred.")
    if p.get("pct") is not None:
        lines.append(f"{round(p['pct'])}% attendance, from the man who keeps the attendance sheet.")
    if best.get("pct") is not None:
        lines.append(f"Did once put up an {round(best['pct'])} on {best['boss']}, and has dined out on it ever since. "
                     "It is, to be fair, the only exhibit he has.")
    if p.get("mplus_score"):
        lines.append(f"Mythic+ score of {round(p['mplus_score'])}, which he will raise the instant anyone mentions "
                     "his raid logs. Nobody has been fooled yet.")
    if h.get("total_raids"):
        lines.append(f"{h['total_raids']} raid nights across {h.get('tier_count') or 0} tiers"
                     f"{', since ' + h['first_tier'] if h.get('first_tier') else ''}. Longevity is a skill. "
                     "It is not, sadly, the one on the meter.")
    lines.append("Genuinely holds the whole thing together, and would be insufferable if we admitted it. So we don't.")
    return lines


LEGENDS = {"findruid": _findruid, "norman": _norman}


def bio(player: dict[str, Any]) -> list[str]:
    """Short, mildly comical sentences, every one built from the player's own numbers.

    The middle of the bio is stably shuffled per player: with fifty raiders on one page, the same lines in the same
    order would read like a mail merge. Line one is always the role joke, and the alts note is always last."""
    name = player["player"]
    legend = LEGENDS.get(str(player.get("slug") or name).lower())
    if legend:
        lines = legend(player)
        alt = _alt_line(player)
        return [*lines, alt] if alt else lines

    middle = [
        _tenure_line(player), _attendance_line(player), _parse_line(player), _best_line(player),
        _worst_line(player), _career_line(player), _armory_line(player), _volume_line(player),
        _progress_line(player), _mplus_line(player),
    ]
    middle = [line for line in middle if line]
    middle.sort(key=lambda line: _hash(name, line[:24]))   # stable per player, different between players
    lines = [_role_line(player), *middle[:6], _alt_line(player)]
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
