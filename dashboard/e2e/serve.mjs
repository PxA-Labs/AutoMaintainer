// Minimal static file server for the Next static export in out/, used by the
// Playwright smoke tests. It mirrors how production serves the export
// (FastAPI StaticFiles(html=True)): directories resolve to index.html, so
// the trailingSlash routes (/admin/, /auth/callback/) work. Kept dependency
// free on purpose; it only ever serves local build output.
import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, normalize, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("../out", import.meta.url)));
const port = Number(process.env.E2E_PORT || 4173);
const host = "127.0.0.1";

const TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".txt": "text/plain; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
  ".woff": "font/woff",
  ".ttf": "font/ttf",
  ".map": "application/json; charset=utf-8",
};

async function isFile(path) {
  try {
    return (await stat(path)).isFile();
  } catch {
    return false;
  }
}

async function resolveFile(pathname) {
  const target = normalize(join(root, decodeURIComponent(pathname)));
  // Never serve anything outside out/.
  if (target !== root && !target.startsWith(root + sep)) return null;
  if (await isFile(target)) return target;
  const index = join(target, "index.html");
  if (await isFile(index)) return index;
  return null;
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://${host}:${port}`);
  let file = null;
  try {
    file = await resolveFile(url.pathname);
  } catch {
    // Malformed percent-encoding: treat as not found.
  }
  if (!file) {
    const notFound = join(root, "404.html");
    res.writeHead(404, { "Content-Type": TYPES[".html"] });
    if (await isFile(notFound)) createReadStream(notFound).pipe(res);
    else res.end("Not found");
    return;
  }
  res.writeHead(200, {
    "Content-Type": TYPES[extname(file)] || "application/octet-stream",
    "Cache-Control": "no-store",
  });
  createReadStream(file).pipe(res);
});

server.listen(port, host, () => {
  console.log(`Serving ${root} at http://${host}:${port}`);
});
