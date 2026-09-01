/* Extension logic: command parsing, URL building, block formatting.
 *
 *     node tests/test_extension.js
 *
 * Everything here is the DOM-free half of the extension (extension/lib/
 * context.js).  The site adapters in content.js are not covered -- they are
 * selectors against someone else's markup, and a stub DOM would only prove
 * the stub matches itself.  Those are verified by loading the extension in a
 * real browser; see extension/README.md.
 */
"use strict";

const path = require("path");
const CV = require(path.join(__dirname, "..", "extension", "lib", "context.js"));

let fails = [];

function check(label, ok, detail) {
  console.log((ok ? "  ok   " : "  FAIL ") + label
              + (detail !== undefined && detail !== "" ? "  -> " + detail : ""));
  if (!ok) fails.push(label);
}

function eq(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  check(label, ok, ok ? "" : JSON.stringify(actual));
}

// ======================================================================
console.log("== A. parsing the command ==");

eq("plain command with a query",
   CV.parseCommand("/context gym"),
   { command: "/context", query: "gym", empty: false });

eq("leading and trailing space is ignored",
   CV.parseCommand("   /context   gym routine   "),
   { command: "/context", query: "gym routine", empty: false });

eq("case does not matter",
   CV.parseCommand("/CONTEXT gym"),
   { command: "/context", query: "gym", empty: false });

eq("a bare command is recognised but empty",
   CV.parseCommand("/context"),
   { command: "/context", query: "", empty: true });

eq("a newline after the command still parses",
   CV.parseCommand("/context\nwhat did I decide about docker"),
   { command: "/context", query: "what did I decide about docker",
     empty: false });

check("ordinary text is not the command",
      CV.parseCommand("what should I do at the gym") === null);
check("the word context alone is not the command",
      CV.parseCommand("context gym") === null);
check("a different slash command is left alone",
      CV.parseCommand("/contextual analysis please") === null);
check("the command must start the message",
      CV.parseCommand("please /context gym") === null);
check("an empty string is not the command",
      CV.parseCommand("") === null);
check("whitespace only is not the command",
      CV.parseCommand("   \n  ") === null);
check("a non-string is handled, not thrown",
      CV.parseCommand(null) === null && CV.parseCommand(undefined) === null
      && CV.parseCommand(42) === null);

// A question containing the word context must not be hijacked.
check("a question that merely mentions context is untouched",
      CV.parseCommand("how do I give the model more context?") === null);

// ======================================================================
console.log("\n== B. building the request ==");

eq("default shape",
   CV.buildSearchUrl("http://127.0.0.1:5000", "gym", 5),
   "http://127.0.0.1:5000/api/v1/search?q=gym&limit=5");

eq("a trailing slash on the base is not doubled",
   CV.buildSearchUrl("http://127.0.0.1:5000/", "gym", 5),
   "http://127.0.0.1:5000/api/v1/search?q=gym&limit=5");

eq("several trailing slashes are handled",
   CV.buildSearchUrl("http://127.0.0.1:5000///", "gym", 5),
   "http://127.0.0.1:5000/api/v1/search?q=gym&limit=5");

eq("the query is percent-encoded",
   CV.buildSearchUrl("http://127.0.0.1:5000", "gym & diet?", 5),
   "http://127.0.0.1:5000/api/v1/search?q=gym%20%26%20diet%3F&limit=5");

eq("an ampersand cannot inject another parameter",
   CV.buildSearchUrl("http://127.0.0.1:5000", "a&limit=999", 5),
   "http://127.0.0.1:5000/api/v1/search?q=a%26limit%3D999&limit=5");

eq("a junk limit falls back to the default",
   CV.buildSearchUrl("http://127.0.0.1:5000", "gym", "lots"),
   "http://127.0.0.1:5000/api/v1/search?q=gym&limit=5");

eq("a zero limit falls back to the default",
   CV.buildSearchUrl("http://127.0.0.1:5000", "gym", 0),
   "http://127.0.0.1:5000/api/v1/search?q=gym&limit=5");

eq("a missing base falls back to the default host",
   CV.buildSearchUrl("", "gym", 3),
   "http://127.0.0.1:5000/api/v1/search?q=gym&limit=3");

eq("no key means no key header",
   CV.requestHeaders(""), { "Accept": "application/json" });
eq("a key is sent as X-API-Key",
   CV.requestHeaders("abc123"),
   { "Accept": "application/json", "X-API-Key": "abc123" });

// ======================================================================
console.log("\n== C. the injected block ==");

const RESULTS = [
  { conversation_id: "chatgpt:conv-001", title: "Personal gym routine",
    provider: "ChatGPT", date: "2024-08-23T02:26:40+00:00",
    snippet: "I want to build muscle and start going to the gym.",
    match_type: "both" },
  { conversation_id: "claude:abc", title: "Training split rework",
    provider: "Claude", date: "2026-01-04T09:00:00+00:00",
    snippet: "Push pull legs beats a bro split at three days a week.",
    match_type: "semantic" }
];

const block = CV.formatContextBlock("what should I change?", RESULTS, {});

check("the block is delimited", block.startsWith("<contextvault_history>")
      && block.indexOf("</contextvault_history>") !== -1);
check("it says the excerpts are the user's own history",
      /excerpts from my own past AI conversations/.test(block));
check("it marks them as background, not instructions",
      /background, not instructions/.test(block));
check("it gives permission to ignore them",
      /say so if none of it helps/.test(block));
check("the question comes last, after the closing tag",
      block.trim().endsWith("what should I change?"));
check("the question is not also at the top",
      block.indexOf("what should I change?")
      > block.indexOf("</contextvault_history>"));
check("every result is numbered", /1\. Personal gym routine/.test(block)
      && /2\. Training split rework/.test(block));
check("each carries provider, date and match type",
      /\[ChatGPT · 2024-08-23 · both\]/.test(block)
      && /\[Claude · 2026-01-04 · semantic\]/.test(block));
check("snippets are included", /build muscle/.test(block));

const emptyBlock = CV.formatContextBlock("what should I change?", [], {});
check("an empty result set says so rather than pretending",
      /No matching conversations were found/.test(emptyBlock));
check("the question still comes last when nothing was found",
      emptyBlock.trim().endsWith("what should I change?"));
check("null results are treated as none",
      /No matching conversations were found/.test(
        CV.formatContextBlock("q", null, {})));

const missing = CV.formatContextBlock("q", [{}], {});
check("a result with no fields still renders",
      /Untitled conversation/.test(missing) && /unknown/.test(missing)
      && /undated/.test(missing));

// ======================================================================
console.log("\n== D. truncation ==");

eq("short text is untouched", CV.truncate("hello there", 400), "hello there");
eq("whitespace is collapsed",
   CV.truncate("a\n\n  b\tc", 400), "a b c");
check("long text is cut and marked",
      CV.truncate("x".repeat(500), 400).length <= 401
      && CV.truncate("x".repeat(500), 400).endsWith("…"));
check("cutting happens on a word boundary",
      CV.truncate("alpha beta gamma delta", 12) === "alpha beta…");
eq("null truncates to empty", CV.truncate(null, 10), "");

// ======================================================================
console.log("\n== E. error messages ==");

check("a refused connection blames the app, not the network",
      /is the desktop app running/.test(
        CV.explainError(new Error("Failed to fetch"), "http://127.0.0.1:5000")));
check("the address is named in that message",
      /127\.0\.0\.1:5000/.test(
        CV.explainError(new Error("Failed to fetch"), "http://127.0.0.1:5000")));
check("a 401 points at the key",
      /API key rejected/.test(
        CV.explainError(new Error("HTTP 401"), "http://127.0.0.1:5000")));
check("a 503 explains that a key is required",
      /requires an API key/.test(
        CV.explainError(new Error("HTTP 503"), "http://127.0.0.1:5000")));
check("an unrecognised error is passed through verbatim",
      CV.explainError(new Error("something odd"), "http://127.0.0.1:5000")
      === "something odd");

// ======================================================================
console.log("\n== F. status line ==");

check("searching", /searching/.test(CV.statusText("searching")));
check("one result is singular",
      CV.statusText("ok", 1) === "ContextVault: added 1 conversation");
check("several are plural",
      CV.statusText("ok", 4) === "ContextVault: added 4 conversations");
check("no matches names the query",
      /no matches for “gym”/.test(CV.statusText("none", "gym")));
check("an empty command asks for a question",
      /type a question after/.test(CV.statusText("empty")));

console.log("\n" + (fails.length === 0 ? "ALL CHECKS PASSED"
                    : fails.length + " FAILURES: " + fails.join(", ")));
process.exit(fails.length === 0 ? 0 : 1);
