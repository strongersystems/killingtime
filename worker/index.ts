/**
 * Cloudflare Worker that fronts the Killing Time container.
 *
 *  - progress.killingtime.fyi (PUBLIC_URL): proxies every request to a single container instance ("main") running
 *    the FastAPI app on port 8000, with an optional shared password (SITE_PASSWORD) on the pages that cost money or
 *    trigger work.
 *  - killingtime.fyi / www (SITE_HOSTS): the public guild site. The container renders it after every sync and PUTs
 *    the HTML to /_internal/public (and each extra page, such as Meet the Team, to /_internal/public/<name>); the
 *    Worker serves those copies from Durable Object storage, so visitors never wake the container. If nothing has
 *    been published yet the request falls through to the container's live page. /static/* is proxied to the
 *    container and cached at the edge, which is how the member portraits reach the public site.
 *  - /_internal/db: the container uploads/downloads a gzipped SQLite snapshot here; it is stored in the
 *    Durable Object's own SQLite storage in 1 MB chunks, so the data survives container restarts.
 *  - Cron trigger: wakes the container and starts an incremental sync.
 */
import { Container, getContainer } from "@cloudflare/containers";

export interface Env {
  KT_CONTAINER: DurableObjectNamespace<KTContainer>;
  PUBLIC_URL: string;
  SITE_HOSTS?: string;
  SITE_URL?: string;
  SITE_TAGLINE?: string;
  SITE_ABOUT?: string;
  SITE_RAID_TIMES?: string;
  SITE_RECRUITING?: string;
  SITE_APPLY_URL?: string;
  SITE_DISCORD_URL?: string;
  RAID_TEAMS?: string;
  SYNC_EXPANSIONS?: string;
  SITE_PASSWORD?: string;
  KT_STATE_SECRET: string;
  WCL_CLIENT_ID?: string;
  WCL_CLIENT_SECRET?: string;
  ANTHROPIC_API_KEY?: string;
  GUILD_NAME?: string;
  GUILD_REALM?: string;
  GUILD_REGION?: string;
  RIVAL_GUILDS?: string;
  TIER_MAP?: string;
  ASK_MODEL?: string;
  ASK_EFFORT?: string;
}

const CHUNK = 1024 * 1024;
const PROTECTED = ["/ask", "/api/ask", "/sync", "/api/sync", "/status"];
const SECRET_HEADER = "X-KT-Secret";
const MIME: Record<string, string> = {
  webp: "image/webp", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", mp4: "video/mp4",
  svg: "image/svg+xml", css: "text/css; charset=utf-8", js: "text/javascript; charset=utf-8",
};

export class KTContainer extends Container<Env> {
  defaultPort = 8000;
  sleepAfter = "30m";

  constructor(ctx: DurableObjectState<{}>, env: Env) {
    super(ctx, env);
    this.envVars = {
      KT_HOST: "0.0.0.0",
      KT_PORT: "8000",
      KT_DB_PATH: "/data/killingtime.db",
      KT_STATE_URL: env.PUBLIC_URL,
      KT_STATE_SECRET: env.KT_STATE_SECRET,
      KT_AUTO_SYNC: "true",
      WCL_CLIENT_ID: env.WCL_CLIENT_ID ?? "",
      WCL_CLIENT_SECRET: env.WCL_CLIENT_SECRET ?? "",
      ANTHROPIC_API_KEY: env.ANTHROPIC_API_KEY ?? "",
      GUILD_NAME: env.GUILD_NAME ?? "Killing Time",
      GUILD_REALM: env.GUILD_REALM ?? "Draenor",
      GUILD_REGION: env.GUILD_REGION ?? "EU",
      RIVAL_GUILDS: env.RIVAL_GUILDS ?? "",
      RAID_TEAMS: env.RAID_TEAMS ?? "",
      SYNC_EXPANSIONS: env.SYNC_EXPANSIONS ?? "2",
      TIER_MAP: env.TIER_MAP ?? "",
      ASK_MODEL: env.ASK_MODEL ?? "claude-opus-5",
      ASK_EFFORT: env.ASK_EFFORT ?? "high",
      SITE_URL: env.SITE_URL ?? "",
      SITE_TAGLINE: env.SITE_TAGLINE ?? "",
      SITE_ABOUT: env.SITE_ABOUT ?? "",
      SITE_RAID_TIMES: env.SITE_RAID_TIMES ?? "",
      SITE_RECRUITING: env.SITE_RECRUITING ?? "",
      SITE_APPLY_URL: env.SITE_APPLY_URL ?? "",
      SITE_DISCORD_URL: env.SITE_DISCORD_URL ?? "",
    };
    this.ctx.storage.sql.exec(
      "CREATE TABLE IF NOT EXISTS snapshot (idx INTEGER PRIMARY KEY, data BLOB NOT NULL); " +
        "CREATE TABLE IF NOT EXISTS snapshot_meta (key TEXT PRIMARY KEY, value TEXT); " +
        "CREATE TABLE IF NOT EXISTS pages (name TEXT PRIMARY KEY, html TEXT NOT NULL, saved_at TEXT NOT NULL)",
    );
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/_internal/db") {
      return this.handleSnapshot(request);
    }
    if (url.pathname === "/_internal/public") {
      return this.handlePage(request, "public");
    }
    // Extra public pages (currently "team"): same storage, one row per page name.
    if (url.pathname.startsWith("/_internal/public/")) {
      const name = url.pathname.slice("/_internal/public/".length);
      if (/^[a-z0-9-]{1,32}$/.test(name)) return this.handlePage(request, name);
      return new Response("bad page name", { status: 400 });
    }
    return super.fetch(request);
  }

  /** Stored copy of the public site. Handled here (before super.fetch) so reads never start the container. */
  private async handlePage(request: Request, name: string): Promise<Response> {
    if (request.headers.get(SECRET_HEADER) !== this.env.KT_STATE_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const sql = this.ctx.storage.sql;
    if (request.method === "GET") {
      const row = [...sql.exec<{ html: string; saved_at: string }>("SELECT html, saved_at FROM pages WHERE name = ?", name)][0];
      if (!row) return new Response("no page", { status: 404 });
      return new Response(row.html, {
        headers: { "Content-Type": "text/html; charset=utf-8", "X-Saved-At": row.saved_at },
      });
    }
    if (request.method === "PUT") {
      const html = await request.text();
      if (html.length < 256 || !/<html/i.test(html)) return new Response("not an html page", { status: 400 });
      sql.exec("INSERT OR REPLACE INTO pages (name, html, saved_at) VALUES (?, ?, ?)", name, html, new Date().toISOString());
      return new Response(JSON.stringify({ ok: true, bytes: html.length }), { headers: { "Content-Type": "application/json" } });
    }
    return new Response("method not allowed", { status: 405 });
  }

  private async handleSnapshot(request: Request): Promise<Response> {
    if (request.headers.get(SECRET_HEADER) !== this.env.KT_STATE_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const sql = this.ctx.storage.sql;
    if (request.method === "GET") {
      const rows = [...sql.exec<{ idx: number; data: ArrayBuffer }>("SELECT idx, data FROM snapshot ORDER BY idx")];
      if (rows.length === 0) return new Response("no snapshot", { status: 404 });
      const total = rows.reduce((n, r) => n + r.data.byteLength, 0);
      const out = new Uint8Array(total);
      let off = 0;
      for (const r of rows) {
        out.set(new Uint8Array(r.data), off);
        off += r.data.byteLength;
      }
      const meta = [...sql.exec<{ value: string }>("SELECT value FROM snapshot_meta WHERE key = 'saved_at'")][0];
      return new Response(out, {
        headers: { "Content-Type": "application/gzip", "X-Saved-At": meta?.value ?? "" },
      });
    }
    if (request.method === "PUT") {
      const body = new Uint8Array(await request.arrayBuffer());
      if (body.byteLength < 64) return new Response("empty snapshot rejected", { status: 400 });
      await this.ctx.storage.transaction(async () => {
        sql.exec("DELETE FROM snapshot");
        for (let i = 0, idx = 0; i < body.byteLength; i += CHUNK, idx++) {
          sql.exec("INSERT INTO snapshot (idx, data) VALUES (?, ?)", idx, body.slice(i, i + CHUNK).buffer);
        }
        sql.exec(
          "INSERT OR REPLACE INTO snapshot_meta (key, value) VALUES ('saved_at', ?), ('bytes', ?)",
          new Date().toISOString(),
          String(body.byteLength),
        );
      });
      return new Response(JSON.stringify({ ok: true, bytes: body.byteLength }), {
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response("method not allowed", { status: 405 });
  }
}

function unauthorized(): Response {
  return new Response("Password required", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="Killing Time", charset="UTF-8"' },
  });
}

function passwordOk(request: Request, env: Env): boolean {
  if (!env.SITE_PASSWORD) return true;
  const header = request.headers.get("Authorization") ?? "";
  if (!header.startsWith("Basic ")) return false;
  try {
    const decoded = atob(header.slice(6));
    const pw = decoded.slice(decoded.indexOf(":") + 1);
    return pw === env.SITE_PASSWORD;
  } catch {
    return false;
  }
}

function siteHosts(env: Env): string[] {
  return (env.SITE_HOSTS ?? "")
    .split(",")
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);
}

/** The public guild site: serve the published page from storage; fall back to the live container render. */
async function servePublicSite(request: Request, env: Env, url: URL): Promise<Response> {
  const container = getContainer(env.KT_CONTAINER, "main");
  const hosts = siteHosts(env);
  const apex = hosts[0];
  if (url.hostname !== apex && url.hostname.startsWith("www.")) {
    return Response.redirect(`https://${apex}${url.pathname}${url.search}`, 301);
  }
  if (url.pathname === "/progress" || url.pathname.startsWith("/progress/")) {
    return Response.redirect(env.PUBLIC_URL + url.pathname.slice("/progress".length) + url.search, 302);
  }
  if (url.pathname === "/_healthz") return new Response("ok");
  if (url.pathname === "/robots.txt") return new Response("User-agent: *\nAllow: /\n", { headers: { "Content-Type": "text/plain" } });
  // Portraits, CSS and the like come from the container, cached hard at the edge so it is woken rarely.
  if (url.pathname.startsWith("/static/")) {
    const asset = await container.fetch(new Request(`${env.PUBLIC_URL}${url.pathname}`));
    if (!asset.ok) return new Response("not found", { status: 404 });
    const headers = new Headers(asset.headers);
    headers.set("Cache-Control", "public, max-age=86400");
    const type = MIME[url.pathname.slice(url.pathname.lastIndexOf(".") + 1).toLowerCase()];
    if (type) headers.set("Content-Type", type);   // the container serves .webp as octet-stream
    return new Response(asset.body, { status: asset.status, headers });
  }
  // Page name -> stored page. "/" is the front page; "/team" is Meet the Team.
  const PAGES: Record<string, { name: string; live: string }> = {
    "/": { name: "public", live: "/public" },
    "/index.html": { name: "public", live: "/public" },
    "/team": { name: "team", live: "/public/team" },
    "/team/": { name: "team", live: "/public/team" },
    "/meet": { name: "team", live: "/public/team" },
    "/join": { name: "join", live: "/public/join" },
    "/join/": { name: "join", live: "/public/join" },
    "/recruit": { name: "join", live: "/public/join" },
  };
  const page = PAGES[url.pathname];
  if (!page) {
    return Response.redirect(`https://${apex}/`, 302);
  }
  const path = page.name === "public" ? "/_internal/public" : `/_internal/public/${page.name}`;
  const stored = await container.fetch(
    new Request(`${env.PUBLIC_URL}${path}`, { headers: { [SECRET_HEADER]: env.KT_STATE_SECRET } }),
  );
  if (stored.ok) {
    return new Response(stored.body, {
      headers: {
        "Content-Type": "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=300",
        "Last-Modified": new Date(stored.headers.get("X-Saved-At") || Date.now()).toUTCString(),
      },
    });
  }
  // Nothing published yet (first deploy, or a page added since the last sync): render live from the container.
  const live = await container.fetch(new Request(`${env.PUBLIC_URL}${page.live}`, { headers: request.headers }));
  return new Response(live.body, { status: live.status, headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" } });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    if (siteHosts(env).includes(url.hostname.toLowerCase())) {
      return servePublicSite(request, env, url);
    }
    if (url.pathname === "/_healthz") return new Response("ok");
    if (PROTECTED.some((p) => url.pathname === p || url.pathname.startsWith(p + "/")) && !passwordOk(request, env)) {
      return unauthorized();
    }
    const container = getContainer(env.KT_CONTAINER, "main");
    return container.fetch(request);
  },

  async scheduled(_controller: ScheduledController, env: Env, ctx: ExecutionContext): Promise<void> {
    const container = getContainer(env.KT_CONTAINER, "main");
    ctx.waitUntil(
      container
        .fetch(new Request(`${env.PUBLIC_URL}/api/sync`, { method: "POST" }))
        .then((r) => console.log("scheduled sync:", r.status))
        .catch((e) => console.error("scheduled sync failed", e)),
    );
  },
} satisfies ExportedHandler<Env>;
