/* Options page: read and write the four settings, and prove the connection. */
(function () {
  "use strict";

  var CV = window.CVContext;
  var CVP = window.CVPlatforms;
  var fields = ["baseUrl", "apiKey", "limit", "autoSend", "captureEnabled"];

  var DEFAULTS = Object.assign({}, CV.DEFAULTS, {
    captureEnabled: true,
    capturePlatforms: { chatgpt: true }
  });

  function el(id) { return document.getElementById(id); }

  /* One checkbox per platform. Platforms without a capture adapter are shown
   * disabled rather than omitted: an absent row looks like a bug, a greyed
   * one reads as "not yet". */
  function renderPlatforms(config) {
    var wrap = el("platforms");
    if (!wrap) return;
    wrap.textContent = "";

    CVP.PLATFORMS.forEach(function (platform) {
      var supported = CVP.supportsCapture(platform);
      var row = document.createElement("div");
      row.className = "platform-row";

      var box = document.createElement("input");
      box.type = "checkbox";
      box.id = "capture-" + platform.id;
      box.disabled = !supported;
      box.checked = supported
        && config.capturePlatforms[platform.id] !== false;
      box.addEventListener("change", function () {
        config.capturePlatforms[platform.id] = box.checked;
        chrome.storage.sync.set(
          { capturePlatforms: config.capturePlatforms },
          function () { setStatus("Saved.", "ok"); });
      });
      row.appendChild(box);

      var label = document.createElement("label");
      label.setAttribute("for", box.id);
      label.textContent = platform.name
        + (supported ? "" : "  (/context only)");
      row.appendChild(label);

      wrap.appendChild(row);
    });
  }

  function setStatus(text, cls) {
    var node = el("status");
    node.textContent = text;
    node.className = cls || "";
  }

  chrome.storage.sync.get(DEFAULTS, function (stored) {
    var config = Object.assign({}, DEFAULTS, stored || {});
    config.capturePlatforms = Object.assign({}, DEFAULTS.capturePlatforms,
                                            config.capturePlatforms || {});
    fields.forEach(function (key) {
      var input = el(key);
      if (!input) return;
      if (input.type === "checkbox") input.checked = !!config[key];
      else input.value = config[key];
    });
    renderPlatforms(config);
  });

  el("save").addEventListener("click", function () {
    var config = {};
    fields.forEach(function (key) {
      var input = el(key);
      if (!input) return;
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
