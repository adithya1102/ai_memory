/* Service worker: the only place that talks to the Bridge API.
 *
 * The content script runs inside chatgpt.com, and a page-context fetch to
 * 127.0.0.1 is a cross-origin request that the Bridge would have to whitelist
 * to allow.  It deliberately does not: a permissive Access-Control-Allow-Origin
 * on a local API that serves an entire conversation archive would let *any*
 * site the user visits read it.
 *
 * Fetching from here avoids that.  The extension's host_permissions cover
 * 127.0.0.1, the request is not subject to the page's origin, and no website
 * gains any access it did not already have.
 */
importScripts("lib/context.js");

var CV = self.CVContext;

var DEFAULTS = Object.assign({}, CV.DEFAULTS, {
  captureEnabled: true,
  capturePlatforms: { chatgpt: true }
});

function settings() {
  return new Promise(function (resolve) {
    chrome.storage.sync.get(DEFAULTS, function (stored) {
      var config = Object.assign({}, DEFAULTS, stored || {});
      config.capturePlatforms = Object.assign({}, DEFAULTS.capturePlatforms,
                                              config.capturePlatforms || {});
      resolve(config);
    });
  });
}

async function request(path, options) {
  var config = await settings();
  var base = String(config.baseUrl || CV.DEFAULTS.baseUrl).replace(/\/+$/, "");
  var opts = Object.assign({ credentials: "omit", cache: "no-store" },
                           options || {});
  opts.headers = Object.assign(CV.requestHeaders(config.apiKey),
                               opts.headers || {});

  var response;
  try {
    response = await fetch(base + path, opts);
  } catch (error) {
    throw new Error(CV.explainError(error, base));
  }

  var payload = null;
  try { payload = await response.json(); } catch (ignored) { /* no body */ }

  if (!response.ok) {
    var detail = payload && payload.error ? payload.error : "";
    throw new Error(CV.explainError(
      new Error("HTTP " + response.status + (detail ? ": " + detail : "")),
      base));
  }
  return payload;
}

// ---------------------------------------------------------------------
// Search
// ---------------------------------------------------------------------

async function search(query, limitOverride) {
  var config = await settings();
  var limit = limitOverride || config.limit;
  // buildSearchUrl owns the encoding and the limit clamping; request() adds
  // the base, so strip the one buildSearchUrl put on.
  var full = CV.buildSearchUrl(config.baseUrl, query, limit);
  return request(full.slice(full.indexOf("/api/v1")));
}

// ---------------------------------------------------------------------
// Capture
// ---------------------------------------------------------------------

/* How many messages the archive already holds for this conversation.
 *
 * A captured thread and the same thread imported from the official export
 * share an id, so a capture that read a short or half-rendered page could
 * otherwise replace a complete import with a truncated one.  Returns -1 when
 * the conversation is not stored yet, which is not a reason to refuse.
 */
async function storedMessageCount(conversationId) {
  try {
    var record = await request("/api/v1/conversations/"
                               + encodeURIComponent(conversationId));
    return (record && typeof record.message_count === "number")
      ? record.message_count : -1;
  } catch (error) {
    return -1;   // absent, or unreachable; the caller decides what that means
  }
}

async function ingest(payload, options) {
  options = options || {};
  var config = await settings();

  var conversation = payload && payload.conversations
    && payload.conversations[0];
  if (!conversation) throw new Error("nothing to send");

  if (!options.force) {
    if (!config.captureEnabled) return { skipped: "capture is off" };
    var platform = conversation.provider;
    if (platform && config.capturePlatforms[platform] === false) {
      return { skipped: "capture is off for " + platform };
    }
  }

  var existing = await storedMessageCount(conversation.conversation_id);
  if (existing > conversation.messages.length) {
    // The archive knows more than this page is showing. Overwriting would
    // lose turns, and the API replaces messages wholesale on update.
    return {
      skipped: "archive already holds " + existing + " messages; page shows "
               + conversation.messages.length
    };
  }

  var result = await request("/api/v1/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload)
  });
  return result;
}

// ---------------------------------------------------------------------
// Messages from the content script and the popup
// ---------------------------------------------------------------------

var HANDLERS = {
  "contextvault:search": function (message) {
    return search(message.query, message.limit);
  },
  "contextvault:ingest": function (message) {
    return ingest(message.payload, { force: message.force });
  },
  "contextvault:health": function () {
    return request("/api/v1/health");
  },
  "contextvault:settings": function () {
    return settings();
  }
};

chrome.runtime.onMessage.addListener(function (message, _sender, sendResponse) {
  var handler = message && HANDLERS[message.type];
  if (!handler) return false;

  Promise.resolve(handler(message)).then(function (data) {
    sendResponse({ ok: true, data: data });
  }).catch(function (error) {
    sendResponse({ ok: false, error: error.message || String(error) });
  });

  return true;  // keep the channel open for the async reply
});

/* A first-run nudge: the extension is useless until it can reach the app. */
chrome.runtime.onInstalled.addListener(function (details) {
  if (details.reason === "install") {
    chrome.runtime.openOptionsPage();
  }
});
