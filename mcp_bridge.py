"""
Small MCP bridge for bsagent.

The core agent loop speaks OpenAI Responses function tools. MCP servers expose
their own tools through tools/list and tools/call. This module adapts MCP tools
into Responses-compatible function schemas and routes calls back to the owning
server.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable


MCP_TOOL_PREFIX = "mcp__"


@dataclass(frozen=True)
class McpServerSpec:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: Path | None = None
    enabled: bool = True
    required: bool = False
    startup_timeout_sec: float = 20
    tool_timeout_sec: float = 120
    supports_parallel_tool_calls: bool = False


@dataclass
class McpServerState:
    spec: McpServerSpec
    session: Any
    tools: list[dict[str, Any]]


ToolHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


def default_mcp_server_specs(workdir: Path) -> list[McpServerSpec]:
    """Built-in MCP servers. Add future first-party MCPs here."""
    if _env_disabled("BSAGENT_ENABLE_PLAYWRIGHT_MCP", default_enabled=True):
        return []

    return [
        McpServerSpec(
            name="playwright",
            command="npx",
            args=[
                "-y",
                "@playwright/mcp@latest",
                "--isolated",
                "--caps=devtools",
                "--output-dir",
                str(workdir / ".agent-browser-artifacts"),
            ],
            cwd=workdir,
            required=False,
            startup_timeout_sec=30,
            tool_timeout_sec=120,
        )
    ]


def _env_disabled(name: str, *, default_enabled: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return not default_enabled
    return raw.strip().lower() in {"0", "false", "no", "off", "disabled"}


class McpManager:
    def __init__(self, specs: list[McpServerSpec] | None = None) -> None:
        self._specs = specs or []
        self._exit_stack = AsyncExitStack()
        self._servers: dict[str, McpServerState] = {}
        self._tool_routes: dict[str, tuple[str, str]] = {}
        self._static_handlers: dict[str, ToolHandler] = {}
        self._startup_errors: dict[str, str] = {}

    @property
    def startup_errors(self) -> dict[str, str]:
        return dict(self._startup_errors)

    async def start(self, workdir: Path) -> list[str]:
        """
        Start enabled MCP servers and register their tools.

        Returns human-readable warning strings for non-required server failures.
        Required server failures are raised.
        """
        warnings: list[str] = []
        for spec in self._specs:
            if not spec.enabled:
                continue
            resolved_spec = _resolve_spec(spec, workdir)
            try:
                await asyncio.wait_for(
                    self._start_stdio_server(resolved_spec),
                    timeout=resolved_spec.startup_timeout_sec,
                )
            except BaseException as exc:
                message = f"{resolved_spec.name}: {exc}"
                self._startup_errors[resolved_spec.name] = message
                if resolved_spec.required:
                    raise RuntimeError(f"required MCP server failed to start: {message}") from exc
                warnings.append(f"MCP server unavailable ({message})")
        return warnings

    async def aclose(self) -> None:
        await self._exit_stack.aclose()
        self._servers.clear()
        self._tool_routes.clear()
        self._static_handlers.clear()

    def openai_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        for state in self._servers.values():
            tools.extend(state.tools)
        return tools

    def is_mcp_tool(self, name: str) -> bool:
        return name in self._tool_routes

    def tool_supports_parallel(self, name: str) -> bool:
        route = self._tool_routes.get(name)
        if route is None:
            return False
        server_name, _ = route
        state = self._servers.get(server_name)
        return bool(state and state.spec.supports_parallel_tool_calls)

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        route = self._tool_routes.get(name)
        if route is None:
            raise KeyError(f"unknown MCP tool: {name}")

        server_name, tool_name = route
        if server_name in self._static_handlers:
            result = await self._static_handlers[server_name](tool_name, arguments)
        else:
            state = self._servers[server_name]
            result = await asyncio.wait_for(
                state.session.call_tool(tool_name, arguments),
                timeout=state.spec.tool_timeout_sec,
            )
        return _serialize_mcp_result(result)

    def register_static_server(
        self,
        server_name: str,
        tools: list[dict[str, Any]],
        handler: ToolHandler,
        *,
        supports_parallel_tool_calls: bool = False,
    ) -> None:
        """
        Register an in-process test or synthetic MCP server.

        This keeps tests independent from a real MCP subprocess while exercising
        the same OpenAI tool conversion and routing code.
        """
        spec = McpServerSpec(
            name=server_name,
            command="<static>",
            supports_parallel_tool_calls=supports_parallel_tool_calls,
        )
        openai_tools = [
            self._register_tool(server_name, _tool_name(tool), tool, spec)
            for tool in tools
        ]
        self._servers[server_name] = McpServerState(
            spec=spec,
            session=None,
            tools=openai_tools,
        )
        self._static_handlers[server_name] = handler

    async def _start_stdio_server(self, spec: McpServerSpec) -> None:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise RuntimeError(
                "Python package 'mcp' is not installed; run pip install -r requirements.txt"
            ) from exc

        env = os.environ.copy()
        env.update(spec.env)
        params = StdioServerParameters(
            command=spec.command,
            args=spec.args,
            env=env,
            cwd=str(spec.cwd) if spec.cwd is not None else None,
        )

        read, write = await self._exit_stack.enter_async_context(stdio_client(params))
        session = await self._exit_stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        listed = await session.list_tools()

        raw_tools = list(getattr(listed, "tools", []) or [])
        openai_tools = [
            self._register_tool(spec.name, _tool_name(tool), tool, spec)
            for tool in raw_tools
        ]
        self._servers[spec.name] = McpServerState(
            spec=spec,
            session=session,
            tools=openai_tools,
        )

    def _register_tool(
        self,
        server_name: str,
        tool_name: str,
        tool: Any,
        spec: McpServerSpec,
    ) -> dict[str, Any]:
        openai_name = _unique_tool_name(
            self._tool_routes,
            f"{MCP_TOOL_PREFIX}{_safe_name(server_name)}__{_safe_name(tool_name)}",
        )
        self._tool_routes[openai_name] = (server_name, tool_name)

        parameters = _tool_input_schema(tool)
        description = _tool_description(tool) or f"MCP tool {server_name}/{tool_name}"
        return {
            "type": "function",
            "name": openai_name,
            "description": description,
            "parameters": parameters,
        }


def _resolve_spec(spec: McpServerSpec, workdir: Path) -> McpServerSpec:
    cwd = spec.cwd or workdir
    return McpServerSpec(
        name=spec.name,
        command=spec.command,
        args=spec.args,
        env=spec.env,
        cwd=cwd,
        enabled=spec.enabled,
        required=spec.required,
        startup_timeout_sec=spec.startup_timeout_sec,
        tool_timeout_sec=spec.tool_timeout_sec,
        supports_parallel_tool_calls=spec.supports_parallel_tool_calls,
    )


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("name", ""))
    return str(getattr(tool, "name", ""))


def _tool_description(tool: Any) -> str:
    if isinstance(tool, dict):
        return str(tool.get("description") or "")
    return str(getattr(tool, "description", "") or "")


def _tool_input_schema(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
    else:
        schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    if hasattr(schema, "model_dump"):
        schema = schema.model_dump()
    if not isinstance(schema, dict):
        schema = {}
    schema = dict(schema)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def _safe_name(value: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", value)
    safe = safe.strip("_")
    return safe or "tool"


def _unique_tool_name(
    routes: dict[str, tuple[str, str]],
    candidate: str,
) -> str:
    if candidate not in routes:
        return candidate
    suffix = 2
    while f"{candidate}_{suffix}" in routes:
        suffix += 1
    return f"{candidate}_{suffix}"


def _serialize_mcp_result(result: Any) -> str:
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json")
    elif isinstance(result, dict):
        payload = result
    else:
        payload = {"content": str(result)}
    return json.dumps(payload, default=str)
