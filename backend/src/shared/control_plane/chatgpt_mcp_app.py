"""ASGI entry for the loopback ChatGPT MCP adapter. Not imported by tests."""

from shared.control_plane.chatgpt_mcp import build_app

app = build_app()
