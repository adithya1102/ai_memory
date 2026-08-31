#!/usr/bin/env python3
"""Launcher for the AI Memory MCP server.

This is the file to point an MCP client at.  Claude Desktop, Cursor and
friends spawn it as a subprocess and speak JSON-RPC over stdin/stdout:

    python /path/to/ai_memory/mcp_server.py

See the "Connecting Claude Desktop" section of README.md for the config JSON.

It lives at the repository root so the path in that config stays short, and it
does nothing except make the repository importable before handing over to
backend/mcp/server.py.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from backend.mcp.server import main

if __name__ == "__main__":
    main()
