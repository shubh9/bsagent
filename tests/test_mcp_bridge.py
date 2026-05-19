from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import loop
import tools
from mcp_bridge import McpManager


def _call(name: str, call_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(arguments),
    }


def _sample_tool() -> dict[str, Any]:
    return {
        "name": "browser_navigate",
        "description": "Navigate to a URL",
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
            },
            "required": ["url"],
        },
    }


def test_static_mcp_server_registers_openai_tools() -> None:
    async def handler(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"tool": tool_name, "arguments": arguments}

    manager = McpManager()
    manager.register_static_server("playwright", [_sample_tool()], handler)

    openai_tools = manager.openai_tools()

    assert len(openai_tools) == 1
    assert openai_tools[0]["type"] == "function"
    assert openai_tools[0]["name"] == "mcp__playwright__browser_navigate"
    assert openai_tools[0]["parameters"]["properties"]["url"]["type"] == "string"


def test_dispatch_routes_mcp_tool_call(tmp_path: Path) -> None:
    async def scenario() -> None:
        async def handler(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"{tool_name}: {arguments['url']}",
                    }
                ]
            }

        manager = McpManager()
        manager.register_static_server("playwright", [_sample_tool()], handler)

        results = await tools.dispatch_tools(
            [
                _call(
                    "mcp__playwright__browser_navigate",
                    "call-1",
                    {"url": "http://localhost:3000"},
                )
            ],
            tmp_path,
            mcp_manager=manager,
        )

        payload = json.loads(results[0]["output"])
        assert results[0]["call_id"] == "call-1"
        assert payload["content"][0]["text"] == "browser_navigate: http://localhost:3000"

    asyncio.run(scenario())


def test_mcp_parallel_policy_comes_from_server_config() -> None:
    async def handler(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"tool": tool_name, "arguments": arguments}

    manager = McpManager()
    manager.register_static_server(
        "parallel",
        [_sample_tool()],
        handler,
        supports_parallel_tool_calls=True,
    )

    assert (
        tools._tool_parallelism(
            {"name": "mcp__parallel__browser_navigate"},
            mcp_manager=manager,
        )
        == "parallel"
    )


class _FakeEvent:
    type = "response.output_text.delta"
    delta = "done"


class _FakeResponse:
    output: list[Any] = []


class _FakeStream:
    def __init__(self, captured: dict[str, Any], **kwargs: Any) -> None:
        self._captured = captured
        self._kwargs = kwargs

    def __enter__(self) -> "_FakeStream":
        self._captured.update(self._kwargs)
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def __iter__(self):
        yield _FakeEvent()

    def get_final_response(self) -> _FakeResponse:
        return _FakeResponse()


class _FakeResponses:
    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    def stream(self, **kwargs: Any) -> _FakeStream:
        return _FakeStream(self._captured, **kwargs)


class _FakeClient:
    def __init__(self, captured: dict[str, Any]) -> None:
        self.responses = _FakeResponses(captured)


def test_loop_passes_mcp_tools_to_model(tmp_path: Path) -> None:
    async def handler(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"tool": tool_name, "arguments": arguments}

    manager = McpManager()
    manager.register_static_server("playwright", [_sample_tool()], handler)
    captured: dict[str, Any] = {}

    result = asyncio.run(
        loop.run_agent(
            "use the browser",
            client=_FakeClient(captured),  # type: ignore[arg-type]
            model="test-model",
            workdir=tmp_path,
            verbose=False,
            mcp_manager=manager,
        )
    )

    assert result == "done"
    tool_names = {tool["name"] for tool in captured["tools"]}
    assert "shell_command" in tool_names
    assert "mcp__playwright__browser_navigate" in tool_names
