/* ContextVault PWA.
 *
 * Vanilla, no build step, no framework. It is served by the same Flask app
 * that serves the Bridge API, so every call here is same-origin -- which is
 * why there is no base URL to configure and no CORS to negotiate. The API
 * deliberately does not send permissive CORS headers, and being same-origin
 * means it never has to.
 *
 * The context block is built by extension/lib/context.js, served to this page
 * at /pwa/lib/context.js. Sharing that one file is what keeps the phone, the
 * extension and the server-rendered /context page producing identical blocks.
 */
(function () {
  "use strict";

  var CV = window.CVContext;
  var API = "/api/v1";
  var STORE = "contextvault.settings";
  var LAST = "contextvault.lastSearch";

  var state = {
    apiKey: "",
    limit: 10,
    view: "search",
    query: "",
    results: [],
    conversation: null
  };

  // ------------------------------------------------------------------
  // Small helpers
  // ------------------------------------------------------------------

  function $(id) { return document.getElementById(id); }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function setStatus(id, text, tone) {
    var node = $(id);
    node.textContent = text || "";
    if (tone) node.setAttribute("data-tone", tone);
    else node.removeAttribute("data-tone");
  }

  var toastTimer = null;
  function toast(text, tone) {
    var node = $("toast");
    node.textContent = text;
    if (tone) node.setAttribute("data-tone", tone); else node.removeAttribute("data-tone");
    node.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { node.classList.remove("show"); }, 2600);
  }

  function formatDate(value) {
    if (!value) return "undated";
    var d = new Date(value);
    if (isNaN(d.getTime())) return String(value).slice(0, 10);
    return d.toLocaleDateString(undefined,
      { year: "numeric", month: "short", day: "numeric" });
  }

  function loadSettings() {
    try {
      var stored = JSON.parse(localStorage.getItem(STORE) || "{}");
      state.apiKey = stored.apiKey || "";
      state.limit = parseInt(stored.limit, 10) || 10;
    } catch (error) { /* first run, or storage blocked */ }
  }

  function saveSettings() {
    try {
      localStorage.setItem(STORE, JSON.stringify({
        apiKey: state.apiKey, limit: state.limit
      }));
    } catch (error) {
      toast("Could not save settings on this device.", "bad");
    }
  }

  // ------------------------------------------------------------------
  // API
  // ------------------------------------------------------------------

  function headers(extra) {
    var h = Object.assign({ "Accept": "application/json" }, extra || {});
    if (state.apiKey) h["X-API-Key"] = state.apiKey;
    return h;
  }

  async function api(path, options) {
    var opts = Object.assign({ credentials: "same-origin" }, options || {});
    opts.headers = headers(opts.headers);

    var response;
    try {
      response = await fetch(API + path, opts);
    } catch (error) {
      // Offline, or the desktop app stopped. The service worker may still
      // have served a cached copy, so only a genuine miss lands here.
      throw new Error(navigator.onLine
        ? "Cannot reach ContextVault. Is the app still running?"
        : "You are offline and this was not cached.");
    }

    var payload = null;
    try { payload = await response.json(); } catch (error) { /* no body */ }

    if (!response.ok) {
      var detail = payload && payload.error ? payload.error
                                            : "HTTP " + response.status;
      if (response.status === 401) detail = "API key rejected. Check Settings.";
      if (response.status === 503) detail = "This ContextVault needs an API key.";
      throw new Error(detail);
    }
    return payload;
  }

  // ------------------------------------------------------------------
  // Views
  // ------------------------------------------------------------------

  var VIEWS = ["search", "conversation", "memories", "settings"];

  function show(view, title) {
    state.view = view;
    VIEWS.forEach(function (name) {
      $("view-" + name).hidden = (name !== view);
    });
    $("title").textContent = title || "ContextVault";
    $("back").hidden = (view === "search" || view === "memories");

    document.querySelectorAll(".tab").forEach(function (tab) {
      tab.classList.toggle("active", tab.dataset.view === view);
    });
    window.scrollTo(0, 0);
  }

  // ------------------------------------------------------------------
  // Search
  // ------------------------------------------------------------------

  function renderResults(results, note) {
    var list = $("results");
    list.textContent = "";

    if (!results.length) {
      list.appendChild(el("p", "empty",
        "Nothing matched. Try fewer words, or a different way of saying it."));
      return;
    }

    results.forEach(function (result) {
      var card = el("button", "card");
      card.type = "button";
      card.appendChild(el("h3", null, result.title || "Untitled conversation"));

      var badges = el("div", "badges");
      badges.appendChild(el("span", "badge " + (result.match_type || ""),
                            result.match_type || "match"));
      badges.appendChild(el("span", "meta",
        (result.provider || "unknown") + " · " + formatDate(result.date)));
      card.appendChild(badges);

      if (result.snippet) card.appendChild(el("p", null, result.snippet));

      card.addEventListener("click", function () {
        openConversation(result.conversation_id);
      });
      list.appendChild(card);
    });

    if (note) setStatus("search-status", note, "ok");
  }

  async function runSearch(query) {
    query = (query || "").trim();
    if (!query) return;
    state.query = query;
    $("q").value = query;

    setStatus("search-status", "Searching…");
    $("search-button").disabled = true;

    try {
      var data = await api("/search?q=" + encodeURIComponent(query)
                           + "&limit=" + state.limit);
      state.results = data.results || [];
      renderResults(state.results);
      setStatus("search-status",
        state.results.length + " result"
        + (state.results.length === 1 ? "" : "s") + " for “" + query + "”");

      // Keep the last search so a cold, offline launch still shows something.
      try {
        localStorage.setItem(LAST, JSON.stringify(
          { query: query, results: state.results, at: Date.now() }));
      } catch (error) { /* storage full or blocked; not fatal */ }
    } catch (error) {
      setStatus("search-status", error.message, "bad");
      showCachedSearch(query);
    } finally {
      $("search-button").disabled = false;
    }
  }

  /* Offline fallback: if this is the query we cached, show it and say so. */
  function showCachedSearch(query) {
    var cached = readLastSearch();
    if (!cached) return;
    if (query && cached.query !== query) return;
    state.results = cached.results || [];
    renderResults(state.results);
    setStatus("search-status",
      "Showing the last cached results for “" + cached.query + "”.", "bad");
  }

  function readLastSearch() {
    try {
      var raw = localStorage.getItem(LAST);
      return raw ? JSON.parse(raw) : null;
    } catch (error) { return null; }
  }

  // ------------------------------------------------------------------
  // Conversation
  // ------------------------------------------------------------------

  async function openConversation(id) {
    show("conversation", "Loading…");
    $("convo-title").textContent = "…";
    $("convo-meta").textContent = "";
    $("messages").textContent = "";

    try {
      var convo = await api("/conversations/" + encodeURIComponent(id));
      state.conversation = convo;
      $("title").textContent = convo.title || "Conversation";
      $("convo-title").textContent = convo.title || "Untitled conversation";
      $("convo-meta").textContent = (convo.provider || "unknown") + " · "
        + formatDate(convo.created_at) + " · "
        + convo.message_count + " message"
        + (convo.message_count === 1 ? "" : "s");

      var wrap = $("messages");
      (convo.messages || []).forEach(function (message) {
        var node = el("div", "message " + (message.role || ""));
        node.appendChild(el("span", "role", message.role || "?"));
        node.appendChild(document.createTextNode(message.content || ""));
        wrap.appendChild(node);
      });
    } catch (error) {
      state.conversation = null;
      $("convo-title").textContent = "Could not open it";
      $("convo-meta").textContent = error.message;
    }
  }

  /* Build the same block the extension pastes, and put it on the clipboard. */
  async function copyContext() {
    var convo = state.conversation;
    if (!convo) return;

    // One conversation, rendered through the shared formatter so what the
    // phone produces is what the extension would have produced.
    var block = CV.formatContextBlock(state.query || convo.title, [{
      title: convo.title,
      provider: convo.provider,
      date: convo.created_at,
      match_type: "opened",
      snippet: (convo.messages || []).map(function (m) {
        return m.role + ": " + m.content;
      }).join("\n")
    }], { snippetLength: 1800 });

    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(block);
        toast("Context copied. Paste it into any chat.", "ok");
        return;
      }
      throw new Error("no clipboard api");
    } catch (error) {
      // iOS outside a secure context, or permission refused. Give the user
      // something selectable rather than a dead button.
      var area = el("textarea");
      area.value = block;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.focus();
      area.select();
      var ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      document.body.removeChild(area);
      toast(ok ? "Context copied." : "Could not copy automatically.",
            ok ? "ok" : "bad");
    }
  }

  async function saveMemoryFromConversation() {
    var convo = state.conversation;
    if (!convo) return;
    var text = prompt("What is worth remembering from this conversation?",
                      convo.title || "");
    if (text === null) return;
    text = text.trim();
    if (!text) return;

    try {
      await api("/memories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: text, source: "pwa", conversation_id: convo.conversation_id
        })
      });
      toast("Memory saved.", "ok");
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  // ------------------------------------------------------------------
  // Memories
  // ------------------------------------------------------------------

  function renderMemories(memories) {
    var list = $("memories");
    list.textContent = "";

    if (!memories.length) {
      list.appendChild(el("p", "empty",
        "No memories yet. Save one from a conversation, or write one above."));
      return;
    }

    memories.forEach(function (memory) {
      var card = el("div", "card memory");
      card.appendChild(el("p", null, memory.content));

      var badges = el("div", "badges tags");
      if (memory.source) badges.appendChild(el("span", "badge", memory.source));
      (memory.tags || []).forEach(function (tag) {
        badges.appendChild(el("span", "badge", tag));
      });
      badges.appendChild(el("span", "meta", formatDate(memory.created_at)));
      card.appendChild(badges);

      var remove = el("button", "memory-delete", "Delete");
      remove.type = "button";
      remove.addEventListener("click", function () {
        deleteMemory(memory.id);
      });
      card.appendChild(remove);
      list.appendChild(card);
    });
  }

  async function loadMemories() {
    setStatus("memories-status", "Loading…");
    try {
      var data = await api("/memories?limit=100");
      renderMemories(data.memories || []);
      setStatus("memories-status",
        data.count + " memor" + (data.count === 1 ? "y" : "ies"));
    } catch (error) {
      setStatus("memories-status", error.message, "bad");
    }
  }

  async function deleteMemory(id) {
    if (!confirm("Delete this memory?")) return;
    try {
      await api("/memories/" + id, { method: "DELETE" });
      toast("Deleted.", "ok");
      loadMemories();
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  async function createMemory(event) {
    event.preventDefault();
    var text = $("memory-text").value.trim();
    if (!text) return;

    var tags = $("memory-tags").value.split(",")
      .map(function (t) { return t.trim(); })
      .filter(Boolean);

    try {
      await api("/memories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: text, source: "pwa", tags: tags })
      });
      $("memory-text").value = "";
      $("memory-tags").value = "";
      toast("Memory saved.", "ok");
      loadMemories();
    } catch (error) {
      toast(error.message, "bad");
    }
  }

  // ------------------------------------------------------------------
  // Settings
  // ------------------------------------------------------------------

  async function checkHealth() {
    setStatus("settings-status", "Checking…");
    try {
      var health = await api("/health");
      var needsKey = health.auth_required && !state.apiKey;
      setStatus("settings-status",
        needsKey
          ? "Reached it, but auth is on and no key is set here."
          : "Connected. " + health.conversations + " conversations, "
            + health.memories + " memories.",
        needsKey ? "bad" : "ok");
      $("version").textContent = "Bridge API v" + health.api_version
        + " · " + health.database;
    } catch (error) {
      setStatus("settings-status", error.message, "bad");
    }
  }

  // ------------------------------------------------------------------
  // Wiring
  // ------------------------------------------------------------------

  function updateOnlineBar() {
    $("offline-bar").hidden = navigator.onLine;
  }

  function init() {
    loadSettings();
    $("api-key").value = state.apiKey;
    $("limit").value = state.limit;

    $("search-form").addEventListener("submit", function (event) {
      event.preventDefault();
      $("q").blur();   // dismiss the phone keyboard
      runSearch($("q").value);
    });

    document.querySelectorAll(".tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        var view = tab.dataset.view;
        show(view, view === "memories" ? "Memories" : "ContextVault");
        if (view === "memories") loadMemories();
      });
    });

    $("back").addEventListener("click", function () {
      show("search", "ContextVault");
      renderResults(state.results);
    });

    $("open-settings").addEventListener("click", function () {
      show("settings", "Settings");
      checkHealth();
    });

    $("copy-context").addEventListener("click", copyContext);
    $("save-memory").addEventListener("click", saveMemoryFromConversation);
    $("memory-form").addEventListener("submit", createMemory);
    $("check-health").addEventListener("click", checkHealth);

    $("save-settings").addEventListener("click", function () {
      state.apiKey = $("api-key").value.trim();
      state.limit = Math.max(1, Math.min(20, parseInt($("limit").value, 10) || 10));
      $("limit").value = state.limit;
      saveSettings();
      toast("Saved.", "ok");
    });

    window.addEventListener("online", updateOnlineBar);
    window.addEventListener("offline", updateOnlineBar);
    updateOnlineBar();

    // A launch with no connection should still show the last thing you looked
    // at, rather than an empty box.
    var cached = readLastSearch();
    if (cached && cached.query) {
      $("q").value = cached.query;
      state.query = cached.query;
      state.results = cached.results || [];
      renderResults(state.results);
      setStatus("search-status", "Last search: “" + cached.query + "”");
    }

    // A query in the URL makes the app shareable and lets the server-rendered
    // /context page hand off to it.
    var params = new URLSearchParams(location.search);
    if (params.get("q")) runSearch(params.get("q"));

    if ("serviceWorker" in navigator) {
      // Registered at the root so its scope covers /api/v1 as well as /pwa.
      navigator.serviceWorker.register("/sw.js").catch(function (error) {
        console.warn("ContextVault: service worker not registered", error);
      });
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
