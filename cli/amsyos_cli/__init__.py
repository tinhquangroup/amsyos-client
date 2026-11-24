"""The `amsyos` command (spec v3 §3.2): AMSYOS from a terminal or an AI coding agent.

It talks to AMSYOS only through the six MCP tools on `<gateway>/mcp`, with the token of a
session (device login, spec v3 §4.2), and never prints a token, a file's content or a
signed URL unless asked (`--show-urls`).
"""

__version__ = "0.5.5"
