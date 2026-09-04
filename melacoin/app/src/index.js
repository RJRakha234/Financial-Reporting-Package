"use strict";
/**
 * index.js - the web server.
 *
 * Start it with:  npm start
 * Then open:      http://localhost:4000
 *
 * There are no frameworks and no installed packages here. Everything below is
 * either Node's own standard library or a file in this project, so you can follow
 * a request from the browser all the way to the database by reading the code.
 */
const http = require("node:http");
const config = require("./config");
const db = require("./db");
const auth = require("./auth");
const httpKit = require("./http");
const { RuleError } = require("./errors");

const { Router, HttpError, readJson, sendJson, serveStatic, createRateLimiter } = httpKit;

function buildRouter() {
  const router = new Router();
  require("./routes/public.routes").register(router);
  require("./routes/auth.routes").register(router);
  require("./routes/customer.routes").register(router);
  require("./routes/vendor.routes").register(router);
  require("./routes/pos.routes").register(router);
  require("./routes/wallet.routes").register(router);
  require("./routes/admin.routes").register(router);
  return router;
}

function createApp() {
  const router = buildRouter();
  const generalLimiter = createRateLimiter({
    windowMs: config.rateLimit.windowMs,
    max: config.rateLimit.maxRequests,
  });
  const authLimiter = createRateLimiter({
    windowMs: config.rateLimit.windowMs,
    max: config.rateLimit.maxAuthAttempts,
  });

  return http.createServer(async (req, res) => {
    const clientIp = req.socket.remoteAddress || "unknown";
    let url;
    try {
      url = new URL(req.url, `http://${req.headers.host || "localhost"}`);
    } catch {
      return sendJson(res, 400, { error: "Malformed URL" });
    }

    // Anything that is not /api/... is a page or an asset.
    if (!url.pathname.startsWith("/api/")) {
      if (req.method !== "GET" && req.method !== "HEAD") {
        return sendJson(res, 405, { error: "Method not allowed" });
      }
      if (serveStatic(res, config.publicDir, url.pathname)) return;
      // Unknown page: fall back to the landing page so links keep working.
      if (serveStatic(res, config.publicDir, "/index.html")) return;
      return sendJson(res, 404, { error: "Not found" });
    }

    try {
      generalLimiter(clientIp);

      const matched = router.match(req.method, url.pathname);
      if (!matched) throw new HttpError(404, `No API endpoint at ${url.pathname}`);

      const body =
        req.method === "POST" || req.method === "PATCH" || req.method === "PUT" ? await readJson(req) : {};

      const ctx = buildContext({ req, res, url, body, params: matched.params, clientIp, authLimiter });
      const result = (await matched.handler(ctx)) || {};
      return sendJson(res, result.status || 200, result.body ?? { ok: true });
    } catch (error) {
      return handleError(res, error, req, url);
    }
  });
}

/** The object every route handler receives. */
function buildContext({ req, res, url, body, params, clientIp, authLimiter }) {
  const header = req.headers.authorization || "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : null;
  const user = token ? auth.userForToken(token) : null;

  return {
    req,
    res,
    body,
    params,
    query: url.searchParams,
    token,
    user,
    clientIp,

    throttleAuth: () => authLimiter(clientIp),

    requireUser() {
      if (!user) throw httpKit.unauthorized();
      return user;
    },

    requireRole(role) {
      if (!user) throw httpKit.unauthorized();
      if (user.role !== role) throw httpKit.forbidden(`This area is for ${role} accounts`);
      return user;
    },

    /** The logged-in shop owner, together with their shop. */
    requireVendor() {
      if (!user) throw httpKit.unauthorized();
      if (user.role !== "vendor") throw httpKit.forbidden("This area is for vendor accounts");
      const vendor = db.get().prepare("SELECT * FROM vendors WHERE owner_user_id = ?").get(user.id);
      if (!vendor) throw httpKit.notFound("No shop is linked to this account");
      return { user, vendor };
    },

    /** The shop identified by the x-api-key header, for till/POS calls. */
    requireApiKey() {
      const key = req.headers["x-api-key"];
      const vendor = auth.vendorForApiKey(Array.isArray(key) ? key[0] : key);
      if (!vendor) {
        authLimiter(clientIp);
        throw httpKit.unauthorized("Invalid or missing x-api-key");
      }
      return vendor;
    },
  };
}

function handleError(res, error, req, url) {
  if (error instanceof HttpError) {
    return sendJson(res, error.status, { error: error.message, details: error.details });
  }
  if (error instanceof RuleError) {
    return sendJson(res, 400, { error: error.message });
  }
  // Anything else is our fault. Log it in full, tell the user nothing useful to an attacker.
  console.error(`[error] ${req.method} ${url.pathname}`, error);
  return sendJson(res, 500, { error: "Something went wrong on our side. Please try again." });
}

function start() {
  db.open();
  auth.purgeExpiredSessions();

  const server = createApp();
  server.listen(config.port, config.host, () => {
    console.log(`\n  MelaCoin is running`);
    console.log(`  ---------------------------------------------`);
    console.log(`  App        http://${config.host}:${config.port}`);
    console.log(`  Database   ${config.dbPath}`);
    console.log(`  Chain mode ${config.chainMode}${config.chainMode === "mock" ? "  (no blockchain needed)" : ""}`);
    console.log(`\n  Seed demo data with:  npm run seed\n`);
  });

  // Tidy up expired sessions and points once an hour.
  const sweep = setInterval(() => {
    try {
      auth.purgeExpiredSessions();
      require("./services/loyalty.service").expireDuePoints();
    } catch (error) {
      console.error("[sweep]", error);
    }
  }, 3600_000);
  sweep.unref();

  const shutdown = () => {
    console.log("\nShutting down...");
    server.close(() => {
      db.close();
      process.exit(0);
    });
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  return server;
}

if (require.main === module) start();

module.exports = { createApp, start, buildRouter };
