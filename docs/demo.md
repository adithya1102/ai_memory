# AI Memory — a walkthrough

This is the canonical demo. It uses three small exports in `docs/demo_data/`,
each in its provider's real format, and every block of output below was
captured from an actual run.

To reproduce the whole thing:

```bash
python docs/demo.py
```

It works against a temporary database and never touches your own library.

---

## 1. The problem: three assistants, three separate memories

Over a month, one person asked three different assistants for help.

**In March, they asked ChatGPT how to start training.** ChatGPT gave them a
**4-day upper/lower split** and some advice on protein.

**Three weeks later they asked Gemini the same kind of question**, phrased
around stamina rather than muscle. Gemini gave them a **5-day
endurance plan** — and told them to keep heavy lifting to a minimum, because
it competes for recovery.

**Claude was never asked about training at all.** It was used for work:
designing a demand-forecasting model, and a question about Postgres
connection pooling.

Here is the problem in one line. *Those two training plans contradict each
other, and nothing in the world knows that.* ChatGPT has never heard of the
Gemini plan. Gemini has never heard of the ChatGPT one. Claude has no idea
either conversation happened. And the person, three months later, remembers
only that "someone told me four days and someone told me five."

There is no such thing as **your** memory across assistants. There are three
vendors' memories of you, none of which talk to each other, and none of which
you can search properly.

```
  chatgpt_export.json    detected as chatgpt
  claude_export.json     detected as claude
  gemini_export.json     detected as gemini
```

Three formats, three shapes. ChatGPT exports a message *tree*; Claude a flat
`chat_messages` list with a `human` sender; Gemini a `messages` list with a
`model` author. AI Memory sniffs the shape, so you never have to say which is
which.

## 2. The import

In the app, open **Import** and pick each file in turn — the provider is
detected from the file itself. The demo script does the same thing
programmatically:

```
  chatgpt_export.json    provider=chatgpt  new=4 messages=10 chains=1
  claude_export.json     provider=claude   new=2 messages=6 chains=2
  gemini_export.json     provider=gemini   new=2 messages=6 chains=2

  Library now holds 8 conversations from 3 providers:
    ChatGPT    4
    Claude     2
    Gemini     2
```

Three vendors, one SQLite file, one search index. Re-importing any of them
imports nothing the second time — conversations are matched on id, then on a
hash of the transcript.

## 3. The search

### "gym routine" — the contradiction, in one query

```
$ search 'gym routine'

  Weekly gym routine for endurance       [both]  72% similar
  Gemini · 2025-03-22
  "… What weekly gym routine should I follow? Here is a 5-day
   endurance-focused plan: Monday - 40 minutes steady-state …"

  Welcome                                [semantic]  60% similar
  ChatGPT · 2025-03-01
  "I want to build muscle and start going to the gym. What should my
   workout plan look like for a beginner?"

  Hydration and electrolytes             [semantic]  24% similar
  Gemini · 2025-03-24
  "For under 60 to 90 minutes in temperate conditions, water is fine…"
```

Both training plans, side by side, for the first time. The 5-day Gemini plan
and the 4-day ChatGPT plan, retrieved by one query against one library.

Note what is **not** in that list: nothing from Claude. Claude was never asked
about the gym, so it has nothing to contribute — which is exactly right, and
exactly what you could not previously verify without opening three apps.

Note also *how* ChatGPT's conversation was found. It is titled **"Welcome"**.
The words "gym routine" appear nowhere in that title. Keyword search on the
title — which is all the ChatGPT sidebar gives you — would never surface it.

### "how do I get stronger" — no shared words at all

```
$ search 'how do I get stronger'

  Weekly gym routine for endurance       [semantic]  39% similar
  Gemini · 2025-03-22
  "Should I lift heavy as well?"

  Welcome                                [semantic]  35% similar
  ChatGPT · 2025-03-01
  "Here is a 4-day upper/lower split, which is the sweet spot for a
   beginner who can train four times a week: Day 1 - Upper (push):
   bench press, overhead press…"
```

Not one word of *"how do I get stronger"* — not "stronger", not "get" —
appears in either conversation. Keyword search returns **nothing** for this
query. Both results are badged `semantic`: the embedding knows that getting
stronger, building muscle and lifting heavy are the same question.

This is the query that justifies the whole feature. You do not have to
remember what you typed. You only have to remember what you meant.

### "Gusto forecasting" — the same project, two assistants

```
$ search 'Gusto forecasting'

  Gusto forecasting discussion           [both]  57% similar
  ChatGPT · 2025-03-13
  "Gusto forecasting discussion"

  Gusto demand model architecture        [both]  68% similar
  Claude · 2025-03-08
  "Let's design the Gusto architecture for demand forecasting. I need to
   predict restaurant covers for next quarter. A hybrid …"
```

One project, worked on with two different assistants five days apart. The
architecture was designed with Claude; the seasonality tuning happened later
with ChatGPT. Neither assistant knows the other half exists. The library does.

## 4. The MCP integration

Searching yourself is useful. Having an assistant search *for* you is the
point.

Start the server — either let Claude Desktop launch it (below), or flip
**Settings → Start MCP server** to run it on a local socket.

### Connecting Claude Desktop

Add this to `claude_desktop_config.json` and restart. **Settings → MCP server**
prints it with your real paths already filled in:

```json
{
  "mcpServers": {
    "ai-memory": {
      "command": "python",
      "args": ["/absolute/path/to/ai_memory/mcp_server.py"]
    }
  }
}
```

### What the assistant actually receives

You can see exactly what a client gets, without a client:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_memory","arguments":{"query":"gym routine","limit":3}}}' | python mcp_server.py
```

Real output, trimmed to two results:

```json
{
  "query": "gym routine",
  "count": 3,
  "results": [
    {
      "conversation_id": "gemini:gm-001",
      "title": "Weekly gym routine for endurance",
      "provider": "Gemini",
      "date": "2025-03-22T07:30:00Z",
      "snippet": "… What weekly gym routine should I follow?\nHere is a 5-day endurance-focused plan:\n\nMonday - 40 minutes steady-state …",
      "relevance_score": 0.032787,
      "match_type": "both"
    },
    {
      "conversation_id": "chatgpt:cg-001",
      "title": "Welcome",
      "provider": "ChatGPT",
      "date": "2025-03-01T00:00:00+00:00",
      "snippet": "I want to build muscle and start going to the gym. What should my workout plan look like for a beginner?",
      "relevance_score": 0.016129,
      "match_type": "semantic"
    }
  ]
}
```

The `provider` field is what makes the next part possible: the assistant can
see that these two answers came from two different systems.

### In Claude Desktop

> **You:** Search my memory for my gym routine.

Claude calls `search_memory`, receives the JSON above, and can then call
`get_conversation` on either id to read the full transcript before answering.
Having done so it has everything it needs to tell you that you are holding two
different plans:

> **Claude:** I found two different training plans in your history, and they
> don't agree.
>
> **ChatGPT, 1 March** — a 4-day upper/lower split built for hypertrophy:
> push, legs, pull, legs, 3×8–12 with progressive overload. Paired with
> 120–165 g of protein a day for your 75 kg bodyweight.
>
> **Gemini, 22 March** — a 5-day endurance plan: zone-2 cardio, intervals, a
> tempo run and a long weekend session, with 80% of volume kept easy. It
> explicitly told you to *limit* heavy lifting, since it competes with
> endurance work for recovery.
>
> These optimise for opposite adaptations, so following both would leave you
> under-recovered for each. Nothing in your Claude history touches training at
> all — that thread never happened here.

<sup>The wording above is illustrative — an assistant's phrasing varies. The
tool call and the JSON it returns are real and reproducible with the command
above; what Claude says next is Claude's.</sup>

That last line is the part worth noticing. The assistant can report on a gap
in its *own* memory, because the memory it is reading is yours, not its own.

## 5. The chains

Chains group conversations you returned to over time, so a topic reads as one
thread instead of scattered sessions.

```
  Chain 1: Gusto (2 conversations)
    1. Gusto demand model architecture        Claude · 2025-03-08
    2. Gusto forecasting discussion           ChatGPT · 2025-03-13

  Chain 2: Sourdough Starter (2 conversations)
    1. Sourdough starter troubleshooting      ChatGPT · 2025-03-21
    2. Sourdough starter feeding schedule     ChatGPT · 2025-03-28
```

**Chain 1 crosses providers.** Nobody told it to. The architecture discussion
happened with Claude on 8 March and the follow-up with ChatGPT on 13 March,
and the chain reads in that order because chains are ordered by creation date,
not by import order or by provider. You can read a project's thinking as one
sequence even though it happened in two different apps.

Chain 2 shows the timeline within a single provider: the starter was failing
on 21 March, and by 28 March it was alive and the question had become how to
maintain it. Same subject, seven days apart, correctly ordered.

Open either from **Chains** in the app, or over MCP:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_conversation_chain","arguments":{"chain_id":1}}}' | python mcp_server.py
```

---

## What this demo is not

Being straight about the edges, since this is the document people will judge
the project by:

- **Chain detection reads titles only.** Chain 1 works because both titles
  contain "Gusto". Two conversations about the same thing with unrelated
  titles will not chain. This is a known weakness, not a subtlety.
- **Chain ids are stable for a given library, but not across changes.**
  Detection rebuilds every chain on import, so ids are a function of current
  contents. Do not store one long-term.
- **The Gemini adapter targets a documented shape, not a verified one.**
  Google Takeout's Gemini format is not publicly specified and has changed;
  this adapter handles the common form and several field-name variants. If
  your real export does not import cleanly, the parsing is what needs
  adjusting — see [CONTRIBUTING.md](../CONTRIBUTING.md).
- **The demo data is synthetic.** It was written to make the contradiction
  legible in eight conversations. Real libraries are messier, and the
  similarity numbers on a 4,000-conversation library will look different.
- **Weak semantic matches surface.** "Hydration and electrolytes" at 24% is a
  real but marginal hit. The threshold is deliberately low, because genuine
  paraphrase matches score around 0.3 and a strict floor would discard them.

## Next

- [README](../README.md) — install, import your own history, how it works
- [ROADMAP](../ROADMAP.md) — what is planned, and what is deliberately not
- [CONTRIBUTING](../CONTRIBUTING.md) — writing an adapter for another assistant
