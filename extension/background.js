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

function settings() {
  return new Promise(function (resolve) {
    chrome.storage.sync.get(CV.DEFAULTS, function (stored) {
      resolve(Object.assign({}, CV.DEFAULTS, stored || {}));
    });
  });
}

async function search(query, limitOverride) {
  var config = await settings();
  var limit = limitOverride || config.limit;
  var url = CV.buildSearchUrl(config.baseUrl, query, limit);

  var response;
  try {
    response = await fetch(url, {
      method: "GET",
      headers: CV.requestHeaders(config.apiKey),
      // The archive is local and private; never attach site cookies to it.
      credentials: "omit",
      cache: "no-store"
    });
  } catch (error) {
    throw new Error(CV.explainError(error, config.baseUrl));
  }

  if (!response.ok) {
    var detail = "";
    try {
      var payload = await response.json();
      detail = payload && payload.error ? payload.error : "";
    } catch (ignored) { /* body was not JSON */ }
    throw new Error(CV.explainError(
      new Error("HTTP " + response.status + (detail ? ": " + detail : "")),
      config.baseUrl));
  }

  return response.json();
}

chrome.runtime.onMessage.addListener(function (message, _sender, sendResponse) {
  if (!message || message.type !== "contextvault:search") return false;

  search(message.query, message.limit).then(function (data) {
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
