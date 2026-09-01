/* Toolbar popup: the switches you want within one click of a conversation. */
(function () {
  "use strict";

  var CV = window.CVContext;
  var CVP = window.CVPlatforms;

  var DEFAULTS = Object.assign({}, CV.DEFAULTS, {
    captureEnabled: true,
    capturePlatforms: { chatgpt: true }
  });

  function $(id) { return document.getElementById(id); }

  function setStatus(text, tone) {
    var node = $("status");
    node.textContent = text || "";
    if (tone) node.setAttribute("data-tone", tone);
    else node.removeAttribute("data-tone");
  }

  function load() {
    return new Promise(function (resolve) {
      chrome.storage.sync.get(DEFAULTS, function (stored) {
        var config = Object.assign({}, DEFAULTS, stored || {});
        config.capturePlatforms = Object.assign({}, DEFAULTS.capturePlatforms,
                                                config.capturePlatforms || {});
        resolve(config);
      });
    });
  }

  function save(patch) {
    return new Promise(function (resolve) {
      chrome.storage.sync.set(patch, resolve);
    });
  }

  function activeTab() {
    return new Promise(function (resolve) {
      chrome.tabs.query({ active: true, currentWindow: true },
                        function (tabs) { resolve(tabs && tabs[0]); });
    });
  }

  /* One row per platform. Only ChatGPT can be captured today, so the rest are
   * shown disabled and labelled rather than hidden -- a missing row reads
   * like a bug, a greyed one reads like a roadmap. */
  function renderPlatforms(config) {
    var wrap = $("platforms");
    wrap.textContent = "";

    CVP.PLATFORMS.forEach(function (platform) {
      var supported = CVP.supportsCapture(platform);
      var row = document.createElement("div");
      row.className = "row";

      var label = document.createElement("label");
      label.setAttribute("for", "p-" + platform.id);
      label.textContent = platform.name;
      if (!supported) {
        var note = document.createElement("span");
        note.className = "note soon";
        note.textContent = "/context only — capture not supported yet";
        label.appendChild(note);
      }
      row.appendChild(label);

      var box = document.createElement("input");
      box.type = "checkbox";
      box.id = "p-" + platform.id;
      box.checked = supported && config.capturePlatforms[platform.id] !== false;
      box.disabled = !supported;
      box.addEventListener("change", function () {
        var platforms = Object.assign({}, config.capturePlatforms);
        platforms[platform.id] = box.checked;
        config.capturePlatforms = platforms;
        save({ capturePlatforms: platforms });
        setStatus(platform.name + (box.checked ? " on" : " off"), "ok");
      });
      row.appendChild(box);

      wrap.appendChild(row);
    });
  }

  async function init() {
    var config = await load();

    $("captureEnabled").checked = !!config.captureEnabled;
    $("captureEnabled").addEventListener("change", function () {
      save({ captureEnabled: $("captureEnabled").checked });
      setStatus($("captureEnabled").checked
        ? "Auto-capture on." : "Auto-capture off.", "ok");
    });

    renderPlatforms(config);

    var tab = await activeTab();
    var host = "";
    try { host = tab && tab.url ? new URL(tab.url).hostname : ""; }
    catch (error) { host = ""; }
    var platform = CVP.forHostname(host);

    $("where").textContent = platform
      ? "On " + platform.name
        + (CVP.supportsCapture(platform) ? "" : " — /context only")
      : "Not on a supported chat site";
    $("capture-now").disabled = !(platform && CVP.supportsCapture(platform));

    $("capture-now").addEventListener("click", function () {
      if (!tab) return;
      setStatus("Capturing…");
      // The content script owns the page read; ask it to do one now.
      chrome.tabs.sendMessage(tab.id, { type: "contextvault:capture-now" },
        function () {
          if (chrome.runtime.lastError) {
            setStatus("Reload the page, then try again.", "bad");
          } else {
            setStatus("Asked the page to capture.", "ok");
          }
        });
    });

    $("open-options").addEventListener("click", function () {
      chrome.runtime.openOptionsPage();
    });

    $("open-app").addEventListener("click", function () {
      var base = String(config.baseUrl || CV.DEFAULTS.baseUrl)
        .replace(/\/+$/, "");
      chrome.tabs.create({ url: base + "/app" });
    });

    // Show whether the archive is actually reachable, since every other
    // control here is useless if it is not.
    chrome.runtime.sendMessage({ type: "contextvault:health" },
      function (response) {
        if (chrome.runtime.lastError) return;
        if (response && response.ok) {
          setStatus(response.data.conversations + " conversations indexed.",
                    "ok");
        } else {
          setStatus(response ? response.error : "ContextVault unreachable.",
                    "bad");
        }
      });
  }

  init();
})();
