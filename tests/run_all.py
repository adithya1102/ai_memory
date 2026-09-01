"""Run every ContextVault test suite and report the totals.

    python tests/run_all.py

Each suite is a plain script with no test-framework dependency, run in its own
process so a crash in one cannot take the others down.  All of them work
against temporary databases and never touch ``data/``.

Suites that need the semantic stack (sentence-transformers, sqlite-vec) are
skipped with a note when it is not installed, rather than failing.
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SUITES = [
    ("test_core.py", "import, FTS search, chains, routes", False),
    ("test_dedup_and_chains.py", "deduplication and chain detection", False),
    ("test_providers.py", "Claude/Gemini adapters, provider sniffing", True),
    ("test_semantic.py", "chunking, embeddings, hybrid ranking", True),
    ("test_preload.py", "background model loading and fallback", True),
    ("test_mcp.py", "MCP tools, stdio protocol, TCP transport", True),
]

NOISE = re.compile(r"it/s\]|HF Hub|Loading weights|^\[transformers\]")


def semantic_stack_available():
    sys.path.insert(0, ROOT)
    try:
        from backend.core import embeddings
        return embeddings.availability()
    except Exception as exc:  # pragma: no cover
        return False, str(exc)


def main():
    ok, reason = semantic_stack_available()
    total = failed = 0
    results = []

    for name, description, needs_model in SUITES:
        if needs_model and not ok:
            results.append((name, None, "skipped: %s" % reason))
            continue

        print("=" * 68)
        print("%s  -- %s" % (name, description))
        print("=" * 68)
        proc = subprocess.run([sys.executable, os.path.join("tests", name)],
                              cwd=ROOT, capture_output=True, text=True)
        output = "\n".join(line for line in (proc.stdout or "").splitlines()
                           if not NOISE.search(line))
        print(output)
        if proc.returncode != 0 and proc.stderr:
            print(proc.stderr[-2000:], file=sys.stderr)

        passed = len(re.findall(r"^  ok ", output, re.M))
        total += passed
        if proc.returncode != 0:
            failed += 1
        results.append((name, passed, "PASS" if proc.returncode == 0 else "FAIL"))
        print()

    print("=" * 68)
    for name, passed, status in results:
        print("  %-28s %s  %s"
              % (name, ("%3d checks" % passed) if passed is not None else "  skipped ",
                 status))
    print("=" * 68)
    print("  %d checks passed, %d suite(s) failing" % (total, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
