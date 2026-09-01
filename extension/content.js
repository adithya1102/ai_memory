/* Content script: watch the composer for "/context <query>".
 *
 * When the user presses Enter on a line that starts with /context, this
 * cancels the send, searches the local archive, and rewrites the composer to
 * the retrieved history followed by the original question.
 *
 * It does not send the message. Injecting context means uploading excerpts of
 * a private local archive to whichever provider owns this tab, so the user
 * sees exactly what is about to leave the machine and presses Enter -- or
 * edits it, or deletes it -- themselves. "Send automatically" is available in
 * the options for anyone who wants it, and is off by default.
 */
(function () {
  "use strict";

  var CV = window.CVContext;
  if (!CV) return;

  /* Site adapters.
   *
   * These selectors belong to someone else's app and will break when it is
   * redesigned; that is why each site lists several and why there is a
   * generic fallback. A miss here degrades to "the command does nothing",
   * never to a broken composer.
   */
  var SITES = [
    {
      name: "chatgpt",
      match: /(^|\.)chatgpt\.com$|(^|\.)chat\.openai\.com$/,
      selectors: ["#prompt-textarea", "div[contenteditable='true']",
                  "textarea[data-id]", "textarea"]
    },
    {
      name: "claude",
      match: /(^|\.)claude\.ai$/,
      selectors: ["div.ProseMirror[contenteditable='true']",
                  "div[contenteditable='true']", "textarea"]
    },
    {
      name: "gemini",
      match: /(^|\.)gemini\.google\.com$/,
      selectors: ["div.ql-editor[contenteditable='true']",
                  "rich-textarea div[contenteditable='true']",
                  "div[contenteditable='true']", "textarea"]
    }
  ];

  var site = SITES.filter(function (s) {
    return s.match.test(location.hostname);
  })[0];
  if (!site) return;

  // ------------------------------------------------------------------
  // Reading and writing the composer
  // ------------------------------------------------------------------

  function isEditor(node) {
    if (!node || node.nodeType !== 1) return false;
    if (node.tagName === "TEXTAREA") return true;
    return node.getAttribute && node.getAttribute("contenteditable") === "true";
  }

  /* Prefer whatever the user is actually typing in; fall back to the site's
   * known selectors. Focus is the more reliable signal of the two. */
  function findEditor(target) {
    if (isEditor(target)) return target;
    var active = document.activeElement;
    if (isEditor(active)) return active;
    for (var i = 0; i < site.selectors.length; i++) {
      var found = document.querySelector(site.selectors[i]);
      if (isEditor(found)) return found;
    }
    return null;
  }

  function readEditor(editor) {
    if (!editor) return "";
    return editor.tagName === "TEXTAREA" ? editor.value : editor.innerText;
  }

  /* Replace the composer's contents.
   *
   * These editors are React/ProseMirror/Quill controlled inputs: assigning to
   * .value or .innerText updates the DOM but leaves the framework's own state
   * stale, and the send handler reads the framework's state. Going through the
   * native setter plus a bubbling input event, or execCommand for
   * contenteditable, is what makes the change one the app actually sees.
   */
  function writeEditor(editor, text) {
    editor.focus();

    if (editor.tagName === "TEXTAREA") {
      var setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, "value").set;
      setter.call(editor, text);
      editor.dispatchEvent(new Event("input", { bubbles: true }));
      return true;
    }

    var selection = window.getSelection();
    var range = document.createRange();
    range.selectNodeContents(editor);
    selection.removeAllRanges();
    selection.addRange(range);

    var inserted = false;
    try {
      inserted = document.execCommand("insertText", false, text);
    } catch (error) {
      inserted = false;
    }
    if (!inserted) {
      // Last resort. Some editors will not notice this, which is why it is
      // not the first choice.
      editor.textContent = text;
      editor.dispatchEvent(new InputEvent("input", {
        bubbles: true, inputType: "insertText", data: text
      }));
    }
    return true;
  }

  function submit(editor) {
    editor.dispatchEvent(new KeyboardEvent("keydown", {
      key: "Enter", code: "Enter", keyCode: 13, which: 13,
      bubbles: true, cancelable: true
    }));
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
      }, 6000);
    }
  }

  // ------------------------------------------------------------------
  // The command
  // ------------------------------------------------------------------

  var running = false;

  function ask(query, limit) {
    return new Promise(function (resolve, reject) {
      chrome.runtime.sendMessage(
        { type: "contextvault:search", query: query, limit: limit },
        function (response) {
          if (chrome.runtime.lastError) {
            reject(new Error(chrome.runtime.lastError.message));
          } else if (!response || !response.ok) {
            reject(new Error(response ? response.error : "no response"));
          } else {
            resolve(response.data);
          }
        }
      );
    });
  }

  function settings() {
    return new Promise(function (resolve) {
      chrome.storage.sync.get(CV.DEFAULTS, function (stored) {
        resolve(Object.assign({}, CV.DEFAULTS, stored || {}));
      });
    });
  }

  async function run(editor, parsed) {
    running = true;
    try {
      showStatus(CV.statusText("searching"), "busy");
      var config = await settings();
      var data = await ask(parsed.query, config.limit);
      var results = (data && data.results) || [];

      var block = CV.formatContextBlock(parsed.query, results,
                                        { snippetLength: 400 });
      writeEditor(editor, block);

      if (!results.length) {
        showStatus(CV.statusText("none", parsed.query), "warn");
      } else {
        showStatus(CV.statusText("ok", results.length), "ok");
        if (config.autoSend) setTimeout(function () { submit(editor); }, 60);
      }
    } catch (error) {
      showStatus(CV.statusText("error", error.message || String(error)),
                 "error");
    } finally {
      running = false;
    }
  }

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    if (running) return;

    var editor = findEditor(event.target);
    if (!editor) return;

    var parsed = CV.parseCommand(readEditor(editor));
    if (!parsed) return;

    // From here on this keystroke is ours: the message must not be sent as
    // typed, because "/context gym" is not a question anyone wants answered.
    event.preventDefault();
    event.stopPropagation();

    if (parsed.empty) {
      showStatus(CV.statusText("empty"), "warn");
      return;
    }
    run(editor, parsed);
  }, true);  // capture: beat the site's own Enter handler to it
})();
