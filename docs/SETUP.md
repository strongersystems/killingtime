# Step-by-step setup

This walks through everything from zero to a running dashboard with the Ask feature. Budget 20–30 minutes.
Commands are shown for macOS/Linux; Windows differences are noted.

---

## Step 1 – Install Python and get the code

1. Install **Python 3.11 or newer** from https://www.python.org/downloads/ (on Windows tick *Add python.exe to PATH*).
2. Open a terminal in the folder where you want the app and run:

   ```bash
   git clone <this repo url> killingtime
   cd killingtime
   python3 -m venv .venv
   source .venv/bin/activate          # Windows PowerShell:  .venv\Scripts\Activate.ps1
   pip install -e .
   ```

3. Check it installed: `kt --help` should print the command list.

---

## Step 2 – Create the Warcraft Logs API client

This gives the app permission to read your guild's public reports. It is free.

1. Log in at https://www.warcraftlogs.com and open **https://www.warcraftlogs.com/api/clients/**.
2. Click **Create Client** and fill in the form exactly like this:

   | Field | Value | Why |
   |---|---|---|
   | Enter a name for your application | `KillingTime` (or `KillingTime Progress Tracker`) | Warcraft Logs asks for something descriptive so they can tell what the key is for |
   | Enter one or more redirect URLs | `http://localhost:8000/callback` | Required by the form, but this app uses the *client credentials* flow so it is never actually visited. Any valid URL works. |
   | Public Client (Only the PKCE code flow will be usable) | **leave unchecked** | We store the secret on your own machine (`.env`), so we can use the simpler client-credentials flow. Ticking this would disable it. |

3. Click **Create**. You will see a **Client ID** and a **Client Secret**. Copy both now – the secret is only shown once
   (if you lose it, delete the client and create a new one).

> The client-credentials flow can read anything that is **public** on Warcraft Logs. If your guild's reports are
> set to *private* or *unlisted*, make them public (or at least "guild only" won't work – the API client isn't a guild
> member). Most progression guilds already log publicly.

---

## Step 3 – Configure `.env`

```bash
cp .env.example .env         # Windows: copy .env.example .env
```

Open `.env` in any text editor and set:

```ini
WCL_CLIENT_ID=paste-the-client-id
WCL_CLIENT_SECRET=paste-the-client-secret

GUILD_NAME=Killing Time
GUILD_REALM=Draenor
GUILD_REGION=EU

# Guilds you want in the head-to-head view (realm-wide standings are pulled anyway).
# Format: Name@realm/region, separated by semicolons. Realm is the slug (spaces -> dashes, no apostrophes).
RIVAL_GUILDS=Internet Diff@draenor/eu; Advance@draenor/eu; Incentive@draenor/eu
```

Other knobs (all optional, defaults are fine):

| Variable | Default | Meaning |
|---|---|---|
| `SYNC_EXPANSIONS` | `2` | How many recent expansions of raid zones to load (2 = current + previous, enough for tier comparisons) |
| `RIO_REALM_SCAN_PAGES` | `2` | Pages (100 guilds each) of the realm leaderboard to store per raid & difficulty |
| `TIER_MAP` | – | Manual Warcraft Logs zone → Raider.IO raid mapping if automatic name matching misses one, e.g. `{"46": "the-venomous-abyss"}`. The **Status** page shows what is mapped. |
| `RAID_TEAMS` | – | Your raid teams and a few roster names each, e.g. `CE Team: Nórmán, Elelena; 6 Hour Team: Andrewro, Billadin`. Each report is attributed to the team with the most roster members in its attendance (accents and case are ignored). Names need not be complete: 6–10 regulars per team is plenty. |
| `RAID_TEAM_MIN_MATCHES` | `2` | Minimum roster matches for a report to be attributed to a team; ties stay unattributed (guild-only view). |
| `RAID_ALTS` | – | Alts, which no API exposes, in the same format as `RAID_TEAMS`: `Findruid: Findpal, Finddk; Nórmán: Normanpriest`. Listed on each Meet the Team card. |
| `MEMBER_IMAGE_URL` | `/static/members/{slug}/{n}.webp` | Where a member's five generated portrait frames live. `{slug}` is their name lower-cased and de-accented, `{n}` the frame number 1–5. Frame 1 is the hero shot; the cards animate through all five on hover once the files exist, and fall back to Blizzard's character render when they do not. |
| `SYNC_PARSES_PER_RUN` | `60` | Kill reports whose Warcraft Logs parses are fetched per sync, newest first (about 18 WCL points each, so 60 is roughly a third of the hourly budget). Older tiers fill in over a few syncs. `0` disables parses. |
| `SITE_APPLY_URL`, `SITE_DISCORD_URL` | – | Links shown on the public site (Apply / Discord buttons). |
| `SITE_TAGLINE`, `SITE_ABOUT`, `SITE_RAID_TIMES`, `SITE_RECRUITING` | – | Text for the public site: hero line, "Join us" paragraph, raid schedule, recruiting banner. |
| `SITE_URL` | – | Canonical URL of the public site (e.g. `https://killingtime.fyi`). |
| `KT_DB_PATH` | `data/killingtime.db` | Where the SQLite database lives |
| `KT_HOST`, `KT_PORT` | `127.0.0.1`, `8000` | Web server bind address |

Never commit `.env` – it is already in `.gitignore`.

---

## Step 4 – Verify and run the first sync

```bash
kt check
```

Expected output looks like:

```
killingtime 0.1.0
guild:        Killing Time @ Draenor (EU)
database:     data/killingtime.db
rivals:       Internet Diff, Advance, Incentive
warcraftlogs: OK - points used 3/3600 this hour; guild found (id 637454)
raider.io:    OK - horde - the-venomous-abyss: 8/8 H, tier-mn-1: 9/9 M, ...
ask:          ANTHROPIC_API_KEY not set - the Ask page will be disabled
```

If `warcraftlogs:` says FAILED, re-check the id/secret (no quotes, no trailing spaces). If the guild is NOT FOUND,
compare `GUILD_NAME`/`GUILD_REALM` with the URL of your guild page on warcraftlogs.com.

Now sync:

```bash
kt sync
```

The first sync lists every raid report your guild has uploaded for the tracked expansions and fetches the boss
pulls for each (about 8 reports per API request), then pulls realm standings from Raider.IO. Expect 2–6 minutes for
a guild with a few hundred reports. Later runs are incremental and take seconds.

**Warcraft Logs API budget.** The public API allows 3,600 "points" per hour. A full first sync of ~300 reports uses
roughly 300–600 points; incremental syncs use a handful. `kt check` and the Status page show the current usage. If you
hit the limit the sync stops with a clear message – just re-run it after the hour resets; it resumes where it left off.

---

## Step 5 – (Optional) Enable Ask with Claude

1. Create an API key at https://console.anthropic.com/ (Settings → API keys) and add a little credit.
2. Add to `.env`:

   ```ini
   ANTHROPIC_API_KEY=sk-ant-...
   ASK_MODEL=claude-opus-5        # default; the most capable general model
   ASK_EFFORT=high                # low | medium | high | xhigh | max
   ASK_ENABLE_FALLBACKS=true      # server-side refusal fallbacks are on by default
   ```

3. Test from the terminal:

   ```bash
   kt ask "How many mythic bosses have we killed this tier and how many pulls did each take?" -v
   ```

Each question costs roughly $0.05–$0.30 depending on how many queries Claude needs. The database schema is sent as a
cached system prompt, so follow-up questions are cheaper. See [ASKING.md](ASKING.md) for what works well.

Refusal fallbacks are enabled by default: if a request is declined by the model's safety classifiers (very unlikely
for raid statistics), the API automatically re-runs it on a fallback model inside the same call. Set
`ASK_ENABLE_FALLBACKS=false` to turn that off.

---

## Step 6 – Run the web app

```bash
kt serve
```

Open http://127.0.0.1:8000. Pages:

- **Which team?** – the first screen (remembered on the device); "whole guild" is also an option.
- **Progress** – the team's current tier; switch tier and difficulty in the bar under the header. Peer numbers sit in
  the boss table.
- **Roster** – attendance and parses per raider. **Nights** – per-night pulls. **History** – tier over tier.
- **Peers** – guilds at our level in detail. **Realm** – rivals and realm standings (guild-wide, from Raider.IO).
- **Ask ✦** – the question box. Every chart has a *Table* toggle.
- **Status** – sync log, zone mapping, Warcraft Logs points, and *Sync now* / *Full re-sync* buttons.

To keep the data fresh automatically while the server runs:

```bash
kt serve --sync-every 60
```

---

## Step 7 – Keep it up to date

Options, from simplest to most robust:

1. Run `kt sync` after each raid night.
2. `kt serve --sync-every 60` (syncs hourly while the server is up).
3. A scheduler: cron `0 * * * * cd /path/to/killingtime && .venv/bin/kt sync >> data/sync.log 2>&1`, or Windows Task
   Scheduler running `kt sync`.
4. Docker with the bundled hourly sync – see [DEPLOY.md](DEPLOY.md).

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Warcraft Logs token request failed (401)` | Wrong id/secret, or you ticked *Public Client*. Create a new client with the box unchecked. |
| Guild not found on Warcraft Logs | Guild name is case-insensitive but must match exactly (spaces included). Realm goes in `GUILD_REALM` as displayed, e.g. `Twisting Nether`. |
| Reports exist but pulls are 0 | Reports are private/unlisted on Warcraft Logs. Set report visibility to public. |
| Rival shows no per-boss data | The guild hides pulls on Raider.IO (`guildPrivacy`) or is further down the realm than `RIO_REALM_SCAN_PAGES` × 100. Raise the page count. |
| A tier shows fewer bosses killed than the guild's all-time total | Progress is counted as it stood at the season cut-off: the date Cutting Edge and Ahead of the Curve stop being awarded, which is also when the Mythic+ season ends. A kill after it is shown as "post-season" and the tier header gives the all-time total. Cut-off dates come from Raider.IO's season data (the `-cutoffs` or `-post` season variant, else the season end), capped by the date the next raid opened. |
| A tier shows fewer bosses killed than we remember | Kills only reach the app through Warcraft Logs reports uploaded to the guild. A night logged personally, or never uploaded, has no pulls here. Raider.IO is used as the record of what the guild killed, so the boss still counts and is marked "no log" with no pull count. |
| Pull counts look about double what Raider.IO shows | Two people logged the same raid. The sync marks the second copy of every pull as a duplicate (same boss and difficulty, starting within 60 s in a different report) and every metric uses the remaining copy; the Status log line "duplicate pulls from second loggers hidden" shows how many. |
| A zone shows no Raider.IO mapping on Status | Set `TIER_MAP={"<zone id>": "<raider.io slug>"}`; slugs are visible in Raider.IO URLs. |
| `rate limit reached (429)` during sync | Wait for the hour to reset. Use `--skip-attendance` to save points, or lower `SYNC_EXPANSIONS`. |
| Ask says API key rejected | Check `ANTHROPIC_API_KEY`; restart `kt serve` after editing `.env`. |
| Ask answers are wrong about "current tier" | Run a sync so the newest zone has reports; Ask uses the newest zone with logs as "current". |
