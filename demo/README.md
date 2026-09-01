# ContextVault demo page

A single page that shows what ContextVault does, with no setup at all. No
install, no login, no backend, no build step. Three files and some invented
conversations.

Point a tester at it and they can try it in about ten seconds.

## Try it locally

Double-click `index.html`. That is the whole procedure.

It works straight from disk because the demo data is embedded in the page as
well as living in `data.json`. A page opened over `file://` is not allowed to
`fetch()` a local file, so a demo that only read `data.json` would show an
empty box to exactly the person you most wanted to impress.

If you prefer to serve it:

```bash
cd demo
python -m http.server 8000
# then open http://localhost:8000
```

## Deploy to GitHub Pages

**From the repository, no extra tooling:**

1. Push this folder to the default branch.
2. In the repository, go to **Settings → Pages**.
3. Under **Source**, choose **Deploy from a branch**.
4. Pick your branch (`master`) and the folder **`/ (root)`**, then **Save**.
5. Wait for the green tick on the Pages settings screen — the first build
   takes a couple of minutes.

The demo is then at:

```
https://<your-username>.github.io/<repository>/demo/
```

For `adithya1102/contextvault` that is
`https://adithya1102.github.io/contextvault/demo/`.

**Note the trailing slash.** Without it GitHub Pages may look for a file named
`demo` rather than the directory.

If you would rather the demo sit at the site root, move these files up one
level or publish from a `gh-pages` branch containing only this folder.

## Deploy to Netlify Drop

No account needed for a temporary link, and about thirty seconds:

1. Open <https://app.netlify.com/drop>.
2. Drag the `demo` folder onto the page.
3. You get a URL like `https://random-name-123.netlify.app` immediately.

The link is live but unclaimed. Sign in within 24 hours if you want to keep it,
rename it, or attach a custom domain.

Netlify serves the folder root, so the demo is at the bare URL rather than
under `/demo/`.

## Files

| File | What it is |
|---|---|
| `index.html` | the page — markup, styles and UI logic, no dependencies |
| `search.js` | the search itself, with no DOM in it |
| `data.json` | the canonical demo conversations |
| `README.md` | this file |

`data.json` is the source of truth for the demo data, and an identical copy is
embedded in `index.html` so the file-open case works. `tests/test_demo.js`
asserts the two are the same, so editing one without the other fails the
suite rather than quietly shipping a stale page.

To change the data, edit `data.json`, then re-embed it:

```bash
python - <<'EOF'
import io, json, re
raw = io.open("demo/data.json", encoding="utf-8").read()
json.loads(raw)
html = io.open("demo/index.html", encoding="utf-8").read()
html = re.sub(r'(<script type="application/json" id="demo-data">\n).*?(\n</script>)',
              lambda m: m.group(1) + raw.rstrip("\n") + m.group(2),
              html, flags=re.S)
io.open("demo/index.html", "w", encoding="utf-8").write(html)
EOF
```

## What this demo is and is not

**It is** a faithful illustration of content search: every conversation shown
is searched by its full text, not just its title. The conversation called
*Welcome* answers a search for `gym` because those words are in the messages,
which is the thing a provider's own sidebar cannot do.

**It is not** the real search engine. The actual app runs two engines — SQLite
FTS5 for words and local embeddings for meaning — and fuses the rankings.
Embeddings need a ~90 MB model and a vector index, which do not belong in a
page that has to open instantly with no setup. So the demo does keyword
matching honestly and does not pretend the results are semantic. Match badges
say `title`, `content` or `title + content`, never `semantic`.

**The data is invented.** Every conversation, name and date in `data.json` is
synthetic. Nothing here came from a real archive.

**The feedback buttons do not transmit anything.** The answer is written to
`localStorage` and the page then offers a link to open a GitHub issue. There
is no backend to collect it, and the page says so rather than implying a
response was recorded somewhere.
