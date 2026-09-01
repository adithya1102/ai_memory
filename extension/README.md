# ContextVault browser extension

Two things, on the AI sites you already use:

- **`/context your question`** — press Enter and the composer fills with
  relevant excerpts from your own archive, followed by your question. Press
  Enter again to send.
- **Auto-capture** — conversations are saved to your local archive as you have
  them, batched at five new messages or thirty seconds.

Chrome, Edge, Brave, and any other Chromium browser. Manifest V3.

## What works where

| Site | `/context` | Auto-capture |
|---|---|---|
| chatgpt.com | yes | **yes** |
| claude.ai | yes | not yet |
| gemini.google.com | yes | not yet |
| chat.deepseek.com | yes | not yet |
| perplexity.ai | yes | not yet |

Capture is ChatGPT-only on purpose. `/context` needs one selector — where the
user types — and degrades to doing nothing when it misses. Capture needs a
reliable way to read every turn back out and tell who said it, which is
different markup on every site; a wrong guess silently records half a
conversation, or files the assistant's words as yours. ChatGPT tags each turn
with `data-message-author-role`, which is a solid hook. The others get an
adapter once each has been checked against its real DOM.

## Install

The extension is not on any store. Load it from this folder:

1. Start ContextVault: `python backend/main.py` (or `--no-window` for headless).
   The extension talks to its Bridge API, so the app has to be running.
2. Open `chrome://extensions`.
3. Turn on **Developer mode** (top right).
4. Click **Load unpacked** and choose this `extension/` folder.
5. The options page opens on first install. Click **Test connection** — it
   should report how many conversations are indexed.

If you enabled API key auth in **Settings → Bridge API**, paste the key into
the options page too. Leave it blank otherwise.

## Use

| You type | What happens |
|---|---|
| `/context gym` | searches for "gym", replaces the composer with the results plus your question |
| `/context` | prompts you to add a question |
| anything else | untouched — the extension only reacts to the command |

Nothing is sent until you press Enter a second time. That is deliberate:
injecting context uploads excerpts of your local archive to whichever provider
owns the tab, so you get to read it first. Turn on **Send automatically** in
the options if you would rather skip that step.

## Auto-capture

On ChatGPT, the extension watches the transcript and sends it to your local
archive. It batches: five new messages, or thirty seconds after the first
unsent one, whichever comes first. A busy conversation flows; a slow one does
not sit unsent forever.

It is **on by default** and switched off in the popup — globally, or per site.
There is also a **Capture this chat now** button in both the popup and the
in-page panel.

Some details that matter:

- **It is idempotent.** The whole visible transcript is sent every time, and
  the Bridge API deduplicates on id and content hash. Re-sending an unchanged
  conversation returns `unchanged` and creates nothing.
- **It shares ids with the official export.** A captured thread uses the
  conversation id from the URL, which is the same id ChatGPT's export uses, so
  capturing now and importing the export later produce one record, not two.
- **It will not truncate.** Because of that shared id, a half-rendered page
  could otherwise replace a complete import with a shorter version — `/ingest`
  replaces messages wholesale. So before sending, the extension asks how many
  messages the archive already holds and skips the write if the page is
  showing fewer.
- **Nothing is captured without a conversation id.** A brand-new unsaved
  thread has no stable identity yet; filing it would duplicate the moment the
  real id appears in the URL.
- **A failed send is not lost.** The batch stays pending and the next tick
  retries.

## How it works

```
composer keydown ──> content.js ──chrome.runtime──> background.js
   DOM mutations ──>     │                              │
                         │                       fetch 127.0.0.1
                         │                              │
                         └───── results / ingest ◄──────┘
```

`content.js` owns the page: the `/context` keystroke, the MutationObserver
that watches for new turns, the floating panel, and the launcher button.
`background.js` makes every HTTP call. The DOM-free logic lives in `lib/` and
is covered by tests that run under node:

| File | What it holds | Tested by |
|---|---|---|
| `lib/context.js` | command parsing, URL building, block formatting | `tests/test_extension.js` |
| `lib/platforms.js` | site detection, selectors, transcript extraction | `tests/test_capture.js` |
| `lib/capture.js` | the batching rules | `tests/test_capture.js` |

**Why the fetch happens in the service worker.** A page-context request from
chatgpt.com to `127.0.0.1:5000` is cross-origin, and the Bridge API does not
send permissive CORS headers. That is on purpose: an
`Access-Control-Allow-Origin: *` on a local API holding your whole
conversation archive would let *any* site you visit read it. The service
worker is not bound by the page's origin, so the extension works without the
API ever opening that door.

## Site selectors

Each site's composer is found by a list of selectors with a generic fallback:

| Site | Primary selector |
|---|---|
| chatgpt.com | `#prompt-textarea` |
| claude.ai | `div.ProseMirror[contenteditable='true']` |
| gemini.google.com | `div.ql-editor[contenteditable='true']` |

These belong to someone else's app and **will** break when it is redesigned.
When one does, the command stops doing anything — it never leaves a broken
composer behind. Fixing it means adding the new selector to `SITES` in
`content.js`; the fallback to "whatever element has focus" already covers many
redesigns on its own.

## Privacy

- The extension talks to the address in its options and nowhere else.
- It only reads the composer, and only when you press Enter on a `/context`
  line.
- It has no analytics and makes no other network requests.
- `credentials: "omit"` on every call, so no site cookie is ever attached to a
  request to your archive.

The one thing it does do is put excerpts of your private history into a text
box belonging to OpenAI, Anthropic or Google. That is the entire point of the
feature, but it is worth being clear-eyed about: whatever you inject leaves
your machine when you send it.

Auto-capture runs the other way — it reads a page you are already on and
writes to your own machine. Nothing is uploaded anywhere. But it does mean
conversations land in your archive without you asking each time, which is
worth knowing if you share a browser profile. The popup's top switch turns it
off entirely, and per-site switches sit underneath it.

## Mobile

Extensions do not run in mobile browsers. Open `/context` on the ContextVault
app instead, search there, and copy the block across. Add it to your home
screen and it behaves like an app.
