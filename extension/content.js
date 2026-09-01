/* Content script.
 *
 * Three jobs on the page:
 *
 *   1. /context   intercept Enter on a "/context ..." line, search the local
 *                 archive, and offer the results in a panel to insert.
 *   2. capture    watch the transcript and send it to the Bridge API, batched
 *                 at five new messages or thirty seconds.
 *   3. launcher   a small button that opens the same panel by hand.
 *
 * Capture is ChatGPT-only for now. The other platforms are detected and get
 * /context, but reading their transcripts back out needs an adapter checked
 * against each site's real markup; guessing produces half-conversations and
 * mislabelled speakers, which is worse than not capturing at all.
 */
(function () {
  "use strict";

  var CV = window.CVContext;
  var CVP = window.CVPlatforms;
  var CVC = window.CVCapture;
  if (!CV || !CVP || !CVC) return;

  var platform = CVP.forHostname(location.hostname);
  if (!platform) return;

  var settings = null;
  var batcher = new CVC.Batcher();
  var flushing = false;
  var lastUrl = location.href;

  // ------------------------------------------------------------------
  // Talking to the service worker
  // ------------------------------------------------------------------

  function send(type, extra) {
    return new Promise(function (resolve, reject) {
      chrome.runtime.sendMessage(Object.assign({ type: type }, extra || {}),
        function (response) {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else if (!response || !response.ok) {
            reject(new Error(response ? response.error : "no response"));
          } else {
            resolve(response.data);
          }
        });
    });
  }

  function loadSettings() {
    return send("contextvault:settings").then(function (config) {
      settings = config;
      return config;
    }).catch(function () {
      settings = { captureEnabled: false, capturePlatforms: {}, limit: 5 };
      return settings;
    });
  }

  // ------------------------------------------------------------------
  // Composer
  // ------------------------------------------------------------------

  function isEditor(node) {
    if (!node || node.nodeType !== 1) return false;
    if (node.tagName === "TEXTAREA") return true;
    return node.getAttribute
        && node.getAttribute("contenteditable") === "true";
  }

  function findEditor(target) {
    if (isEditor(target)) return target;
    if (isEditor(document.activeElement)) return document.activeElement;
    for (var i = 0; i < platform.composer.length; i++) {
      var found = document.querySelector(platform.composer[i]);
      if (isEditor(found)) return found;
    }
    return null;
  }

  function readEditor(editor) {
    if (!editor) return "";
    return editor.tagName === "TEXTAREA" ? editor.value : editor.innerText;
  }

  /* These editors are React/ProseMirror/Quill controlled inputs: assigning to
   * .value or .innerText updates the DOM but leaves the framework's state
   * stale, and the send button reads the framework's state. */
  function writeEditor(editor, value) {
    editor.focus();

    if (editor.tagName === "TEXTAREA") {
      var setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, "value").set;
      setter.call(editor, value);
      editor.dispatchEvent(new Event("input", { bubbles: true }));
      return;
    }

    var selection = window.getSelection();
    var range = document.createRange();
    range.selectNodeContents(editor);
    selection.removeAllRanges();
    selection.addRange(range);

    var ok = false;
    try { ok = document.execCommand("insertText", false, value); }
    catch (error) { ok = false; }
    if (!ok) {
      editor.textContent = value;
      editor.dispatchEvent(new InputEvent("input", {
        bubbles: true, inputType: "insertText", data: value
      }));
    }
  }

  // ------------------------------------------------------------------
  // Status pill
  // ------------------------------------------------------------------

  var pill = null;
  var pillTimer = null;

  function showStatus(text, tone) {
    if (!pill) {
      pill = document.createElement("div");
      pill.className = "cv-status";
      document.body.appendChild(pill);
    }
    pill.textContent = text;
    pill.setAttribute("data-tone", tone || "info");
    pill.classList.add("cv-visible");
    clearTimeout(pillTimer);
    if (tone !== "busy") {
      pillTimer = setTimeout(function () {
        pill.classList.remove("cv-visible");
      }, 5000);
    }
  }

  // ------------------------------------------------------------------
  // The panel
  // ------------------------------------------------------------------

  var panel = null;

  function buildPanel() {
    if (panel) return panel;

    panel = document.createElement("div");
    panel.className = "cv-panel";
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "ContextVault");
    panel.innerHTML =
      '<div class="cv-panel-head">' +
        '<strong>ContextVault</strong>' +
        '<button class="cv-x" type="button" aria-label="Close">&times;</button>' +
      '</div>' +
      '<form class="cv-panel-search">' +
        '<input type="search" placeholder="Search your history…" ' +
               'aria-label="Search your history">' +
        '<button type="submit">Search</button>' +
      '</form>' +
      '<div class="cv-panel-status"></div>' +
      '<div class="cv-panel-results"></div>' +
      '<div class="cv-panel-foot">' +
        '<button class="cv-insert-all" type="button">Insert all</button>' +
        '<button class="cv-capture-now" type="button">Capture this chat</button>' +
      '</div>';

    document.body.appendChild(panel);

    panel.querySelector(".cv-x").addEventListener("click", hidePanel);
    panel.querySelector(".cv-panel-search")
      .addEventListener("submit", function (event) {
        event.preventDefault();
        var value = panel.querySelector("input").value.trim();
        if (value) runSearch(value);
      });
    panel.querySelector(".cv-insert-all")
      .addEventListener("click", function () { insertAll(); });
    panel.querySelector(".cv-capture-now")
      .addEventListener("click", function () { captureNow(); });

    return panel;
  }

  function showPanel(query) {
    buildPanel().classList.add("cv-open");
    var input = panel.querySelector("input");
    if (query !== undefined) input.value = query;
    input.focus();
  }

  function hidePanel() {
    if (panel) panel.classList.remove("cv-open");
  }

  function panelStatus(text, tone) {
    if (!panel) return;
    var node = panel.querySelector(".cv-panel-status");
    node.textContent = text || "";
    node.setAttribute("data-tone", tone || "info");
  }

  var lastResults = [];
  var lastQuery = "";

  function renderResults(query, results) {
    lastResults = results || [];
    lastQuery = query;
    var wrap = panel.querySelector(".cv-panel-results");
    wrap.textContent = "";

    if (!lastResults.length) {
      var none = document.createElement("p");
      none.className = "cv-empty";
      none.textContent = "Nothing matched.";
      wrap.appendChild(none);
      return;
    }

    lastResults.forEach(function (result) {
      var card = document.createElement("div");
      card.className = "cv-result";

      var title = document.createElement("div");
      title.className = "cv-result-title";
      title.textContent = result.title || "Untitled conversation";
      card.appendChild(title);

      var meta = document.createElement("div");
      meta.className = "cv-result-meta";
      meta.textContent = (result.provider || "unknown") + " · "
        + String(result.date || "").slice(0, 10)
        + " · " + (result.match_type || "match");
      card.appendChild(meta);

      if (result.snippet) {
        var snippet = document.createElement("p");
        snippet.className = "cv-result-snippet";
        snippet.textContent = CV.truncate(result.snippet, 260);
        card.appendChild(snippet);
      }

      var insert = document.createElement("button");
      insert.type = "button";
      insert.className = "cv-insert";
      insert.textContent = "Insert";
      insert.addEventListener("click", function () { insertOne(result); });
      card.appendChild(insert);

      wrap.appendChild(card);
    });
  }

  function insertBlock(results) {
    var editor = findEditor(null);
    if (!editor) {
      showStatus("ContextVault: could not find the message box", "error");
      return;
    }
    // Anything the user had already typed is the real question; keep it.
    var typed = readEditor(editor).trim();
    var parsed = CV.parseCommand(typed);
    var question = parsed ? parsed.query : typed;

    writeEditor(editor, CV.formatContextBlock(question || lastQuery, results,
                                              { snippetLength: 400 }));
    hidePanel();
    showStatus(CV.statusText("ok", results.length), "ok");
  }

  function insertOne(result) { insertBlock([result]); }
  function insertAll() {
    if (lastResults.length) insertBlock(lastResults);
  }

  function runSearch(query) {
    showPanel(query);
    panelStatus("Searching…");
    return send("contextvault:search",
                { query: query, limit: (settings && settings.limit) || 5 })
      .then(function (data) {
        var results = (data && data.results) || [];
        renderResults(query, results);
        panelStatus(results.length
          ? results.length + " result" + (results.length === 1 ? "" : "s")
          : "No matches.", results.length ? "ok" : "warn");
        return results;
      })
      .catch(function (error) {
        panelStatus(CV.explainError(error, ""), "error");
      });
  }

  // ------------------------------------------------------------------
  // Launcher button
  // ------------------------------------------------------------------

  function addLauncher() {
    if (document.querySelector(".cv-launcher")) return;
    var button = document.createElement("button");
    button.className = "cv-launcher";
    button.type = "button";
    button.title = "ContextVault — search your history";
    button.setAttribute("aria-label", "Open ContextVault");
    button.innerHTML =
      '<svg viewBox="0 0 24 24" aria-hidden="true">' +
        '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="10" r="3"/>' +
        '<path d="M12 13v4"/></svg>';
    button.addEventListener("click", function () {
      if (panel && panel.classList.contains("cv-open")) hidePanel();
      else showPanel("");
    });
    document.body.appendChild(button);
  }

  // ------------------------------------------------------------------
  // Capture
  // ------------------------------------------------------------------

  function captureAllowed() {
    if (!CVP.supportsCapture(platform)) return false;
    if (!settings || !settings.captureEnabled) return false;
    return settings.capturePlatforms[platform.id] !== false;
  }

  function readPage() {
    return CVP.readConversation(platform, document, location.href);
  }

  async function flush(force) {
    if (flushing) return;
    var payload = batcher.payload();
    if (!payload) return;

    flushing = true;
    var count = batcher.messages.length;
    try {
      var result = await send("contextvault:ingest",
                              { payload: payload, force: !!force });
      if (result && result.skipped) {
        if (force) showStatus("ContextVault: " + result.skipped, "warn");
      } else {
        batcher.markSent(count);
        if (force) showStatus("ContextVault: saved " + count + " messages",
                              "ok");
      }
    } catch (error) {
      // Leave the batch pending; the next tick will try again. A failed
      // capture must not lose the transcript.
      if (force) showStatus("ContextVault: " + error.message, "error");
    } finally {
      flushing = false;
    }
  }

  function observePage() {
    if (!captureAllowed()) return;
    var conversation = readPage();
    if (!conversation) return;
    batcher.observe(conversation);
    if (batcher.due()) flush(false);
  }

  async function captureNow() {
    if (!CVP.supportsCapture(platform)) {
      showStatus("ContextVault: capture is not supported on "
                 + platform.name + " yet", "warn");
      return;
    }
    var conversation = readPage();
    if (!conversation) {
      showStatus("ContextVault: nothing to capture — open a saved conversation",
                 "warn");
      return;
    }
    batcher.observe(conversation);
    await flush(true);
  }

  // ------------------------------------------------------------------
  // Wiring
  // ------------------------------------------------------------------

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && panel
        && panel.classList.contains("cv-open")) {
      hidePanel();
      return;
    }
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;

    var editor = findEditor(event.target);
    if (!editor) return;

    var parsed = CV.parseCommand(readEditor(editor));
    if (!parsed) return;

    // This keystroke is ours: "/context gym" is not a question anyone wants
    // an assistant to answer.
    event.preventDefault();
    event.stopPropagation();

    if (parsed.empty) {
      showStatus(CV.statusText("empty"), "warn");
      return;
    }
    runSearch(parsed.query);
  }, true);

  function start() {
    addLauncher();

    if (CVP.supportsCapture(platform)) {
      var observer = new MutationObserver(function () {
        clearTimeout(observer._timer);
        // The transcript mutates constantly while a reply streams in.
        // Debounce so a full read happens once the dust settles, not per
        // token.
        observer._timer = setTimeout(observePage, 900);
      });
      observer.observe(document.body, { childList: true, subtree: true });

      // The count rule is handled by the observer; this is the timer half.
      setInterval(function () {
        if (batcher.due()) flush(false);
      }, 5000);

      // ChatGPT is a single-page app: a new thread changes the URL without a
      // reload, and mixing two threads into one record would corrupt both.
      setInterval(function () {
        if (location.href !== lastUrl) {
          lastUrl = location.href;
          flush(false);
          batcher.reset();
        }
      }, 1000);

      // A tab closed mid-conversation should still save what it has.
      window.addEventListener("pagehide", function () { flush(false); });
    }
  }

  // The popup's "Capture this chat now" button: the page read has to happen
  // here, since only the content script can see the transcript.
  chrome.runtime.onMessage.addListener(function (message, _sender, respond) {
    if (!message) return false;
    if (message.type === "contextvault:capture-now") {
      captureNow().then(function () { respond({ ok: true }); });
      return true;
    }
    if (message.type === "contextvault:open-panel") {
      showPanel("");
      respond({ ok: true });
      return true;
    }
    return false;
  });

  loadSettings().then(start);

  chrome.storage.onChanged.addListener(function () { loadSettings(); });
})();
