/* Auto-capture: platform detection, transcript extraction, batching.
 *
 *     node tests/test_capture.js
 *
 * The DOM here is a hand-rolled stub, not jsdom -- this project carries no npm
 * dependencies and is not about to start for a test.  That bounds what these
 * checks prove: they cover the extraction *logic* (which roles count, what is
 * skipped, ordering, how the conversation id is derived) and all of the
 * batching.  They do not prove the selectors match ChatGPT's real markup,
 * which only a browser on the live site can show.
 */
"use strict";

const path = require("path");
const CVP = require(path.join(__dirname, "..", "extension", "lib", "platforms.js"));
const CVC = require(path.join(__dirname, "..", "extension", "lib", "capture.js"));

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

// ------------------------------------------------------------------
// A DOM stub: only what platforms.js actually touches.
// ------------------------------------------------------------------

function node(attrs, text, children) {
  return {
    attrs: attrs || {},
    innerText: text || "",
    children: children || [],
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attrs, name)
        ? this.attrs[name] : null;
    },
    querySelector(selector) {
      // The adapter asks for ".markdown, .whitespace-pre-wrap"; a turn in
      // these fixtures carries its text directly, so report no inner body and
      // let the adapter fall back to the node itself.
      const wanted = selector.split(",").map(s => s.trim());
      for (const child of this.children) {
        if (wanted.includes("." + (child.attrs.class || ""))) return child;
      }
      return null;
    }
  };
}

function doc(turns, title) {
  return {
    title: title || "",
    querySelectorAll(selector) {
      if (selector === "[data-message-author-role]") {
        return turns.filter(t => t.getAttribute("data-message-author-role") !== null);
      }
      return [];
    },
    querySelector() { return null; }
  };
}

function turn(role, text, id) {
  return node({ "data-message-author-role": role, "data-message-id": id || null },
              text);
}

// ======================================================================
console.log("== A. which platform are we on ==");

const CASES = [
  ["chatgpt.com", "chatgpt"],
  ["www.chatgpt.com", "chatgpt"],
  ["chat.openai.com", "chatgpt"],
  ["claude.ai", "claude"],
  ["gemini.google.com", "gemini"],
  ["chat.deepseek.com", "deepseek"],
  ["perplexity.ai", "perplexity"],
  ["www.perplexity.ai", "perplexity"]
];

CASES.forEach(([host, id]) => {
  const platform = CVP.forHostname(host);
  check(host + " is " + id, platform && platform.id === id,
        platform ? platform.id : "null");
});

check("an unrelated site matches nothing",
      CVP.forHostname("example.com") === null);
check("a lookalike domain does not match",
      CVP.forHostname("notchatgpt.com") === null,
      (CVP.forHostname("notchatgpt.com") || {}).id);
check("an empty hostname is handled", CVP.forHostname("") === null);
check("undefined is handled", CVP.forHostname(undefined) === null);

check("every platform declares composer selectors",
      CVP.PLATFORMS.every(p => Array.isArray(p.composer) && p.composer.length));

// Capture is ChatGPT-only on purpose, and the tests should fail loudly if a
// platform is switched on without an adapter being written for it.
check("ChatGPT supports capture",
      CVP.supportsCapture(CVP.forHostname("chatgpt.com")));
["claude", "gemini", "deepseek", "perplexity"].forEach(id => {
  const platform = CVP.PLATFORMS.find(p => p.id === id);
  check(id + " does not claim capture support yet",
        !CVP.supportsCapture(platform));
});

// ======================================================================
console.log("\n== B. reading a ChatGPT transcript ==");

const chatgpt = CVP.forHostname("chatgpt.com");
const URL_OK = "https://chatgpt.com/c/abc123-def";

let read = CVP.readConversation(chatgpt, doc([
  turn("user", "how do I get stronger", "m1"),
  turn("assistant", "Start with a full body split.", "m2")
], "Gym plan | ChatGPT"), URL_OK);

eq("both turns are read, in order",
   read.messages.map(m => m.role + ":" + m.content),
   ["user:how do I get stronger", "assistant:Start with a full body split."]);
check("the id is namespaced by platform",
      read.conversation_id === "chatgpt:abc123-def", read.conversation_id);
check("the platform is recorded", read.platform === "chatgpt");
check("the title is cleaned of the site suffix",
      read.title === "Gym plan", read.title);

read = CVP.readConversation(chatgpt, doc([
  turn("user", "a", "m1"),
  turn("system", "you are a helpful assistant", "m2"),
  turn("tool", "{}", "m3"),
  turn("assistant", "b", "m4")
]), URL_OK);
eq("only user and assistant turns count",
   read.messages.map(m => m.role), ["user", "assistant"]);

read = CVP.readConversation(chatgpt, doc([
  turn("user", "a real question", "m1"),
  turn("assistant", "", "m2"),
  turn("assistant", "   \n  ", "m3")
]), URL_OK);
eq("empty turns are skipped, not stored blank",
   read.messages.map(m => m.role), ["user"]);

check("a page with no conversation id is refused",
      CVP.readConversation(chatgpt, doc([turn("user", "hi", "m1")]),
                           "https://chatgpt.com/") === null);
check("a page with no messages is refused",
      CVP.readConversation(chatgpt, doc([]), URL_OK) === null);
check("a platform without an adapter returns nothing",
      CVP.readConversation(CVP.forHostname("claude.ai"),
                           doc([turn("user", "hi", "m1")]),
                           "https://claude.ai/chat/x") === null);

check("non-breaking spaces are normalised",
      CVP.text({ innerText: "a b" }) === "a b",
      JSON.stringify(CVP.text({ innerText: "a b" })));
check("runs of blank lines collapse",
      CVP.text({ innerText: "a\n\n\n\nb" }) === "a\n\nb");

// ======================================================================
console.log("\n== C. batching ==");

let clock = 1000;
const now = () => clock;

let batch = new CVC.Batcher({ now: now });

check("the default batch size is five", CVC.BATCH_SIZE === 5);
check("the default interval is thirty seconds", CVC.BATCH_MS === 30000);

const convo = (messages) => ({
  platform: "chatgpt",
  conversation_id: "chatgpt:abc",
  title: "T",
  messages: messages
});

check("nothing pending at the start", batch.pending() === 0);
check("and nothing is due", batch.due() === false);

batch.observe(convo([{ id: "m1", role: "user", content: "one" }]));
check("one message is pending", batch.pending() === 1, batch.pending());
check("one message is not yet due", batch.due() === false);

// The page is re-read constantly while a reply streams; re-reading the same
// turns must not queue them again.
const added = batch.observe(convo([{ id: "m1", role: "user", content: "one" }]));
check("re-reading the same page adds nothing", added === 0, added);
check("and pending is unchanged", batch.pending() === 1, batch.pending());

batch.observe(convo([
  { id: "m1", role: "user", content: "one" },
  { id: "m2", role: "assistant", content: "two" },
  { id: "m3", role: "user", content: "three" },
  { id: "m4", role: "assistant", content: "four" }
]));
check("four pending is still under the threshold", batch.due() === false,
      batch.pending());

batch.observe(convo([
  { id: "m1", role: "user", content: "one" },
  { id: "m2", role: "assistant", content: "two" },
  { id: "m3", role: "user", content: "three" },
  { id: "m4", role: "assistant", content: "four" },
  { id: "m5", role: "user", content: "five" }
]));
check("the fifth message makes it due", batch.due() === true, batch.pending());

const payload = batch.payload();
check("the payload is the ingest shape",
      payload.source === "extension" && payload.conversations.length === 1,
      JSON.stringify(payload).slice(0, 80));
check("it carries the whole transcript, not just the new turns",
      payload.conversations[0].messages.length === 5);
check("it carries the conversation id",
      payload.conversations[0].conversation_id === "chatgpt:abc");
check("it carries the provider",
      payload.conversations[0].provider === "chatgpt");
eq("messages are role/content only",
   Object.keys(payload.conversations[0].messages[0]).sort(),
   ["content", "role"]);

batch.markSent(5);
check("nothing is pending after a send", batch.pending() === 0);
check("and nothing is due", batch.due() === false);

// ======================================================================
console.log("\n== D. the thirty second rule ==");

clock = 1000;
batch = new CVC.Batcher({ now: now });
batch.observe(convo([{ id: "m1", role: "user", content: "one" }]));
check("a lone message is not due immediately", batch.due() === false);

clock = 1000 + 29000;
check("still not due at 29 seconds", batch.due() === false);

clock = 1000 + 30000;
check("due at exactly 30 seconds", batch.due() === true);

clock = 1000 + 90000;
check("still due later", batch.due() === true);

batch.markSent(1);
check("sending clears it", batch.due() === false);

// Something arriving while a send was in flight must not be lost.
clock = 200000;
batch.observe(convo([
  { id: "m1", role: "user", content: "one" },
  { id: "m2", role: "assistant", content: "two" }
]));
batch.markSent(1);
check("a message that arrived mid-send stays pending",
      batch.pending() === 1, batch.pending());

// ======================================================================
console.log("\n== E. switching conversations ==");

clock = 1000;
batch = new CVC.Batcher({ now: now });
batch.observe(convo([{ id: "m1", role: "user", content: "one" }]));
batch.observe({
  platform: "chatgpt", conversation_id: "chatgpt:other", title: "Other",
  messages: [{ id: "n1", role: "user", content: "different thread" }]
});

check("the new thread replaces the old one",
      batch.conversationId === "chatgpt:other", batch.conversationId);
check("the old thread's messages are gone, not merged",
      batch.messages.length === 1, batch.messages.length);
eq("only the new thread is in the payload",
   batch.payload().conversations[0].messages.map(m => m.content),
   ["different thread"]);

batch.reset();
check("reset clears the id", batch.conversationId === null);
check("reset clears the queue", batch.pending() === 0);
check("reset means no payload", batch.payload() === null);

// A platform that gives no message ids must still deduplicate.
batch = new CVC.Batcher({ now: now });
batch.observe(convo([{ role: "user", content: "no id here" }]));
batch.observe(convo([{ role: "user", content: "no id here" }]));
check("messages without ids deduplicate on role and content",
      batch.pending() === 1, batch.pending());
batch.observe(convo([
  { role: "user", content: "no id here" },
  { role: "user", content: "a different one" }
]));
check("but genuinely new content is added", batch.pending() === 2,
      batch.pending());

check("an empty conversation is ignored",
      batch.observe(null) === 0 && batch.observe({}) === 0);

console.log("\n" + (fails.length === 0 ? "ALL CHECKS PASSED"
                    : fails.length + " FAILURES: " + fails.join(", ")));
process.exit(fails.length === 0 ? 0 : 1);
