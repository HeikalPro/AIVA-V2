"""Dispatch tool calls to callables."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from llm_service.tools.schema import ToolCall, ToolResult


class ToolExecutor:
    def __init__(self, tools: dict[str, Callable[..., Any]]) -> None:
        self._tools = tools

    async def execute(self, tool_call: ToolCall) -> ToolResult:
        fn = self._tools.get(tool_call.name)
        if fn is None:
            return ToolResult(tool_call_id=tool_call.id, content="Tool not found", is_error=True)
        try:
            if inspect.iscoroutinefunction(fn):
                result = await fn(**tool_call.arguments)
            else:
                result = await asyncio.to_thread(fn, **tool_call.arguments)
            return ToolResult(tool_call_id=tool_call.id, content=str(result))
        except Exception as e:  # pragma: no cover - user tools
            return ToolResult(tool_call_id=tool_call.id, content=str(e), is_error=True)
