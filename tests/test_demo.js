/* The standalone demo page: data, search behaviour, and the embedded copy.
 *
 *     node tests/test_demo.js
 *
 * The demo has to work when index.html is opened straight from disk, which
 * means the data is embedded in the page as well as living in data.json.
 * Part A keeps those two byte-identical -- editing one and forgetting the
 * other would ship a page that disagrees with its own source file.
 *
 * Part B covers the fixture's promises, C the search itself, D the page
 * wiring. There is no DOM here, so what the browser renders is not asserted;
 * the search module is DOM-free by design and that is what these drive.
 */
"use strict";

const fs = require("fs");
const path = require("path");

const DEMO = path.join(__dirname, "..", "demo");
const Search = require(path.join(DEMO, "search.js"));

let fails = [];

function check(label, ok, detail) {
  console.log((ok ? "  ok   " : "  FAIL ") + label
              + (detail !== undefined && detail !== "" ? "  -> " + detail : ""));
  if (!ok) fails.push(label);
}

/* Git checks these out with CRLF on Windows, so every anchored newline in a
 * pattern below would miss. Normalise on read: the comparisons here are about
 * content, not about which line ending the working copy happens to have. */
function read(name) {
  return fs.readFileSync(path.join(DEMO, name), "utf8").replace(/\r\n/g, "\n");
}

const rawJson = read("data.json");
const html = read("index.html");
const searchJs = read("search.js");
const readme = read("README.md");

/* HTML comments explain why things are done; they are not what the page does.
 * Strip them before asserting on behaviour, or a comment saying "no fetch
 * here" trips a check looking for fetch. */
const htmlCode = html.replace(/<!--[\s\S]*?-->/g, "");

const data = JSON.parse(rawJson);
const conversations = data.conversations;

// ======================================================================
console.log("== A. the embedded copy matches data.json ==");

check("data.json is valid JSON", !!data && Array.isArray(conversations));

const block = /<script type="application\/json" id="demo-data">\n([\s\S]*?)\n<\/script>/
  .exec(html);
check("index.html carries an embedded data block", !!block);

if (block) {
  const embedded = block[1];
  check("the embedded copy is byte-identical to data.json",
        embedded === rawJson.replace(/\n+$/, ""),
        embedded === rawJson.replace(/\n+$/, "") ? ""
          : "lengths " + embedded.length + " vs " + rawJson.replace(/\n+$/, "").length);
  let parsed = null;
  try { parsed = JSON.parse(embedded); } catch (error) { parsed = null; }
  check("the embedded copy parses on its own", !!parsed);
  check("and holds the same conversations",
        parsed && parsed.conversations.length === conversations.length);
  check("the placeholder was replaced", html.indexOf("__DEMO_DATA__") === -1);
}

// The page must not need a network fetch: that is what breaks under file://.
check("the page does not fetch() its data",
      htmlCode.indexOf("fetch(") === -1);
// Links out are fine; a script or stylesheet from another host is not, since
// the demo has to work offline and from disk.
check("no external script or stylesheet is loaded",
      !/<(script|link)\b[^>]*\b(src|href)\s*=\s*["']https?:/i.test(htmlCode));

// ======================================================================
console.log("\n== B. the fixture keeps its promises ==");

check("there are 10 to 15 conversations",
      conversations.length >= 10 && conversations.length <= 15,
      conversations.length);

const providers = new Set(conversations.map(c => c.provider));
["ChatGPT", "Claude", "Gemini"].forEach(p => {
  check("includes " + p, providers.has(p));
});
check("no other providers sneak in", providers.size === 3,
      [...providers].join(", "));

conversations.forEach(c => {
  const okShape = c.id && c.provider && c.title && c.date
    && Array.isArray(c.messages) && c.messages.length > 0;
  if (!okShape) check("conversation " + (c.id || "?") + " is well formed", false,
                      JSON.stringify(c).slice(0, 80));
});
check("every conversation is well formed",
      conversations.every(c => c.id && c.provider && c.title && c.date
                          && Array.isArray(c.messages) && c.messages.length));
check("every message has a role and content",
      conversations.every(c => c.messages.every(
        m => (m.role === "user" || m.role === "assistant") && m.content)));
check("ids are unique",
      new Set(conversations.map(c => c.id)).size === conversations.length);
check("dates are ISO-ish",
      conversations.every(c => /^\d{4}-\d{2}-\d{2}$/.test(c.date)));

// The topics the demo is supposed to cover.
const corpus = conversations.map(
  c => c.title + " " + c.messages.map(m => m.content).join(" ")).join(" ")
  .toLowerCase();
[["project planning", "phoenix"], ["fitness", "gym"], ["recipes", "sourdough"],
 ["travel", "kyoto"], ["coding", "postgres"], ["book recommendations", "read"]
].forEach(([topic, marker]) => {
  check("covers " + topic, corpus.indexOf(marker) !== -1);
});

const welcome = conversations.find(c => c.title === "Welcome");
check("there is a conversation titled Welcome", !!welcome);
check("its title says nothing about what it discusses",
      welcome && welcome.title.toLowerCase().indexOf("gym") === -1);
check("but its content does",
      welcome && Search.transcript(welcome).toLowerCase().indexOf("gym") !== -1);

check("chains are declared", Array.isArray(data.chains) && data.chains.length >= 1,
      data.chains && data.chains.length);
check("every chain has at least two members",
      data.chains.every(ch => ch.conversations.length >= 2));
check("chained conversations have different titles",
      data.chains.every(ch => {
        const titles = ch.conversations.map(
          id => (conversations.find(c => c.id === id) || {}).title);
        return new Set(titles).size === titles.length;
      }));
check("every chain member exists",
      data.chains.every(ch => ch.conversations.every(
        id => conversations.some(c => c.id === id))));

// The fixture is meant to be synthetic; a stray real detail would be a leak.
check("the data declares itself synthetic",
      /synthetic/i.test(data.note || ""));

// ======================================================================
console.log("\n== C. search ==");

let hits = Search.search(conversations, "gym");
check("searching 'gym' returns something", hits.length >= 1, hits.length);
check("Welcome is found by its contents, not its title",
      hits.some(h => h.title === "Welcome"),
      hits.map(h => h.title));
const welcomeHit = hits.find(h => h.title === "Welcome");
check("and is badged as a content match",
      welcomeHit && welcomeHit.matchType === "content",
      welcomeHit && welcomeHit.matchType);
check("its snippet shows the matching text",
      welcomeHit && /gym/i.test(welcomeHit.snippet),
      welcomeHit && welcomeHit.snippet.slice(0, 50));

hits = Search.search(conversations, "sourdough");
check("a title word ranks its conversation first",
      hits[0] && /sourdough/i.test(hits[0].title), hits[0] && hits[0].title);
check("title matches are badged as such",
      hits[0] && hits[0].matchType.indexOf("title") === 0,
      hits[0] && hits[0].matchType);

check("never more than five results",
      Search.search(conversations, "the a is").length <= 5);
check("an empty query returns nothing",
      Search.search(conversations, "").length === 0);
check("whitespace returns nothing",
      Search.search(conversations, "   ").length === 0);
check("a term nobody used returns nothing",
      Search.search(conversations, "zzzqqxx").length === 0);

check("search is case insensitive",
      Search.search(conversations, "SOURDOUGH").length
      === Search.search(conversations, "sourdough").length);

// Multi-term queries should favour conversations covering more of the query.
hits = Search.search(conversations, "connection pool");
check("'connection pool' finds the slow-requests conversation",
      hits.some(h => /slow under load/i.test(h.title)),
      hits.map(h => h.title));

hits = Search.search(conversations, "Q4 launch");
check("'Q4 launch' finds a Phoenix conversation",
      hits.some(h => /phoenix|ship/i.test(h.title)),
      hits.map(h => h.title));

// Results carry what the page renders.
hits = Search.search(conversations, "kyoto");
check("results carry provider, date, snippet and badge",
      hits.length > 0 && hits.every(
        h => h.provider && h.date && h.snippet && h.matchType));
check("results carry the matched terms for highlighting",
      hits.every(h => Array.isArray(h.matchedTerms) && h.matchedTerms.length));
check("results are sorted by score, descending",
      hits.every((h, i) => i === 0 || hits[i - 1].score >= h.score));

// Snippets have to be short enough to read in a card.
check("snippets stay short",
      Search.search(conversations, "starter").every(h => h.snippet.length <= 260),
      Math.max(...Search.search(conversations, "starter").map(h => h.snippet.length)));

// Punctuation in a query must not throw a regex error.
["c++", "what's", "a.b*c", "(paren", "back\\slash"].forEach(q => {
  let threw = false;
  try { Search.search(conversations, q); } catch (error) { threw = true; }
  check("a query containing " + JSON.stringify(q) + " does not throw", !threw);
});

check("chainFor finds a conversation's chain",
      (Search.chainFor(data.chains, "chatgpt:demo-002") || {}).name === "Sourdough");
check("chainFor returns null for an unchained conversation",
      Search.chainFor(data.chains, "chatgpt:demo-001") === null);

// ======================================================================
console.log("\n== D. the page ==");

check("the value proposition is on the page",
      html.indexOf("Search everything you&#39;ve told any AI") !== -1
      || html.indexOf("Search everything you've told any AI") !== -1);
check("there is a search input", /<input[^>]+id="q"/.test(html));
check("results render into a container", html.indexOf('id="results"') !== -1);
check("the feedback section asks the question",
      /Would you use this\?/.test(html));
["yes", "maybe", "no"].forEach(answer => {
  check("there is a " + answer + " button",
        html.indexOf('data-answer="' + answer + '"') !== -1);
});
check("search is debounced by 300ms", /DEBOUNCE_MS\s*=\s*300/.test(html));
check("the page loads search.js", html.indexOf('src="search.js"') !== -1);

check("it is mobile responsive", /@media \(max-width/.test(html));
check("the viewport is declared", /name="viewport"/.test(html));
check("inputs are at least 16px so iOS does not zoom",
      /font-size:\s*1[6-9]px/.test(html));
check("dark mode is the default", /color-scheme:\s*dark/.test(html));
check("reduced motion is honoured", /prefers-reduced-motion/.test(html));

// Provider colours, as specified.
check("ChatGPT is green", /--chatgpt:\s*#10a37f/i.test(html));
check("Claude is orange", /--claude:\s*#d97757/i.test(html));
check("Gemini is blue", /--gemini:\s*#4285f4/i.test(html));
// Collapse runs of spaces: the stylesheet aligns these rules for readability.
const cssFlat = html.replace(/[ \t]+/g, " ");
["ChatGPT", "Claude", "Gemini"].forEach(p => {
  check(p + " has a coloured marker",
        cssFlat.indexOf('.provider[data-p="' + p + '"] .dot') !== -1);
});

// Rendered text is escaped before highlighting, so fixture text cannot inject.
check("the page escapes before it highlights",
      html.indexOf("function escapeHtml") !== -1
      && html.indexOf("highlight(escapeHtml(") !== -1);

// The demo must not overclaim: no semantic badge, since it does no embedding.
check("search.js never claims a semantic match",
      searchJs.indexOf("semantic") === -1
      || /not[\s\S]{0,80}semantic/i.test(searchJs));
check("the page never renders a 'semantic' badge",
      !/badge[^>]*>semantic/i.test(html));
check("the feedback UI says nothing is transmitted",
      /nothing was sent|Nothing is sent/i.test(html));

// ======================================================================
console.log("\n== E. the README ==");

check("it explains opening index.html directly",
      /index\.html/.test(readme));
check("it covers GitHub Pages", /GitHub Pages/i.test(readme));
check("it covers Netlify Drop", /Netlify Drop/i.test(readme));
check("it gives the Pages URL shape",
      /github\.io/.test(readme));
check("it says the data is synthetic", /synthetic/i.test(readme));
check("it is honest that search is keyword only",
      /keyword/i.test(readme) && /not[\s\S]{0,120}semantic/i.test(readme));

console.log("\n" + (fails.length === 0 ? "ALL CHECKS PASSED"
                    : fails.length + " FAILURES: " + fails.join(", ")));
process.exit(fails.length === 0 ? 0 : 1);
