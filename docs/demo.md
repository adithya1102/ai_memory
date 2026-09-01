# ContextVault — a walkthrough

This is the canonical demo. It uses three small exports in `docs/demo_data/`,
each in its provider's real format, and every block of output below was
captured from an actual run.

To reproduce the whole thing:

```bash
python docs/demo.py
```

It works against a temporary database and never touches your own library.

---

## 1. The problem: three AIs, same user, different memories

One person. One training plan. Three assistants, asked the same question:
**"What's my gym routine?"**

| AI Assistant | What it knows about "gym routine" |
|---|---|
| **ChatGPT** | 4-day split; Day 2 is lower body; cleared up the user's leg extension vs leg raise confusion; knows the whole point is a Himalayan trek next year |
| **Claude** | Nothing — *"I don't have a gym routine on file"* |
| **Gemini** | 5-day endurance circuit; told them to book a 2-hour off-peak workout block |

Three answers. All three incomplete. None of them wrong, exactly — each one is
a faithful account of the fraction of the story it happened to be told.

**ChatGPT** has the richest picture. It designed the 4-day split, and when the
user said their quads weren't sore, it worked out they had been doing *leg
raises* (an ab exercise) instead of *leg extensions* (a quad exercise) on Day
2. It also knows the goal is a Himalayan trek, and reshaped the plan around
loaded hiking because of it.

**Gemini** knows none of that. Asked a differently-phrased question three
weeks later, it produced a 5-day endurance circuit — a plan that explicitly
tells the user to *limit* heavy lifting, directly contradicting ChatGPT's
progressive-overload advice. It also solved a problem ChatGPT never heard
about: the gym is packed after work, so block out two hours off-peak.

**Claude** knows nothing at all. Asked point blank in April, it answered
honestly:

> I don't have a gym routine on file for you. I have no record of us
> discussing your training — as far as I can see, this is the first time you
> have mentioned it to me.

That is the problem in one sentence. The information exists. The user
generated all of it. It is simply scattered across three vendors who cannot
see each other, and the only person who could assemble it is the one who has
forgotten the details — which is why they were asking in the first place.

There is no such thing as *your* memory across assistants. There are three
companies' partial memories of you, none of which you can search properly.

## 2. The import

Start the app:

```bash
cd contextvault
python backend/main.py --no-window
```

Open `http://127.0.0.1:5000`, go to **Import**, and pick each export in turn.
You never have to say which assistant a file came from — the provider is
detected from the file's shape:

```
  chatgpt_export.json    detected as chatgpt
  claude_export.json     detected as claude
  gemini_export.json     detected as gemini
```

Three vendors, three completely different formats. ChatGPT exports a message
*tree*, where every regeneration is a branch. Claude exports a flat
`chat_messages` list with a `human` sender. Gemini exports a `messages` list
with a `model` author. Each gets its own adapter; everything downstream sees
one universal format.

Importing all three:

```
  chatgpt_export.json    provider=chatgpt  new=4 messages=14 chains=1
  claude_export.json     provider=claude   new=3 messages=8 chains=2
  gemini_export.json     provider=gemini   new=2 messages=8 chains=3

  Library now holds 9 conversations from 3 providers:
    ChatGPT    4
    Claude     3
    Gemini     2
```

One SQLite file. One search index. Re-import any of them and nothing
duplicates — conversations are matched on id, then on a hash of the
transcript, scoped per provider.

## 3. The search

### "gym routine" — the whole picture, for the first time

```
$ search 'gym routine'

  Gym routine check                      [both]  79% similar
  Claude · 2025-04-02
  "What's my gym routine? Remind me what I'm supposed to be doing on
   day 2. I don't …"

  Weekly gym routine for endurance       [both]  72% similar
  Gemini · 2025-03-22
  "… What weekly gym routine should I follow? Here is a 5-day endurance
   circuit: Monday - 40 minutes steady-state cardio …"

  Welcome                                [semantic]  60% similar
  ChatGPT · 2025-03-01
  "I want to build muscle and start going to the gym. What should my
   workout plan look like for a beginner?"
```

All three assistants, in one list, ranked. This is the table from section 1,
assembled — and it took one query.

Read the third result again. ChatGPT's conversation — the one holding the
4-day split, the leg extension correction and the Himalayan trek — is titled
**"Welcome"**. The words "gym routine" appear nowhere in that title. In
ChatGPT's own sidebar, which searches titles only, it is effectively lost. It
is found here because the search reads message bodies, and matched on meaning
at 60%.

The second result is Claude's *"I don't have a gym routine on file"*. Worth
keeping, not filtering out: it is the evidence of the gap, and it is the
reason the user went looking.

### "how do I get stronger" — no shared words at all

```
$ search 'how do I get stronger'

  Weekly gym routine for endurance       [semantic]  39% similar
  Gemini · 2025-03-22
  "Should I lift heavy as well?"

  Gym routine check                      [semantic]  34% similar
  Claude · 2025-04-02
  "I don't have a gym routine on file for you. I have no record of us
   discussing your training …"
```

Not one word of *"how do I get stronger"* — not "stronger", not "get" —
appears in any of these conversations. Keyword search returns **nothing** at
all for this query. Both hits are badged `semantic`: the embedding knows that
getting stronger, lifting heavy and training are the same subject.

This is the query that justifies the feature. You do not have to remember what
you typed. You only have to remember what you meant.

### "Gusto forecasting" — one project, two assistants

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

The same project, five days apart, in two different apps. The architecture was
designed with Claude on 8 March; the seasonality tuning happened with ChatGPT
on 13 March. Neither assistant knows the other half exists.

## 4. The MCP integration

Searching yourself is useful. Having an assistant search *for* you is the
point.

### Start the server

Either flip **Settings → Start MCP server** to run it on a local socket, or —
for Claude Desktop — let the client launch it. Add this to
`claude_desktop_config.json` and restart. **Settings → MCP server** prints it
with your real paths already filled in:

```json
{
  "mcpServers": {
    "contextvault": {
      "command": "python",
      "args": ["/absolute/path/to/contextvault/mcp_server.py"]
    }
  }
}
```

### What the assistant actually receives

You can see exactly what a client gets, without a client:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_memory","arguments":{"query":"gym routine","limit":3}}}' | python mcp_server.py
```

Real output, trimmed to two of the three results:

```json
{
  "query": "gym routine",
  "count": 3,
  "results": [
    {
      "conversation_id": "claude:cl-000",
      "title": "Gym routine check",
      "provider": "Claude",
      "date": "2025-04-02T20:11:00Z",
      "snippet": "What's my gym routine? Remind me what I'm supposed to be doing on day 2. I don't …",
      "relevance_score": 0.032787,
      "match_type": "both"
    },
    {
      "conversation_id": "chatgpt:cg-001",
      "title": "Welcome",
      "provider": "ChatGPT",
      "date": "2025-03-01T00:00:00+00:00",
      "snippet": "I want to build muscle and start going to the gym. What should my workout plan look like for a beginner?",
      "relevance_score": 0.015625,
      "match_type": "semantic"
    }
  ]
}
```

The `provider` field is what makes the next part work: the assistant can see
that these answers came from three different systems, and that one of them was
its own.

### In Claude Desktop

> **You:** Search my memory for my gym routine.

Claude calls `search_memory`, gets the JSON above, then calls
`get_conversation` on the ids to read the transcripts in full before
answering:

> **Claude:** You have two different plans on record, from two different
> assistants, and they don't agree.
>
> **ChatGPT, 1 March — a 4-day split.** Day 1 upper push, **Day 2 lower body**
> (squats, Romanian deadlifts, leg extensions, calf raises), Day 3 rest, Day 4
> upper pull, Day 5 lower. 3×8–12 with progressive overload, and 120–165 g of
> protein a day for your 75 kg.
>
> Two things from that conversation you'll want: you had been doing **leg
> raises instead of leg extensions** on Day 2 — an ab exercise where a quad
> exercise should be, which is why your quads weren't sore. And because you're
> trekking in the **Himalayas** next year, ChatGPT added a weekly loaded hike
> building towards four hours, and told you not to drop strength work, since
> strong legs protect your knees on long descents.
>
> **Gemini, 22 March — a 5-day endurance circuit**, with 80% of volume easy,
> and advice to book a **2-hour off-peak block** because your gym is packed
> after work. It explicitly told you to *limit* heavy lifting.
>
> These conflict: ChatGPT's plan is built on progressive overload, Gemini's
> asks you to keep lifting light. Given the Himalayan trek, ChatGPT's hybrid —
> keep the 4-day split, add the loaded hike — is the closer fit, and Gemini's
> off-peak scheduling advice is worth keeping regardless.
>
> One more thing: I asked myself. On 2 April you asked *me* this same question
> and I told you I had no record of it. That's still true — I had no way to
> see either of these conversations until now.

<sup>The wording above is illustrative; an assistant's phrasing varies. The
tool call and the JSON it returns are real and reproducible with the command
above. What Claude says next is Claude's.</sup>

That last paragraph is the one to notice. The assistant can report on a gap in
its own memory, because the memory it is reading is yours rather than its own.

## 5. The chains

Chains group conversations you returned to over time, so a topic reads as one
thread instead of scattered sessions.

```
  Chain 1: Gusto (2 conversations)
    1. Gusto demand model architecture        Claude · 2025-03-08
    2. Gusto forecasting discussion           ChatGPT · 2025-03-13

  Chain 2: Gym Routine (2 conversations)
    1. Weekly gym routine for endurance       Gemini · 2025-03-22
    2. Gym routine check                      Claude · 2025-04-02

  Chain 3: Sourdough Starter (2 conversations)
    1. Sourdough starter troubleshooting      ChatGPT · 2025-03-21
    2. Sourdough starter feeding schedule     ChatGPT · 2025-03-28
```

**Two of these three chains cross providers, and nobody told them to.**

Chain 1 is a work project: designed with Claude on 8 March, tuned with ChatGPT
five days later. Chain 2 is the gym story: Gemini's plan on 22 March, then the
April conversation where Claude had nothing on file. Both read in date order,
because chains are ordered by when the conversation happened — not by import
order, and not by provider.

That ordering is the point. The timeline survives the fragmentation. You can
read a project's thinking as one sequence even though it happened in two
different apps, weeks apart.

Chain 3 shows the same thing inside a single provider: the starter was failing
on 21 March, and by 28 March it was alive and the question had become how to
maintain it. Same subject, seven days apart, correctly ordered.

Open any of them from **Chains** in the app, or over MCP:

```bash
echo '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_conversation_chain","arguments":{"chain_id":2}}}' | python mcp_server.py
```

---

## What this demo is not

Being straight about the edges, since this is the document people will judge
the project by:

- **Chain detection reads titles only.** Chains 1 and 2 work because the
  titles share words. ChatGPT's *"Welcome"* — the richest gym conversation in
  the library — is **not** in the Gym Routine chain, because its title says
  nothing about the gym. Search finds it; chaining does not. That is a real
  weakness, not a subtlety.
- **Chain ids are stable for a given library, not across changes.** Detection
  rebuilds every chain on import, so ids are a function of current contents.
  Do not store one long-term.
- **The Gemini adapter targets a documented shape, not a verified one.**
  Google Takeout's Gemini format is not publicly specified and has changed;
  this adapter handles the common form and several field-name variants. If a
  real export does not import cleanly, the parsing is what needs adjusting —
  see [CONTRIBUTING.md](../CONTRIBUTING.md).
- **The demo data is synthetic.** It was written to make the fragmentation
  legible in nine conversations. Real libraries are messier, and similarity
  numbers on a 4,000-conversation library will look different.
- **Weak semantic matches surface.** The threshold is deliberately low,
  because genuine paraphrase matches score around 0.3 and a strict floor would
  discard exactly the results the feature exists to find.

## Next

- [README](../README.md) — install, import your own history, how it works
- [ROADMAP](../ROADMAP.md) — what is planned, and what is deliberately not
- [CONTRIBUTING](../CONTRIBUTING.md) — writing an adapter for another assistant
