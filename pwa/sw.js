/* Service worker for the ContextVault PWA.
 *
 * Served from the site root (/sw.js) rather than /pwa/, because a worker's
 * default scope is the directory it is served from and this one has to
 * intercept /api/v1/* as well as the app shell.
 *
 * Two strategies, chosen by what the request is for:
 *
 *   app shell   cache-first    it changes only when the app is redeployed,
 *                              and it must open instantly with no network
 *   API reads   network-first  fresh data whenever the machine is reachable,
 *                              the last good copy when it is not
 *
 * Writes are never cached and never replayed. A POST that failed offline has
 * to fail visibly: silently retrying a "save memory" days later, against a
 * database the user has since changed, is worse than an error message.
 */

var VERSION = "contextvault-v1";
var SHELL_CACHE = VERSION + "-shell";
var API_CACHE = VERSION + "-api";

var SHELL = [
  "/app",
  "/pwa/index.html",
  "/pwa/app.js",
  "/pwa/style.css",
  "/pwa/lib/context.js",
  "/pwa/manifest.json",
  "/static/icon.svg"
];

/* Only these are worth keeping offline. /health is deliberately excluded: a
 * cached "ok" would claim the app is reachable when it is not. */
var CACHEABLE_API = [/^\/api\/v1\/search/, /^\/api\/v1\/conversations\//,
                     /^\/api\/v1\/memories$/];

self.addEventListener("install", function (event) {
  event.waitUntil(
    caches.open(SHELL_CACHE).then(function (cache) {
      // addAll is atomic: one 404 would leave the app permanently unusable
      // offline, so each file is added on its own and a miss is survivable.
      return Promise.all(SHELL.map(function (url) {
        return cache.add(url).catch(function (error) {
          console.warn("ContextVault SW: could not precache", url, error);
        });
      }));
    }).then(function () { return self.skipWaiting(); })
  );
});

self.addEventListener("activate", function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.filter(function (name) {
        return name.indexOf(VERSION) !== 0;
      }).map(function (name) {
        return caches.delete(name);
      }));
    }).then(function () { return self.clients.claim(); })
  );
});

function isCacheableApi(pathname) {
  return CACHEABLE_API.some(function (pattern) { return pattern.test(pathname); });
}

/* Network first, falling back to whatever was stored last. */
async function networkFirst(request) {
  var cache = await caches.open(API_CACHE);
  try {
    var response = await fetch(request);
    if (response && response.ok) {
      cache.put(request, response.clone());
    }
    return response;
  } catch (error) {
    var cached = await cache.match(request);
    if (cached) {
      // Mark it so the page can tell the user this is not live data.
      var headers = new Headers(cached.headers);
      headers.set("X-ContextVault-Cached", "1");
      return new Response(await cached.blob(), {
        status: cached.status, statusText: cached.statusText, headers: headers
      });
    }
    throw error;
  }
}

/* Cache first, refreshing in the background so the next launch is current. */
async function cacheFirst(request) {
  var cache = await caches.open(SHELL_CACHE);
  var cached = await cache.match(request);
  if (cached) {
    fetch(request).then(function (response) {
      if (response && response.ok) cache.put(request, response.clone());
    }).catch(function () { /* offline: the cached copy stands */ });
    return cached;
  }

  var response = await fetch(request);
  if (response && response.ok) cache.put(request, response.clone());
  return response;
}

self.addEventListener("fetch", function (event) {
  var request = event.request;
  if (request.method !== "GET") return;          // writes go straight through

  var url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.indexOf("/api/v1") === 0) {
    if (isCacheableApi(url.pathname)) {
      event.respondWith(networkFirst(request));
    }
    return;                                       // everything else stays live
  }

  if (url.pathname === "/app" || url.pathname.indexOf("/pwa/") === 0
      || url.pathname === "/static/icon.svg") {
    event.respondWith(cacheFirst(request));
    return;
  }

  // A navigation to anything else, offline, should land on the app rather
  // than the browser's error page.
  if (request.mode === "navigate") {
    event.respondWith(fetch(request).catch(function () {
      return caches.match("/app");
    }));
  }
});

/* Lets the page trigger an update without a full reload. */
self.addEventListener("message", function (event) {
  if (event.data === "skipWaiting") self.skipWaiting();
});
