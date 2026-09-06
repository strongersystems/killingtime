# Asking questions

The **Ask** page (and `kt ask`) sends your question to Claude together with a description of the database. Claude
then calls three tools as needed:

| Tool | What it can do |
|---|---|
| `get_overview` | Current guild snapshot: tiers with data, kill counts per difficulty, Raider.IO ranks, last sync |
| `run_sql` | One read-only `SELECT` at a time, max 200 rows, 8-second budget. Runs on a separate read-only connection with an authorizer that only allows reading – `UPDATE`, `DELETE`, `PRAGMA`, `ATTACH` etc. are rejected. |
| `render_chart` | Queue a line / bar / horizontal bar / stacked chart (≤ 8 series) that the page renders with Chart.js. Every chart has a Table toggle. |

The answer is markdown; the SQL Claude ran is shown under a collapsible "tool calls" line so you can sanity-check it.
Follow-up questions include the last few Q&A pairs as context, so "and what about heroic?" works.

## Questions that work well

**Current tier**
- Where are we on the current tier and what is the next boss?
- What's our best pull on the boss we're progressing, and how has the best % moved night by night?
- How many pulls and hours did each mythic boss take this tier?
- How many raid nights have we spent on the current boss?

**Tier comparisons**
- Compare pulls-to-kill by boss position with the previous tier.
- How many days did each tier take from first pull to full clear?
- Were we faster or slower to 4/8 this tier than last tier?
- Average pulls per boss killed, per tier, mythic only.

**Rivals and realm**
- How does our progress race compare with Internet Diff and Advance on the current raid?
- Which realm guilds are within two bosses of us, and how many pulls have they used?
- Who on Draenor killed boss 6 before us, and how many days earlier?
- What is our realm and world rank in heroic and mythic this tier versus last tier?

**Roster**
- Who has the best attendance this tier? Who is under 60%?
- Which classes do we bring most often?
- Average item level of our raid on kills, per boss.

**Raid nights**
- Show kills and wipes per raid night for the last month.
- Which night this tier had the most pulls, and on what?
- How many hours in combat per week?

## Tips

- Say the difficulty if you care ("mythic", "heroic"); otherwise Claude uses the highest difficulty you've pulled
  and tells you which one it picked.
- "Current tier" means the newest raid zone you have logs for. "Previous tier" is the one before that.
- Ask for a chart explicitly if you want one: "…and chart it".
- Our own pull-level detail (every wipe, %, duration) only exists for Killing Time. For other guilds the data is
  Raider.IO's first-kill time, pull count and best %, and some guilds hide pulls.
- `kt ask "question" -v` prints the SQL that was run; `--json` dumps the whole result including chart data.

## Cost and speed

Each question is one or more calls to `claude-opus-5` with the schema in a cached system prompt. Simple questions take
10–20 seconds and cost a few cents; multi-step comparisons take up to a minute. `ASK_EFFORT=medium` is a bit faster
and cheaper; `xhigh` is more thorough on tricky comparisons.
