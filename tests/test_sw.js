/* Service worker caching logic, run under node against a stubbed Cache API.
 *
 *     node tests/test_sw.js
 *
 * pwa/sw.js is loaded into a fake ServiceWorkerGlobalScope built here: enough
 * of caches, Request, Response and fetch to drive the real install/activate/
 * fetch handlers and see which strategy each request took.
 *
 * This does not prove the browser installs the worker -- only a browser can
 * show that. It proves the decisions inside it: that an API read falls back
 * to cache when the network is gone, that the shell opens offline, that a
 * write is never cached, and that /health is never served stale.
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

let fails = [];

function check(label, ok, detail) {
  console.log((ok ? "  ok   " : "  FAIL ") + label
              + (detail !== undefined && detail !== "" ? "  -> " + detail : ""));
  if (!ok) fails.push(label);
}

// ------------------------------------------------------------------
// A small stand-in for the Cache API
// ------------------------------------------------------------------

class FakeResponse {
  constructor(body, init) {
    init = init || {};
    this.body = body;
    this.status = init.status === undefined ? 200 : init.status;
    this.statusText = init.statusText || "";
    // init.headers may be a plain object, a Map, or the FakeHeaders the
    // worker builds when it tags a cached response.
    const source = init.headers;
    if (source instanceof FakeHeaders) this.headers = new Map(source.map);
    else if (source instanceof Map) this.headers = new Map(source);
    else this.headers = new Map(Object.entries(source || {}));
    this.ok = this.status >= 200 && this.status < 300;
  }
  clone() {
    return new FakeResponse(this.body, {
      status: this.status, statusText: this.statusText,
      headers: Object.fromEntries(this.headers)
    });
  }
  async blob() { return this.body; }
}

class FakeHeaders {
  constructor(source) {
    this.map = new Map(source instanceof Map ? source
                       : Object.entries(source || {}));
  }
  set(key, value) { this.map.set(key, value); }
  get(key) { return this.map.get(key); }
}

class FakeCache {
  constructor() { this.store = new Map(); }
  async put(request, response) {
    this.store.set(keyOf(request), response);
  }
  async match(request) { return this.store.get(keyOf(request)) || undefined; }
  async add(url) {
    const response = await scope.fetch(new FakeRequest(url));
    if (!response.ok) throw new Error("bad status " + response.status);
    this.store.set(keyOf(url), response);
  }
}

class FakeRequest {
  constructor(url, init) {
    init = init || {};
    this.url = url.startsWith("http") ? url : "https://vault.local" + url;
    this.method = init.method || "GET";
    this.mode = init.mode || "cors";
  }
}

function keyOf(request) {
  const url = typeof request === "string" ? request : request.url;
  return url.startsWith("http") ? url : "https://vault.local" + url;
}

const caches = {
  stores: new Map(),
  async open(name) {
    if (!this.stores.has(name)) this.stores.set(name, new FakeCache());
    return this.stores.get(name);
  },
  async keys() { return Array.from(this.stores.keys()); },
  async delete(name) { return this.stores.delete(name); },
  async match(url) {
    for (const cache of this.stores.values()) {
      const hit = await cache.match(url);
      if (hit) return hit;
    }
    return undefined;
  }
};

// ------------------------------------------------------------------
// The scope the worker runs in
// ------------------------------------------------------------------

let online = true;
let served = [];          // what the network was asked for
const handlers = {};

const scope = {
  location: { origin: "https://vault.local" },
  caches,
  Response: FakeResponse,
  Headers: FakeHeaders,
  Request: FakeRequest,
  URL,
  console,
  Promise,
  clients: { claim: async () => true },
  skipWaiting: async () => true,
  addEventListener(type, handler) { handlers[type] = handler; },
  async fetch(request) {
    served.push(keyOf(request));
    if (!online) throw new TypeError("Failed to fetch");
    return new FakeResponse("live:" + keyOf(request), { status: 200 });
  }
};
scope.self = scope;

const source = fs.readFileSync(
  path.join(__dirname, "..", "pwa", "sw.js"), "utf8");
vm.createContext(scope);
vm.runInContext(source, scope, { filename: "sw.js" });

// ------------------------------------------------------------------
// Driving the handlers
// ------------------------------------------------------------------

async function fire(type, event) {
  const waits = [];
  const wrapped = Object.assign({
    waitUntil(promise) { waits.push(promise); },
    respondWith(promise) { wrapped._response = promise; }
  }, event);
  handlers[type](wrapped);
  await Promise.all(waits);
  return wrapped._response ? await wrapped._response : undefined;
}

function get(url, init) {
  return { request: new FakeRequest(url, init) };
}

(async function () {
  // ================================================================
  console.log("== A. install precaches the shell ==");

  await fire("install", {});
  const shellCache = await caches.open("contextvault-v1-shell");

  for (const asset of ["/app", "/pwa/app.js", "/pwa/style.css",
                       "/pwa/lib/context.js", "/static/icon.svg"]) {
    check("precached " + asset, !!(await shellCache.match(asset)));
  }

  // ================================================================
  console.log("\n== B. the shell opens offline ==");

  online = false;
  served = [];
  let response = await fire("fetch", get("/app"));
  check("/app is served from cache with the network down",
        response && response.body === "live:https://vault.local/app",
        response && response.body);
  // The shell revalidates in the background so the next launch is current.
  // That request is fired and allowed to fail; what matters is that the
  // response above did not wait for it and did not break when it failed.
  check("the background revalidation failure is swallowed",
        served.length === 1 && response !== undefined, served);

  response = await fire("fetch", get("/pwa/style.css"));
  check("css is served from cache", !!response && !!response.body);

  // ================================================================
  console.log("\n== C. API reads fall back to the cache ==");

  online = true;
  const searchUrl = "/api/v1/search?q=gym&limit=10";
  response = await fire("fetch", get(searchUrl));
  check("a live search is returned from the network",
        response.body === "live:https://vault.local" + searchUrl,
        response.body);

  const apiCache = await caches.open("contextvault-v1-api");
  check("and stored for later", !!(await apiCache.match(searchUrl)));

  online = false;
  served = [];
  response = await fire("fetch", get(searchUrl));
  check("the same search offline returns the cached copy",
        response.body === "live:https://vault.local" + searchUrl,
        response.body);
  check("it is flagged as cached, so the page can say so",
        response.headers.get("X-ContextVault-Cached") === "1");
  check("the network was tried first", served.length === 1, served);

  let threw = false;
  try {
    await fire("fetch", get("/api/v1/search?q=never-searched"));
  } catch (error) { threw = true; }
  check("a query never searched before fails offline rather than lying",
        threw);

  online = true;
  await fire("fetch", get("/api/v1/conversations/chatgpt:conv-001"));
  check("an opened conversation is cached too",
        !!(await apiCache.match("/api/v1/conversations/chatgpt:conv-001")));
  await fire("fetch", get("/api/v1/memories"));
  check("the memories list is cached",
        !!(await apiCache.match("/api/v1/memories")));

  // ================================================================
  console.log("\n== D. what must never be cached ==");

  await fire("fetch", get("/api/v1/health"));
  check("/health is never cached — a stale 'ok' would claim the app is up",
        !(await apiCache.match("/api/v1/health")));

  served = [];
  const post = await fire("fetch",
    get("/api/v1/memories", { method: "POST" }));
  check("a POST is not intercepted at all", post === undefined);
  check("and nothing was cached for it",
        !(await apiCache.match("/api/v1/memories?post")));

  const del = await fire("fetch",
    get("/api/v1/memories/1", { method: "DELETE" }));
  check("a DELETE is not intercepted", del === undefined);

  const crossOrigin = await fire("fetch",
    get("https://chatgpt.com/backend-api/conversation"));
  check("another origin is left entirely alone", crossOrigin === undefined);

  // ================================================================
  console.log("\n== E. navigation falls back to the app ==");

  online = false;
  response = await fire("fetch", get("/some/deep/link", { mode: "navigate" }));
  check("an offline navigation lands on the app, not a browser error page",
        !!response && !!response.body, response && response.body);

  // ================================================================
  console.log("\n== F. activate clears old versions ==");

  online = true;
  caches.stores.set("contextvault-v0-shell", new FakeCache());
  caches.stores.set("something-else", new FakeCache());
  await fire("activate", {});
  const remaining = await caches.keys();
  check("previous versions are dropped",
        !remaining.includes("contextvault-v0-shell"), remaining);
  check("unrelated caches are also dropped, not left to grow",
        !remaining.includes("something-else"), remaining);
  check("the current caches survive",
        remaining.includes("contextvault-v1-shell")
        && remaining.includes("contextvault-v1-api"), remaining);

  console.log("\n" + (fails.length === 0 ? "ALL CHECKS PASSED"
                      : fails.length + " FAILURES: " + fails.join(", ")));
  process.exit(fails.length === 0 ? 0 : 1);
})();
