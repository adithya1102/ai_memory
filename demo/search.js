/* Client-side search for the ContextVault demo.
 *
 * This is keyword matching only, and it is deliberately honest about that.
 * The real app runs two engines -- FTS5 for words and local embeddings for
 * meaning -- and fuses them. Embeddings need a ~90 MB model and a vector
 * index, neither of which belongs in a page that has to open with no setup,
 * so the demo does the half it can do properly rather than faking the other.
 *
 * What it does show is the part people find surprising: that searching the
 * *contents* of conversations finds things their titles never mention.
 *
 * No DOM in here, so tests/test_demo.js can run it under node.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.DemoSearch = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var MAX_RESULTS = 5;
  var SNIPPET_RADIUS = 90;

  var STOP_WORDS = {
    a: 1, an: 1, and: 1, are: 1, as: 1, at: 1, be: 1, but: 1, by: 1, do: 1,
    for: 1, from: 1, how: 1, i: 1, if: 1, in: 1, is: 1, it: 1, my: 1, of: 1,
    on: 1, or: 1, s: 1, should: 1, that: 1, the: 1, to: 1, was: 1, what: 1,
    when: 1, with: 1, you: 1
  };

  function tokenise(text) {
    return String(text || "").toLowerCase().match(/[a-z0-9]+/g) || [];
  }

  /* Words worth scoring. Stop words are dropped so "how do I" does not match
   * every conversation equally, but if the query is *only* stop words we keep
   * them -- otherwise a search for "how to" would silently match nothing with
   * no explanation. */
  function queryTerms(query) {
    var all = tokenise(query);
    var meaningful = all.filter(function (term) { return !STOP_WORDS[term]; });
    return meaningful.length ? meaningful : all;
  }

  function transcript(conversation) {
    return (conversation.messages || []).map(function (message) {
      return message.content;
    }).join("\n");
  }

  /* Where a term first appears, matching whole words so "run" does not hit
   * "running" at a random offset and produce a confusing snippet. */
  function findTerm(haystack, term) {
    var pattern = new RegExp("\\b" + term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
                             "i");
    var match = pattern.exec(haystack);
    return match ? match.index : -1;
  }

  function countTerm(haystack, term) {
    var pattern = new RegExp("\\b" + term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"),
                             "gi");
    var matches = haystack.match(pattern);
    return matches ? matches.length : 0;
  }

  /* A window of text around the first match, cut on word boundaries. */
  function snippetAround(text, index) {
    if (index < 0) {
      return text.length > SNIPPET_RADIUS * 2
        ? text.slice(0, SNIPPET_RADIUS * 2).replace(/\s+\S*$/, "") + "…"
        : text;
    }
    var start = Math.max(0, index - SNIPPET_RADIUS);
    var end = Math.min(text.length, index + SNIPPET_RADIUS);
    var slice = text.slice(start, end);
    if (start > 0) slice = slice.replace(/^\S*\s+/, "… ");
    if (end < text.length) slice = slice.replace(/\s+\S*$/, " …");
    return slice.replace(/\s+/g, " ").trim();
  }

  /* Score one conversation.
   *
   * Title hits are worth more than body hits -- a conversation named after
   * your query is almost always the one you meant -- but body hits still
   * count, which is the whole point of the "Welcome" example.
   */
  function scoreConversation(conversation, terms) {
    var title = conversation.title || "";
    var body = transcript(conversation);

    var titleHits = 0;
    var bodyHits = 0;
    var matched = [];

    terms.forEach(function (term) {
      var inTitle = countTerm(title, term);
      var inBody = countTerm(body, term);
      if (inTitle || inBody) matched.push(term);
      titleHits += inTitle;
      bodyHits += inBody;
    });

    if (!matched.length) return null;

    // Conversations matching more distinct query terms rank above those that
    // merely repeat one of them.
    var coverage = matched.length / terms.length;
    var score = (titleHits * 8 + bodyHits) * (0.4 + 0.6 * coverage);

    var where = titleHits && bodyHits ? "title + content"
              : titleHits ? "title" : "content";

    var firstTerm = matched[0];
    var index = findTerm(body, firstTerm);
    return {
      conversation: conversation,
      id: conversation.id,
      title: title,
      provider: conversation.provider,
      date: conversation.date,
      score: score,
      matchType: where,
      matchedTerms: matched,
      snippet: snippetAround(body, index)
    };
  }

  function search(conversations, query, limit) {
    var terms = queryTerms(query);
    if (!terms.length) return [];

    var scored = [];
    (conversations || []).forEach(function (conversation) {
      var result = scoreConversation(conversation, terms);
      if (result) scored.push(result);
    });

    scored.sort(function (a, b) {
      if (b.score !== a.score) return b.score - a.score;
      // Stable, readable tie-break rather than whatever order the data was in.
      return String(a.title).localeCompare(String(b.title));
    });

    return scored.slice(0, limit || MAX_RESULTS);
  }

  /* Which chain, if any, a conversation belongs to. */
  function chainFor(chains, conversationId) {
    var found = null;
    (chains || []).forEach(function (chain) {
      if ((chain.conversations || []).indexOf(conversationId) !== -1) {
        found = chain;
      }
    });
    return found;
  }

  return {
    MAX_RESULTS: MAX_RESULTS,
    tokenise: tokenise,
    queryTerms: queryTerms,
    transcript: transcript,
    snippetAround: snippetAround,
    scoreConversation: scoreConversation,
    chainFor: chainFor,
    search: search
  };
});
