"use strict";
/**
 * http.js - a very small web framework, written out so there is no magic.
 *
 * It does four things: match a URL to a handler, parse JSON bodies safely, serve
 * the files in /public, and turn thrown errors into clean JSON responses.
 */
const fs = require("node:fs");
const path = require("node:path");

const MAX_BODY_BYTES = 256 * 1024;

/** An error with an HTTP status attached. `throw new HttpError(404, "Not found")`. */
class HttpError extends Error {
  constructor(status, message, details) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

const badRequest = (message, details) => new HttpError(400, message, details);
const unauthorized = (message = "Sign in to continue") => new HttpError(401, message);
const forbidden = (message = "You do not have access to this") => new HttpError(403, message);
const notFound = (message = "Not found") => new HttpError(404, message);
const conflict = (message) => new HttpError(409, message);

class Router {
  constructor() {
    this.routes = [];
  }

  /** register("POST", "/api/vendors/:id", handler) */
  register(method, pattern, handler) {
    const names = [];
    const regexSource = pattern
      .split("/")
      .map((segment) => {
        if (!segment.startsWith(":")) return segment.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        names.push(segment.slice(1));
        return "([^/]+)";
      })
      .join("/");
    this.routes.push({ method, regex: new RegExp(`^${regexSource}$`), names, handler });
    return this;
  }

  get(p, h) { return this.register("GET", p, h); }
  post(p, h) { return this.register("POST", p, h); }
  patch(p, h) { return this.register("PATCH", p, h); }
  del(p, h) { return this.register("DELETE", p, h); }

  match(method, pathname) {
    let pathExists = false;
    for (const route of this.routes) {
      const found = route.regex.exec(pathname);
      if (!found) continue;
      pathExists = true;
      if (route.method !== method) continue;
      const params = {};
      route.names.forEach((name, index) => {
        params[name] = decodeURIComponent(found[index + 1]);
      });
      return { handler: route.handler, params };
    }
    // A known path with the wrong verb deserves 405, not a confusing 404.
    if (pathExists) throw new HttpError(405, `Method ${method} is not allowed here`);
    return null;
  }
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > MAX_BODY_BYTES) {
        reject(new HttpError(413, "Request body is too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

async function readJson(req) {
  const raw = await readBody(req);
  if (raw.length === 0) return {};
  try {
    const parsed = JSON.parse(raw.toString("utf8"));
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      throw badRequest("Request body must be a JSON object");
    }
    return parsed;
  } catch (error) {
    if (error instanceof HttpError) throw error;
    throw badRequest("Request body is not valid JSON");
  }
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
  });
  res.end(body);
}

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ico": "image/x-icon",
  ".json": "application/json; charset=utf-8",
  ".webmanifest": "application/manifest+json",
};

/** Serves a file from `rootDir`, refusing any path that tries to escape it. */
function serveStatic(res, rootDir, urlPath) {
  const relative = urlPath === "/" ? "index.html" : urlPath.replace(/^\/+/, "");
  const target = path.resolve(rootDir, relative);
  if (target !== rootDir && !target.startsWith(rootDir + path.sep)) return false;
  if (!fs.existsSync(target) || !fs.statSync(target).isFile()) return false;

  const body = fs.readFileSync(target);
  res.writeHead(200, {
    "content-type": MIME[path.extname(target)] || "application/octet-stream",
    "content-length": body.length,
    "cache-control": "no-cache",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "content-security-policy":
      "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
  });
  res.end(body);
  return true;
}

/** Simple in-memory request throttle, keyed by IP. Enough for one server. */
function createRateLimiter({ windowMs, max }) {
  const hits = new Map();
  return function check(key) {
    const nowMs = Date.now();
    const entry = hits.get(key);
    if (!entry || nowMs > entry.resetAt) {
      hits.set(key, { count: 1, resetAt: nowMs + windowMs });
      if (hits.size > 10_000) {
        for (const [k, v] of hits) if (nowMs > v.resetAt) hits.delete(k);
      }
      return;
    }
    entry.count += 1;
    if (entry.count > max) {
      throw new HttpError(429, "Too many requests. Please wait a moment and try again.");
    }
  };
}

module.exports = {
  HttpError,
  Router,
  readJson,
  sendJson,
  serveStatic,
  createRateLimiter,
  badRequest,
  unauthorized,
  forbidden,
  notFound,
  conflict,
};
