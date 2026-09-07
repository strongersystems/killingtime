# Hosting it for the guild

## Cloudflare (Workers + Containers) - progress.killingtime.fyi

The repo ships a Cloudflare deployment: `wrangler.jsonc`, `worker/index.ts` and the `Dockerfile`. The Worker
proxies to one container running the FastAPI app, keeps the SQLite database alive across container restarts by
storing a gzipped snapshot in its Durable Object storage (`/_internal/db`), protects `/ask`, `/status` and the sync
endpoints with a shared password, and runs an incremental sync on a cron every two hours.

**Requirements**

- Workers **Paid** plan on the account (Containers are not available on the Free plan).
- An API token with: *Workers Scripts: Edit*, *Containers: Edit*, *Workers Routes: Edit* and *DNS: Edit* on the
  `killingtime.fyi` zone (the last two for the custom domain). Docker running locally.

**Deploy**

```bash
npm install
export CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID=...
npx wrangler secret put KT_STATE_SECRET      # any long random string (protects the snapshot endpoint)
npx wrangler secret put SITE_PASSWORD        # password the guild types for Ask / Status / sync
npx wrangler secret put ANTHROPIC_API_KEY
npx wrangler secret put WCL_CLIENT_ID
npx wrangler secret put WCL_CLIENT_SECRET
npx wrangler deploy                          # builds the image, pushes it, creates the container app
```

The deploy also attaches the custom domain `progress.killingtime.fyi` (the `routes` entry in `wrangler.jsonc`;
the zone must be on the same account and the token needs the DNS permissions above). To use a different hostname,
change both the `routes` pattern and `PUBLIC_URL` - the container uses `PUBLIC_URL` to reach the snapshot endpoint.

On first boot the container auto-syncs (Raider.IO immediately; Warcraft Logs once the WCL secrets exist), uploads a
snapshot, and the cron keeps it fresh. Rough cost: the `basic` instance only runs while requests or the cron keep it
awake (it sleeps after 30 minutes idle), so expect a few dollars a month on top of the $5 Workers Paid plan.

Building inside a sandbox that intercepts TLS? Pass the proxy CA as a build secret:
`docker build --network=host --secret id=ca,src=/path/ca.crt -t killingtime:0.1.0 .` then
`npx wrangler containers push killingtime:0.1.0` and point `containers[0].image` at the printed registry reference.

The app is a single Python process with a SQLite file. Anything that can run a container or a Python process works.

## Docker (recommended)

```bash
cp .env.example .env    # fill in as in SETUP.md
docker compose up -d --build
docker compose exec killingtime kt sync --full     # first sync
```

The container serves on port 8000 and runs an incremental sync every 60 minutes (see `Dockerfile` CMD). The database
lives in the `kt-data` volume. Logs: `docker compose logs -f`.

## A small VPS (Fly.io, Railway, Hetzner, a Raspberry Pi…)

1. Install Python 3.11+, clone the repo, `pip install -e .`, create `.env`.
2. Run under a process manager, for example systemd:

   ```ini
   [Unit]
   Description=Killing Time raid tracker
   After=network.target

   [Service]
   WorkingDirectory=/opt/killingtime
   ExecStart=/opt/killingtime/.venv/bin/kt serve --host 0.0.0.0 --port 8000 --sync-every 60
   Restart=always
   User=kt

   [Install]
   WantedBy=multi-user.target
   ```

3. Put it behind a reverse proxy (Caddy/nginx) for HTTPS.

## Access control

There is no login. The Ask page spends your Anthropic credit, and Status can trigger syncs, so **don't expose the
server to the open internet without protection**. Options:

- Keep it on a LAN / VPN (Tailscale is the easy answer for a guild).
- Put basic auth in the reverse proxy (Caddy: `basicauth`), or use Cloudflare Access.
- Or share only read-only pages by blocking `/ask`, `/api/ask`, `/sync`, `/api/sync` at the proxy.

## Backups

Copy the SQLite file (`data/killingtime.db`, or the Docker volume). Everything can be re-synced from the APIs anyway;
the only thing a backup saves is API budget and time.
