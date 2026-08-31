"""Flask application for the AI Memory desktop UI."""

import os
import re
import sys
from datetime import datetime

# Allow `python backend/web/app.py` as well as `python backend/main.py`.
PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, url_for)
from werkzeug.utils import secure_filename

from backend.core import database as db
from backend.core import embeddings
from backend.core.importer import detect_chains, import_file
from backend.core.search import SEMANTIC_ENABLED_KEY, hybrid_search
from backend.mcp import tools as mcp_tools

# Off by default: the TCP transport listens on a socket that serves the whole
# conversation archive, so turning it on is the user's decision to make.
MCP_ENABLED_KEY = "mcp_server_enabled"
MCP_PORT_KEY = "mcp_server_port"


def create_app(db_path=db.DB_PATH, imports_dir=db.IMPORTS_DIR):
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path
    app.config["IMPORTS_DIR"] = imports_dir
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # exports get big
    app.secret_key = os.urandom(24)  # local app: flash messages only

    # ------------------------------------------------------------------
    # Per-request database connection
    # ------------------------------------------------------------------
    def connection():
        if "db" not in g:
            g.db = db.get_connection(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_connection(_exception):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    # ------------------------------------------------------------------
    # Template helpers
    # ------------------------------------------------------------------
    @app.template_filter("datefmt")
    def datefmt(value, fmt="%d %b %Y"):
        if not value:
            return "—"
        try:
            return datetime.fromisoformat(str(value)).strftime(fmt)
        except ValueError:
            return str(value)[:10]

    @app.template_filter("preview")
    def preview(value, length=220):
        text = re.sub(r"\s+", " ", value or "").strip()
        return text if len(text) <= length else text[:length].rstrip() + "…"

    # ------------------------------------------------------------------
    # Routes
    # ------------------------------------------------------------------
    @app.route("/")
    def index():
        conn = connection()
        return render_template(
            "index.html",
            recent=db.get_recent_conversations(conn, limit=10),
            chains=db.get_chains(conn)[:5],
            stats=db.get_stats(conn),
        )

    @app.route("/search")
    def search():
        conn = connection()
        query = (request.args.get("q") or "").strip()
        if not query:
            return redirect(url_for("index"))
        semantic_on = db.get_flag(conn, SEMANTIC_ENABLED_KEY, default=True)
        results = hybrid_search(conn, query, limit=50)
        state = embeddings.model_state()
        indexed = embeddings.has_embeddings(conn)
        return render_template(
            "results.html", query=query, results=results,
            semantic_on=semantic_on,
            semantic_used=any("semantic" in r["match_types"] for r in results),
            has_embeddings=indexed,
            model_state=state,
            # Only worth telling the user the model is warming up when it would
            # otherwise have contributed results.
            model_loading=(semantic_on and indexed
                           and state["state"] in ("idle", "loading")),
        )

    @app.route("/api/model-status")
    def model_status():
        return jsonify(embeddings.model_state())

    @app.route("/conversation/<path:conversation_id>")
    def conversation(conversation_id):
        conn = connection()
        record = db.get_conversation(conn, conversation_id)
        if record is None:
            abort(404)
        return render_template(
            "conversation.html",
            conversation=record,
            messages=db.get_messages(conn, conversation_id),
            chains=db.get_chains_for_conversation(conn, conversation_id),
            query=(request.args.get("q") or "").strip(),
        )

    @app.route("/chains")
    def chains():
        return render_template("chain.html", chain=None,
                               chains=db.get_chains(connection()))

    @app.route("/chain/<int:chain_id>")
    def chain(chain_id):
        record = db.get_chain(connection(), chain_id)
        if record is None:
            abort(404)
        return render_template("chain.html", chain=record, chains=None)

    @app.route("/settings")
    def settings():
        conn = connection()
        return render_template(
            "settings.html",
            stats=db.get_stats(conn),
            db_path=os.path.abspath(app.config["DB_PATH"]),
            imports_dir=os.path.abspath(app.config["IMPORTS_DIR"]),
            db_size=_file_size(app.config["DB_PATH"]),
            semantic_on=db.get_flag(conn, SEMANTIC_ENABLED_KEY, default=True),
            embedding=embeddings.embedding_stats(conn),
            mcp=_mcp_status(conn),
        )

    def _mcp_status(conn):
        from backend.mcp import server as mcp_server

        status = mcp_server.BACKGROUND.status()
        status["enabled"] = db.get_flag(conn, MCP_ENABLED_KEY, default=False)
        status["launcher"] = os.path.join(PROJECT_ROOT, "mcp_server.py")
        status["python"] = sys.executable
        status["tools"] = [t["name"] for t in mcp_tools.TOOL_SCHEMAS]
        return status

    @app.route("/settings/semantic", methods=["POST"])
    def toggle_semantic():
        conn = connection()
        enabled = request.form.get("enabled") == "1"
        db.set_flag(conn, SEMANTIC_ENABLED_KEY, enabled)
        flash("Semantic search %s." % ("enabled" if enabled else "disabled"),
              "success")
        return redirect(url_for("settings"))

    @app.route("/settings/mcp", methods=["POST"])
    def toggle_mcp():
        from backend.mcp import server as mcp_server

        conn = connection()
        enabled = request.form.get("enabled") == "1"
        db.set_flag(conn, MCP_ENABLED_KEY, enabled)
        try:
            if enabled:
                port = mcp_server.find_free_port(
                    int(db.get_setting(conn, MCP_PORT_KEY,
                                       mcp_server.DEFAULT_TCP_PORT)))
                status = mcp_server.BACKGROUND.start(
                    app.config["DB_PATH"], port=port)
                db.set_setting(conn, MCP_PORT_KEY, status["port"])
                flash("MCP server listening on 127.0.0.1:%d. Claude Desktop "
                      "uses the stdio launcher instead — see Settings below."
                      % status["port"], "success")
            else:
                mcp_server.BACKGROUND.stop()
                flash("MCP server stopped.", "success")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            db.set_flag(conn, MCP_ENABLED_KEY, False)
            flash("Could not start the MCP server: %s" % exc, "error")
        return redirect(url_for("settings"))

    @app.route("/embeddings/rebuild", methods=["POST"])
    def rebuild_embeddings():
        conn = connection()
        try:
            result = embeddings.sync_embeddings(conn)
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            flash("Embedding failed: %s" % exc, "error")
            return redirect(url_for("settings"))
        if result["skipped"]:
            flash("Semantic search unavailable: %s" % result["skipped"], "error")
        elif result["conversations"] == 0:
            flash("Semantic index is already up to date.", "success")
        else:
            flash("Embedded %d conversation%s into %d chunks."
                  % (result["conversations"],
                     "" if result["conversations"] == 1 else "s",
                     result["chunks"]), "success")
        return redirect(url_for("settings"))

    @app.route("/import", methods=["GET", "POST"])
    def import_export():
        if request.method == "GET":
            return render_template("import.html")

        source_path = None
        upload = request.files.get("file")
        local_path = (request.form.get("path") or "").strip().strip('"')

        if upload and upload.filename:
            os.makedirs(app.config["IMPORTS_DIR"], exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            filename = secure_filename(upload.filename) or "conversations.json"
            source_path = os.path.join(app.config["IMPORTS_DIR"],
                                       "%s_%s" % (stamp, filename))
            upload.save(source_path)
        elif local_path:
            if not os.path.isfile(local_path):
                flash("No file found at %s" % local_path, "error")
                return redirect(url_for("import_export"))
            source_path = local_path
        else:
            flash("Choose a conversations.json file to import.", "error")
            return redirect(url_for("import_export"))

        try:
            stats = import_file(connection(), source_path)   # provider sniffed
        except Exception as exc:  # noqa: BLE001 - surfaced to the user
            flash("Import failed: %s" % exc, "error")
            return redirect(url_for("import_export"))

        flash(
            "Imported %d new, %d updated, %d unchanged, %d duplicate "
            "(%d messages, %d skipped) — detected %s, %d chains, "
            "%d conversations embedded."
            % (stats["inserted"], stats["updated"], stats["unchanged"],
               stats.get("duplicate", 0), stats["messages"], stats["skipped"],
               stats.get("provider", "chatgpt"), stats["chains"],
               stats.get("embedded", 0)),
            "success",
        )
        if stats.get("embedding_note"):
            flash("Semantic indexing: %s" % stats["embedding_note"], "error")
        return redirect(url_for("index"))

    @app.route("/rebuild-chains", methods=["POST"])
    def rebuild_chains():
        count = detect_chains(connection())
        flash("Rebuilt chains: %d found." % count, "success")
        return redirect(url_for("settings"))

    return app


def _file_size(path):
    try:
        size = os.path.getsize(path)
    except OSError:
        return "—"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit)
        size /= 1024.0


if __name__ == "__main__":
    db.init_db(db.DB_PATH).close()
    create_app().run(host="127.0.0.1", port=5000, debug=True, threaded=True)
