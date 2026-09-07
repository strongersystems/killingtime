/**
 * Cloudflare Worker that fronts the Killing Time container.
 *
 *  - Proxies every request to a single container instance ("main") running the FastAPI app on port 8000.
 *  - Optional shared password (SITE_PASSWORD secret) on the pages that cost money or trigger work.
 *  - /_internal/db: the container uploads/downloads a gzipped SQLite snapshot here; it is stored in the
 *    Durable Object's own SQLite storage in 1 MB chunks, so the data survives container restarts.
 *  - Cron trigger: wakes the container and starts an incremental sync.
 */
import { Container, getContainer } from "@cloudflare/containers";

export interface Env {
  KT_CONTAINER: DurableObjectNamespace<KTContainer>;
  PUBLIC_URL: string;
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
      TIER_MAP: env.TIER_MAP ?? "",
      ASK_MODEL: env.ASK_MODEL ?? "claude-opus-5",
      ASK_EFFORT: env.ASK_EFFORT ?? "high",
    };
    this.ctx.storage.sql.exec(
      "CREATE TABLE IF NOT EXISTS snapshot (idx INTEGER PRIMARY KEY, data BLOB NOT NULL); " +
        "CREATE TABLE IF NOT EXISTS snapshot_meta (key TEXT PRIMARY KEY, value TEXT)",
    );
  }

  override async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/_internal/db") {
      return this.handleSnapshot(request);
    }
    return super.fetch(request);
  }

  private async handleSnapshot(request: Request): Promise<Response> {
    if (request.headers.get("X-KT-Secret") !== this.env.KT_STATE_SECRET) {
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

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
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
