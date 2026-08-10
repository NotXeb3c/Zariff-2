"""
Example custom tool for Zariff extensibility.

To add a new tool:
1. Create a file in tools/
2. Use @register_tool decorator
3. Import the module in tools/__init__.py
"""

from __future__ import annotations

from tools.registry import register_tool


@register_tool(
    name="echo_message",
    description="Example tool — echoes a message back (for developers)",
    parameters={"message": "Message to echo"},
)
def echo_message(params: dict) -> dict:
    return {"success": True, "message": params["message"]}
