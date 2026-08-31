"""AI Memory desktop entry point.

Starts the Flask server on a background thread and opens a pywebview window
pointing at it.  Use --no-window to run as a plain local web app instead.
"""

import argparse
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.core import database as db
from backend.web.app import create_app

HOST = "127.0.0.1"


def free_port(preferred=5000):
    """Use the preferred port if it is free, otherwise let the OS pick one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((HOST, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def wait_until_up(url, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except urllib.error.HTTPError:
            return True  # server answered, even if with an error status
        except OSError:
            time.sleep(0.15)
    return False


def preload_model(db_path):
    """Start loading the embedding model in the background.

    The encoder takes several seconds to load, so doing it at launch keeps that
    cost off the first search.  It runs on a daemon thread and never blocks
    startup: if it is still loading when a search arrives, the search returns
    keyword results and the page retries once the model is ready.

    Skipped when semantic search is switched off, so a user who does not want
    it pays neither the time nor the memory.
    """
    from backend.core import embeddings
    from backend.core.search import SEMANTIC_ENABLED_KEY

    try:
        conn = db.get_connection(db_path)
        try:
            enabled = db.get_flag(conn, SEMANTIC_ENABLED_KEY, default=True)
        finally:
            conn.close()
        if not enabled:
            return None
        return embeddings.start_preload()
    except Exception:
        return None  # never let preloading stop the app from starting


def main():
    parser = argparse.ArgumentParser(description="AI Memory")
    parser.add_argument("--no-window", action="store_true",
                        help="run the web server only, without a desktop window")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--db", default=db.DB_PATH, help="path to the SQLite file")
    args = parser.parse_args()

    os.makedirs(db.DATA_DIR, exist_ok=True)
    os.makedirs(db.IMPORTS_DIR, exist_ok=True)
    db.init_db(args.db).close()

    preload_model(args.db)
    app = create_app(db_path=args.db)
    port = free_port(args.port)
    url = "http://%s:%d" % (HOST, port)

    def serve():
        app.run(host=HOST, port=port, debug=False, threaded=True,
                use_reloader=False)

    if args.no_window:
        print("AI Memory running at %s  (Ctrl+C to stop)" % url)
        serve()
        return

    try:
        import webview
    except ImportError:
        print("pywebview is not installed - falling back to the browser.\n"
              "  pip install -r requirements.txt\n")
        print("AI Memory running at %s  (Ctrl+C to stop)" % url)
        serve()
        return

    threading.Thread(target=serve, daemon=True).start()
    if not wait_until_up(url):
        print("The server did not start in time; try --no-window to see errors.")
        return

    webview.create_window("AI Memory", url, width=1024, height=768,
                          min_size=(640, 480))
    webview.start()


if __name__ == "__main__":
    main()
