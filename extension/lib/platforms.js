/* Which site are we on, where is its composer, and can we read its messages.
 *
 * Everything here is data plus pure functions over a DOM that is passed in,
 * so tests/test_capture.js can drive it against a parsed document instead of
 * a browser.
 *
 * Two capabilities are tracked separately, because they are not the same
 * problem:
 *
 *   composer  where the user types.  Needed for /context.  Cheap to support:
 *             one selector, and a fallback to whatever has focus.
 *   capture   how to read the whole transcript back out.  Needed for
 *             auto-capture.  Expensive to support: every site marks up its
 *             turns differently, and getting it wrong means recording
 *             half a conversation or the wrong speaker.
 *
 * ChatGPT is the only site with a capture adapter today.  The rest get
 * /context now and capture when each has been checked against the real DOM.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.CVPlatforms = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function text(node) {
    if (!node) return "";
    return String(node.innerText || node.textContent || "")
      .replace(/ /g, " ")
      .replace(/[ \t]+\n/g, "\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  var PLATFORMS = [
    {
      id: "chatgpt",
      name: "ChatGPT",
      match: /^(www\.)?(chatgpt\.com|chat\.openai\.com)$/,
      composer: ["#prompt-textarea", "div[contenteditable='true']",
                 "textarea[data-id]", "textarea"],
      /* ChatGPT tags every turn with the author role and a stable id. That
       * pair is the most reliable hook the page offers: it survives the
       * styling changes that break class-name selectors. */
      capture: {
        turnSelector: "[data-message-author-role]",
        /* Read one turn. Returns null for anything that is not a real
         * message, so an empty streaming placeholder is skipped rather than
         * recorded as a blank turn. */
        readTurn: function (node) {
          var role = node.getAttribute("data-message-author-role");
          if (role !== "user" && role !== "assistant") return null;
          var body = node.querySelector(".markdown, .whitespace-pre-wrap")
                     || node;
          var content = text(body);
          if (!content) return null;
          return {
            id: node.getAttribute("data-message-id") || null,
            role: role,
            content: content
          };
        },
        /* The conversation id in the URL is what the official export uses
         * too, so a captured thread and the same thread imported later land
         * on one record instead of two. */
        conversationId: function (url) {
          var match = /\/c\/([0-9a-zA-Z-]+)/.exec(url || "");
          return match ? match[1] : null;
        },
        title: function (doc) {
          var heading = doc.querySelector("nav a[data-active], nav li[data-active] a");
          var fromNav = text(heading);
          if (fromNav) return fromNav;
          var title = (doc.title || "").replace(/\s*[|·-]\s*ChatGPT\s*$/i, "");
          return title.trim();
        }
      }
    },
    {
      id: "claude",
      name: "Claude",
      match: /^(www\.)?claude\.ai$/,
      composer: ["div.ProseMirror[contenteditable='true']",
                 "div[contenteditable='true']", "textarea"],
      capture: null
    },
    {
      id: "gemini",
      name: "Gemini",
      match: /^gemini\.google\.com$/,
      composer: ["div.ql-editor[contenteditable='true']",
                 "rich-textarea div[contenteditable='true']",
                 "div[contenteditable='true']", "textarea"],
      capture: null
    },
    {
      id: "deepseek",
      name: "DeepSeek",
      match: /^chat\.deepseek\.com$/,
      composer: ["textarea#chat-input", "div[contenteditable='true']",
                 "textarea"],
      capture: null
    },
    {
      id: "perplexity",
      name: "Perplexity",
      match: /^(www\.)?perplexity\.ai$/,
      composer: ["textarea[placeholder]", "div[contenteditable='true']",
                 "textarea"],
      capture: null
    }
  ];

  function forHostname(hostname) {
    for (var i = 0; i < PLATFORMS.length; i++) {
      if (PLATFORMS[i].match.test(String(hostname || ""))) return PLATFORMS[i];
    }
    return null;
  }

  function supportsCapture(platform) {
    return !!(platform && platform.capture);
  }

  /* Read the whole visible transcript.
   *
   * Returns null rather than a partial record when there is no conversation
   * id: an unsaved or brand-new thread has no stable identity yet, and
   * inventing one would file the same conversation twice once the real id
   * appears in the URL.
   */
  function readConversation(platform, doc, url) {
    if (!supportsCapture(platform)) return null;
    var spec = platform.capture;

    var id = spec.conversationId(url);
    if (!id) return null;

    var nodes = doc.querySelectorAll(spec.turnSelector);
    var messages = [];
    for (var i = 0; i < nodes.length; i++) {
      var turn = spec.readTurn(nodes[i]);
      if (turn) messages.push(turn);
    }
    if (!messages.length) return null;

    return {
      platform: platform.id,
      conversation_id: platform.id + ":" + id,
      title: spec.title(doc) || "Untitled conversation",
      messages: messages
    };
  }

  return {
    PLATFORMS: PLATFORMS,
    forHostname: forHostname,
    supportsCapture: supportsCapture,
    readConversation: readConversation,
    text: text
  };
});
