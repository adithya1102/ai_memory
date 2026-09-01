"""The mobile PWA: routing, the manifest, installability, offline plumbing.

Part A checks every asset is served with a sane content type, B the manifest
against the rules a browser applies before it offers "Add to Home Screen", C
the service worker's scope and headers, D that the app is genuinely
same-origin with the API it calls, E the markup's mobile affordances, and F
runs the JavaScript suites under node.

What this cannot do is open a browser.  Whether Chrome actually shows the
install prompt, and whether the worker really activates, are checked by hand
against the criteria asserted here.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from backend.core import database as db
from backend.core.importer import import_file
from backend.web.app import create_app

WORK = tempfile.mkdtemp(prefix="contextvault-pwa-")
DB = os.path.join(WORK, "pwa.db")
EXPORT = os.path.join(ROOT, "dummy_export.json.json")
PWA_DIR = os.path.join(ROOT, "pwa")

fails = []


def check(label, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + label
          + (("  -> " + str(detail)) if detail else ""))
    if not ok:
        fails.append(label)


db.init_db(DB).close()
conn = db.get_connection(DB)
import_file(conn, EXPORT)
conn.close()

app = create_app(db_path=DB)
app.config["TESTING"] = True
client = app.test_client()


# ======================================================================
print("== A. the files are served ==")

for name in ("index.html", "app.js", "style.css", "manifest.json", "sw.js"):
    check("pwa/%s exists in the repo" % name,
          os.path.exists(os.path.join(PWA_DIR, name)))

EXPECTED = [
    ("/app", 200, "text/html"),
    ("/pwa/app.js", 200, "javascript"),
    ("/pwa/style.css", 200, "text/css"),
    ("/pwa/manifest.json", 200, "json"),
    ("/pwa/lib/context.js", 200, "javascript"),
    ("/sw.js", 200, "javascript"),
    ("/manifest.webmanifest", 200, "manifest"),
    ("/static/icon.svg", 200, "svg"),
]
for path, status, content_type in EXPECTED:
    response = client.get(path)
    check("GET %s" % path, response.status_code == status,
          response.status_code)
    check("  served as %s" % content_type,
          content_type in response.headers.get("Content-Type", ""),
          response.headers.get("Content-Type"))

check("the shell is not empty", len(client.get("/app").data) > 1500)
check("a missing PWA asset is a 404, not a crash",
      client.get("/pwa/nope.js").status_code == 404)

# The library is shared with the extension rather than copied, which is what
# keeps the phone and the extension producing identical context blocks.
served_lib = client.get("/pwa/lib/context.js").data
on_disk = open(os.path.join(ROOT, "extension", "lib", "context.js"),
               "rb").read()
check("the context library is the extension's own file, not a copy",
      served_lib == on_disk)


# ======================================================================
print("\n== B. the manifest ==")

manifest = client.get("/pwa/manifest.json").get_json()

check("name is ContextVault", manifest.get("name") == "ContextVault",
      manifest.get("name"))
check("there is a short_name for the home screen",
      bool(manifest.get("short_name")), manifest.get("short_name"))
check("display is standalone", manifest.get("display") == "standalone",
      manifest.get("display"))
check("start_url points at the app", manifest.get("start_url") == "/app",
      manifest.get("start_url"))
check("scope covers the API it calls", manifest.get("scope") == "/",
      manifest.get("scope"))
check("there is a description", bool(manifest.get("description")))
check("theme_color is set", bool(manifest.get("theme_color")))
check("background_color is set", bool(manifest.get("background_color")))
check("at least one icon is declared", len(manifest.get("icons", [])) >= 1)
check("a maskable icon is offered, so Android does not letterbox it",
      any("maskable" in i.get("purpose", "")
          for i in manifest.get("icons", [])))
for icon in manifest.get("icons", []):
    check("icon %s resolves" % icon["src"],
          client.get(icon["src"]).status_code == 200)

# start_url has to be reachable, or the installed app opens on an error.
check("start_url actually loads",
      client.get(manifest["start_url"]).status_code == 200)

# One manifest for both entry points: two with the same name would show up as
# two rival install targets.
check("the server-rendered pages share this manifest",
      client.get("/manifest.webmanifest").get_json() == manifest)
check("the app links its manifest",
      b'rel="manifest"' in client.get("/app").data)


# ======================================================================
print("\n== C. the service worker ==")

sw = client.get("/sw.js")
check("it is served from the root, so its scope covers /api/v1",
      sw.status_code == 200)
check("Service-Worker-Allowed is set to /",
      sw.headers.get("Service-Worker-Allowed") == "/",
      sw.headers.get("Service-Worker-Allowed"))
check("it is not cached by the browser, or updates would never land",
      "no-cache" in (sw.headers.get("Cache-Control") or ""),
      sw.headers.get("Cache-Control"))

worker = sw.data.decode("utf-8")
check("the app shell is precached", "/pwa/app.js" in worker)
check("writes are excluded from caching",
      'request.method !== "GET"' in worker)
check("health is not in the cacheable list",
      "/api/v1/health" not in worker.split("CACHEABLE_API")[1][:300]
      if "CACHEABLE_API" in worker else False)
check("the app registers it at the root",
      'register("/sw.js")' in client.get("/pwa/app.js").data.decode("utf-8"))


# ======================================================================
print("\n== D. same origin as the API ==")

shell = client.get("/app").data.decode("utf-8")
appjs = client.get("/pwa/app.js").data.decode("utf-8")

check("the app calls the API by relative path", 'var API = "/api/v1"' in appjs)
# An absolute http://127.0.0.1:5000 would break the moment the app is opened
# from a phone over the LAN, and would make every call cross-origin.
check("no hard-coded localhost in the app",
      "127.0.0.1" not in appjs and "localhost" not in appjs)
check("no hard-coded host in the shell",
      "127.0.0.1" not in shell and "localhost" not in shell)
check("no external script or style is loaded",
      not re.search(r'(src|href)="https?://', shell))

# The endpoints the app depends on must all exist.
for path in ("/api/v1/health", "/api/v1/search?q=gym&limit=10",
             "/api/v1/memories"):
    check("the API answers %s" % path.split("?")[0],
          client.get(path).status_code == 200)

conversation_id = client.get("/api/v1/search?q=gym&limit=1"
                             ).get_json()["results"][0]["conversation_id"]
check("and serves a conversation the app can open",
      client.get("/api/v1/conversations/" + conversation_id).status_code == 200)


# ======================================================================
print("\n== E. built for a phone ==")

check("the viewport is declared with viewport-fit for notched screens",
      'name="viewport"' in shell and "viewport-fit=cover" in shell)
check("it declares itself installable on iOS",
      'name="apple-mobile-web-app-capable"' in shell)
check("a theme colour is set for the browser chrome",
      'name="theme-color"' in shell)
check("dark mode is the default", 'data-theme="dark"' in shell)

css = client.get("/pwa/style.css").data.decode("utf-8")
check("touch targets are at least 44px", "--tap: 48px" in css)
check("inputs are 16px so iOS does not zoom on focus",
      "font-size: 16px" in css)
check("safe-area insets are respected", "env(safe-area-inset" in css)
check("a light theme is provided for devices that ask for it",
      'data-theme="light"' in css)
check("reduced motion is honoured", "prefers-reduced-motion" in css)
check("it adapts to bigger screens", "@media (min-width: 720px)" in css)

for feature in ('id="q"', 'id="results"', 'id="copy-context"',
                'id="save-memory"', 'id="memories"', 'id="memory-form"'):
    check("the shell has %s" % feature, feature in shell)

check("search, conversation, memories and settings views all exist",
      all(('id="view-%s"' % v) in shell
          for v in ("search", "conversation", "memories", "settings")))
check("the last search is kept for a cold offline start",
      "contextvault.lastSearch" in appjs)
check("an offline banner exists", 'id="offline-bar"' in shell)


# ======================================================================
print("\n== F. the JavaScript suites ==")

NODE = shutil.which("node")
if not NODE:
    print("  (node not installed -- JS suites skipped)")
else:
    for name in ("test_sw.js", "test_extension.js", "test_capture.js"):
        done = subprocess.run([NODE, os.path.join(ROOT, "tests", name)],
                              capture_output=True, text=True, timeout=300,
                              encoding="utf-8")
        last = (done.stdout or "").strip().splitlines()[-1:] or ["(no output)"]
        check("%s passes" % name, done.returncode == 0, last[0])

    # Every shipped script must at least parse; a syntax error in the worker
    # would fail silently in the browser and simply never register.
    for name in ("app.js", "sw.js"):
        done = subprocess.run([NODE, "--check", os.path.join(PWA_DIR, name)],
                              capture_output=True, text=True, timeout=120,
                              encoding="utf-8")
        check("pwa/%s parses" % name, done.returncode == 0,
              (done.stderr or "").strip().splitlines()[:1])
    for name in ("content.js", "background.js", "options.js", "popup.js",
                 os.path.join("lib", "context.js"),
                 os.path.join("lib", "platforms.js"),
                 os.path.join("lib", "capture.js")):
        done = subprocess.run(
            [NODE, "--check", os.path.join(ROOT, "extension", name)],
            capture_output=True, text=True, timeout=120, encoding="utf-8")
        check("extension/%s parses" % name.replace(os.sep, "/"),
              done.returncode == 0, (done.stderr or "").strip().splitlines()[:1])

json.load(open(os.path.join(PWA_DIR, "manifest.json"), encoding="utf-8"))
check("pwa/manifest.json is valid JSON", True)


print("\n" + ("ALL CHECKS PASSED" if not fails
              else "%d FAILURES: %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)
