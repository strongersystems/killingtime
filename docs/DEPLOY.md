# Hosting it for the guild

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
