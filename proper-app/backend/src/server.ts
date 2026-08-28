import { resolve } from "node:path";
import { closeDatabase } from "./db";
import { errorResponse, json, parseId, regionFilters } from "./http";
import { metadata, points, protocolDetail } from "./repository";

const port = Number(process.env.PORT ?? 3001);
const appOrigin = process.env.APP_ORIGIN ?? "http://localhost:5173";
const frontendRoot = resolve(import.meta.dir, "../../frontend/dist");

function withCors(response: Response): Response {
  const headers = new Headers(response.headers);
  headers.set("access-control-allow-origin", appOrigin);
  headers.set("vary", "Origin");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

async function api(request: Request, url: URL): Promise<Response> {
  if (url.pathname === "/api/health") {
    return json({ status: "ok", runtime: "bun", database: "postgresql" });
  }
  const metaMatch = /^\/api\/elections\/([^/]+)\/meta$/.exec(url.pathname);
  if (metaMatch) {
    const result = await metadata(metaMatch[1]);
    return result ? json(result) : errorResponse(404, "Election not found");
  }
  const pointsMatch = /^\/api\/elections\/([^/]+)\/points$/.exec(url.pathname);
  if (pointsMatch) {
    const optionId = parseId(url.searchParams.get("option"), "option");
    return json({ points: await points(pointsMatch[1], optionId, regionFilters(url.searchParams)) }, {
      headers: { "cache-control": "private, max-age=60" }
    });
  }
  const detailMatch = /^\/api\/elections\/([^/]+)\/protocols\/(\d+)$/.exec(url.pathname);
  if (detailMatch) {
    const result = await protocolDetail(detailMatch[1], parseId(detailMatch[2], "protocol"));
    return result ? json(result) : errorResponse(404, "Protocol not found");
  }
  return errorResponse(404, "API route not found");
}

async function staticResponse(url: URL): Promise<Response> {
  const relative = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\//, "");
  const candidate = Bun.file(resolve(frontendRoot, relative));
  if (await candidate.exists()) return new Response(candidate);
  const index = Bun.file(resolve(frontendRoot, "index.html"));
  return await index.exists() ? new Response(index, { headers: { "content-type": "text/html; charset=utf-8" } }) : errorResponse(404, "Frontend has not been built");
}

const server = Bun.serve({
  port,
  async fetch(request) {
    const url = new URL(request.url);
    if (request.method === "OPTIONS") return withCors(new Response(null, { status: 204 }));
    if (request.method !== "GET") return withCors(errorResponse(405, "Read-only API"));
    try {
      const response = url.pathname.startsWith("/api/") ? await api(request, url) : await staticResponse(url);
      return withCors(response);
    } catch (error) {
      const message = error instanceof Error ? error.message : "Unknown server error";
      const status = /must be a positive integer/.test(message) ? 400 : 500;
      if (status === 500) console.error(error);
      return withCors(errorResponse(status, message));
    }
  }
});

console.log(`Proper election API listening on ${server.url}`);

for (const signal of ["SIGINT", "SIGTERM"] as const) {
  process.on(signal, async () => {
    await closeDatabase();
    server.stop(true);
    process.exit(0);
  });
}
