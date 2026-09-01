# ContextVault browser extension

Type `/context your question` in ChatGPT, Claude or Gemini, press Enter, and
the composer is replaced with relevant excerpts from your own archive followed
by your question. Press Enter again to send it.

Chrome, Edge, Brave, and any other Chromium browser. Manifest V3.

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

## How it works

```
composer keydown ──> content.js ──chrome.runtime──> background.js
                          │                              │
                          │                       fetch 127.0.0.1
                          │                              │
                          └────── context block ◄────────┘
```

`content.js` watches for Enter on a line starting with `/context` and cancels
the send. `background.js` does the HTTP call. `lib/context.js` holds the parts
with no DOM in them — command parsing, URL building, block formatting — and is
covered by `tests/test_extension.js`.

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

## Mobile

Extensions do not run in mobile browsers. Open `/context` on the ContextVault
app instead, search there, and copy the block across. Add it to your home
screen and it behaves like an app.
