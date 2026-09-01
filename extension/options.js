/* Options page: read and write the four settings, and prove the connection. */
(function () {
  "use strict";

  var CV = window.CVContext;
  var fields = ["baseUrl", "apiKey", "limit", "autoSend"];

  function el(id) { return document.getElementById(id); }

  function setStatus(text, cls) {
    var node = el("status");
    node.textContent = text;
    node.className = cls || "";
  }

  chrome.storage.sync.get(CV.DEFAULTS, function (stored) {
    var config = Object.assign({}, CV.DEFAULTS, stored || {});
    fields.forEach(function (key) {
      var input = el(key);
      if (input.type === "checkbox") input.checked = !!config[key];
      else input.value = config[key];
    });
  });

  el("save").addEventListener("click", function () {
    var config = {};
    fields.forEach(function (key) {
      var input = el(key);
      config[key] = input.type === "checkbox" ? input.checked : input.value;
    });
    config.baseUrl = String(config.baseUrl || "").trim()
                     || CV.DEFAULTS.baseUrl;
    config.limit = Math.max(1, Math.min(20, parseInt(config.limit, 10) || 5));
    el("limit").value = config.limit;

    chrome.storage.sync.set(config, function () {
      setStatus("Saved.", "ok");
    });
  });

  /* Hit /health rather than /search: it is unauthenticated, so a failure here
   * separates "the app is not running" from "the key is wrong". */
  el("test").addEventListener("click", async function () {
    var base = String(el("baseUrl").value || "").trim() || CV.DEFAULTS.baseUrl;
    setStatus("Checking…");
    try {
      var response = await fetch(base.replace(/\/+$/, "") + "/api/v1/health",
                                 { credentials: "omit", cache: "no-store" });
      if (!response.ok) throw new Error("HTTP " + response.status);
      var health = await response.json();
      var needsKey = health.auth_required && !el("apiKey").value.trim();
      setStatus(
        needsKey
          ? "Reached it — but auth is on and no key is set here."
          : "Connected. " + health.conversations + " conversations indexed.",
        needsKey ? "bad" : "ok");
    } catch (error) {
      setStatus(CV.explainError(error, base), "bad");
    }
  });
})();
