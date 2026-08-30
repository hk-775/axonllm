#!/usr/bin/env python3
"""Run the supported Codex CLI against AxonLLM's real Responses adapter.

The loopback server records Codex's request, passes successful requests through
``OpenAICompatAPI.responses``, and supplies a deterministic fake provider behind
the adapter. The gate covers a streamed function-call round trip, OpenAI-shaped
errors, and cancellation without requiring provider credentials or network
access.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.gateway.chat.openai_routes import OpenAICompatAPI


SUPPORTED_CODEX_VERSION = "0.150.1"
MODEL = "claude-sonnet"
MODEL_CATALOG = ROOT / "config" / "codex" / "model_catalog.json"
TOKEN = "axonllm-codex-conformance-token"
SUCCESS_PROMPT = "AXONLLM_CONFORMANCE_SUCCESS"
ERROR_PROMPT = "AXONLLM_CONFORMANCE_ERROR"
CANCEL_PROMPT = "AXONLLM_CONFORMANCE_CANCEL"
FINAL_TEXT = "AXONLLM_CODEX_CONFORMANCE_OK"
ERROR_TEXT = "axonllm conformance error"


def _command_value(schema: Any) -> str | list[str]:
    if isinstance(schema, dict) and schema.get("type") == "array":
        return ["/bin/sh", "-lc", "printf AXONLLM_TOOL_OK"]
    return "printf AXONLLM_TOOL_OK"


def _minimal_value(schema: Any) -> Any:
    if not isinstance(schema, dict):
        return None
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    value_type = schema.get("type")
    if value_type == "string":
        return ""
    if value_type == "integer":
        return 1000
    if value_type == "number":
        return 1
    if value_type == "boolean":
        return False
    if value_type == "array":
        return []
    if value_type == "object":
        return {}
    return None


def _safe_tool_call(tools: Any) -> tuple[str, dict[str, Any]]:
    """Choose Codex's shell tool and build its smallest valid invocation."""
    if not isinstance(tools, list):
        raise AssertionError("Codex request did not include function tools")
    functions = [
        tool.get("function")
        for tool in tools
        if isinstance(tool, dict)
        and tool.get("type") == "function"
        and isinstance(tool.get("function"), dict)
    ]
    preferred = next(
        (
            tool
            for tool in functions
            if tool.get("name") in {"exec_command", "shell_command"}
        ),
        None,
    )
    if preferred is None:
        raise AssertionError("Codex request did not expose a safe shell tool")

    name = str(preferred["name"])
    parameters = preferred.get("parameters")
    properties = (
        parameters.get("properties", {})
        if isinstance(parameters, dict)
        else {}
    )
    required = (
        parameters.get("required", [])
        if isinstance(parameters, dict)
        else []
    )
    command_key = "cmd" if name == "exec_command" else "command"
    arguments: dict[str, Any] = {}
    for key in required:
        schema = properties.get(key, {})
        arguments[key] = (
            _command_value(schema)
            if key == command_key
            else _minimal_value(schema)
        )
    arguments.setdefault(
        command_key,
        _command_value(properties.get(command_key, {})),
    )
    return name, arguments


class _FakeClientAgent:
    """Return deterministic streamed provider output behind AxonLLM."""

    async def chat(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Codex conformance must use streamed Responses")

    async def chat_stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ):
        has_tool_output = any(
            message.get("role") == "tool"
            for message in messages
            if isinstance(message, dict)
        )
        if has_tool_output:
            yield {
                "id": "axonllm-conformance",
                "model": model,
                "content": FINAL_TEXT,
                "finish_reason": "stop",
                "is_final": True,
            }
            yield {"done": True}
            return

        tool_name, arguments = _safe_tool_call(kwargs.get("tools"))
        yield {
            "id": "axonllm-conformance",
            "model": model,
            "content": "",
            "tool_calls": [
                {
                    "id": "call_axonllm_conformance",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(
                            arguments,
                            separators=(",", ":"),
                        ),
                    },
                }
            ],
            "finish_reason": "tool_calls",
            "is_final": True,
        }
        yield {"done": True}


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.cancel_started = threading.Event()

    def record(self, body: dict[str, Any]) -> None:
        with self.lock:
            self.requests.append(body)

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return list(self.requests)


def _request_marker(body: dict[str, Any]) -> str:
    encoded = json.dumps(body.get("input"), ensure_ascii=False)
    for marker in (SUCCESS_PROMPT, ERROR_PROMPT, CANCEL_PROMPT):
        if marker in encoded:
            return marker
    return ""


def _make_handler(
    state: _State,
    api: OpenAICompatAPI,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def do_POST(self) -> None:
            if self.path != "/v1/responses":
                self.send_error(404)
                return
            if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                self._json_error(401, "missing conformance bearer token")
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(content_length))
            except (TypeError, ValueError, json.JSONDecodeError):
                self._json_error(400, "malformed conformance request")
                return
            if not isinstance(body, dict):
                self._json_error(400, "request body must be an object")
                return
            state.record(body)

            marker = _request_marker(body)
            if marker == ERROR_PROMPT:
                self._json_error(400, ERROR_TEXT)
                return
            if marker == CANCEL_PROMPT:
                self._hold_stream(body)
                return
            if marker != SUCCESS_PROMPT:
                self._json_error(400, "missing conformance marker")
                return
            self._adapter_response(body)

        def _adapter_response(self, body: dict[str, Any]) -> None:
            import asyncio

            scope = {
                "type": "http",
                "method": "POST",
                "path": "/v1/responses",
                "headers": [],
                "query_string": b"",
                "server": ("127.0.0.1", self.server.server_port),
                "scheme": "http",
            }
            encoded = json.dumps(body).encode()
            sent = False

            async def receive() -> dict[str, Any]:
                nonlocal sent
                if sent:
                    return {"type": "http.disconnect"}
                sent = True
                return {
                    "type": "http.request",
                    "body": encoded,
                    "more_body": False,
                }

            request = Request(scope, receive)
            response = asyncio.run(api.responses(request))
            self.send_response(response.status_code)
            for key, value in response.headers.items():
                if key.lower() not in {"content-length", "connection"}:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()

            if hasattr(response, "body_iterator"):
                async def collect() -> bytes:
                    chunks: list[bytes] = []
                    async for chunk in response.body_iterator:
                        chunks.append(
                            chunk.encode() if isinstance(chunk, str) else chunk
                        )
                    return b"".join(chunks)

                self.wfile.write(asyncio.run(collect()))
            else:
                self.wfile.write(response.body)

        def _hold_stream(self, body: dict[str, Any]) -> None:
            response_id = "resp_axonllm_cancel"
            initial = {
                "id": response_id,
                "object": "response",
                "created_at": int(time.time()),
                "completed_at": None,
                "status": "in_progress",
                "error": None,
                "incomplete_details": None,
                "instructions": body.get("instructions"),
                "max_output_tokens": body.get("max_output_tokens"),
                "model": body.get("model", MODEL),
                "output": [],
                "parallel_tool_calls": body.get(
                    "parallel_tool_calls",
                    True,
                ),
                "previous_response_id": None,
                "reasoning": None,
                "service_tier": "default",
                "store": False,
                "temperature": None,
                "text": {"format": {"type": "text"}},
                "tool_choice": "none",
                "tools": [],
                "top_p": None,
                "truncation": "disabled",
                "usage": None,
                "metadata": {},
            }
            events = [
                {
                    "type": "response.created",
                    "sequence_number": 0,
                    "response": initial,
                },
                {
                    "type": "response.in_progress",
                    "sequence_number": 1,
                    "response": initial,
                },
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                for event in events:
                    payload = json.dumps(event, separators=(",", ":"))
                    self.wfile.write(
                        f"event: {event['type']}\n"
                        f"data: {payload}\n\n".encode()
                    )
                self.wfile.flush()
                state.cancel_started.set()
                while True:
                    time.sleep(0.2)
                    self.wfile.write(b": waiting for cancellation\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        def _json_error(self, status: int, message: str) -> None:
            payload = json.dumps(
                {
                    "error": {
                        "message": message,
                        "type": "invalid_request_error",
                        "code": "axonllm_conformance_error",
                    }
                },
                separators=(",", ":"),
            ).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def _codex_command(
    *,
    codex_bin: Path,
    workspace: Path,
    port: int,
    output: Path,
    prompt: str,
) -> list[str]:
    return [
        str(codex_bin),
        "exec",
        "--ignore-user-config",
        "--ephemeral",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(workspace),
        "--model",
        MODEL,
        "--output-last-message",
        str(output),
        "-c",
        f'model_catalog_json="{MODEL_CATALOG}"',
        "-c",
        'model_provider="axonllm"',
        "-c",
        'model_providers.axonllm.name="AxonLLM"',
        "-c",
        f'model_providers.axonllm.base_url="http://127.0.0.1:{port}/v1"',
        "-c",
        'model_providers.axonllm.env_key="AXONLLM_API_KEY"',
        "-c",
        'model_providers.axonllm.wire_api="responses"',
        "-c",
        "model_providers.axonllm.request_max_retries=0",
        "-c",
        "model_providers.axonllm.stream_max_retries=0",
        "-c",
        "model_providers.axonllm.stream_idle_timeout_ms=5000",
        "-c",
        "model_providers.axonllm.requires_openai_auth=false",
        "-c",
        "model_supports_reasoning_summaries=false",
        "-c",
        'model_reasoning_summary="none"',
        "-c",
        "features.multi_agent=false",
        "-c",
        'approval_policy="never"',
        "-c",
        'web_search="disabled"',
        prompt,
    ]


def _environment(codex_home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "CODEX_HOME": str(codex_home),
            "AXONLLM_API_KEY": TOKEN,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return environment


def _assert_request_contract(requests: list[dict[str, Any]]) -> None:
    success = [
        request
        for request in requests
        if _request_marker(request) == SUCCESS_PROMPT
    ]
    if len(success) < 2:
        raise AssertionError("Codex did not complete a function-call round trip")
    first = success[0]
    if first.get("model") != MODEL:
        raise AssertionError(f"unexpected model: {first.get('model')}")
    if first.get("stream") is not True or first.get("store") is not False:
        raise AssertionError("Codex did not request a stateless streamed response")
    if first.get("reasoning") not in (None, {}):
        raise AssertionError("Codex requested unsupported reasoning metadata")
    if first.get("include") not in (
        None,
        [],
        ["reasoning.encrypted_content"],
    ):
        raise AssertionError("Codex requested unsupported include fields")
    tools = first.get("tools")
    if not isinstance(tools, list) or not tools:
        raise AssertionError("Codex request did not include tools")
    if any(
        not isinstance(tool, dict) or tool.get("type") != "function"
        for tool in tools
    ):
        raise AssertionError("Codex request included a non-function tool")
    final_input = success[-1].get("input")
    if not isinstance(final_input, list) or not any(
        isinstance(item, dict)
        and item.get("type") == "function_call_output"
        for item in final_input
    ):
        raise AssertionError("Codex did not return the tool output")


def _request_diagnostics(requests: list[dict[str, Any]]) -> str:
    safe_fields = (
        "model",
        "reasoning",
        "include",
        "service_tier",
        "store",
        "stream",
        "parallel_tool_calls",
    )
    sanitized = [
        {
            **{
                field: request.get(field)
                for field in safe_fields
                if field in request
            },
            "input_types": [
                item.get("type")
                for item in request.get("input", [])
                if isinstance(item, dict)
            ]
            if isinstance(request.get("input"), list)
            else type(request.get("input")).__name__,
            "tool_types": [
                tool.get("type")
                for tool in request.get("tools", [])
                if isinstance(tool, dict)
            ],
        }
        for request in requests
    ]
    return json.dumps(sanitized, sort_keys=True, separators=(",", ":"))


def _run_success(
    command: list[str],
    *,
    environment: dict[str, str],
    output: Path,
) -> None:
    completed = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            "Codex success case failed:\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    if output.read_text().strip() != FINAL_TEXT:
        raise AssertionError("Codex did not consume AxonLLM's final text event")


def _run_error(
    command: list[str],
    *,
    environment: dict[str, str],
) -> None:
    completed = subprocess.run(
        command,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    combined = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode == 0 or ERROR_TEXT not in combined:
        raise AssertionError(
            "Codex did not surface the OpenAI-shaped error:\n"
            f"{combined}"
        )


def _run_cancellation(
    command: list[str],
    *,
    environment: dict[str, str],
    state: _State,
) -> None:
    process = subprocess.Popen(
        command,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if not state.cancel_started.wait(timeout=20):
        process.kill()
        stdout, stderr = process.communicate(timeout=5)
        raise AssertionError(
            "Codex never entered the cancellation stream:\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    started = time.monotonic()
    process.send_signal(signal.SIGINT)
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.communicate(timeout=5)
        raise AssertionError("Codex did not cancel the active stream") from exc
    if time.monotonic() - started >= 10:
        raise AssertionError("Codex cancellation exceeded the deadline")
    if FINAL_TEXT in f"{stdout}\n{stderr}":
        raise AssertionError("cancelled Codex request produced a final answer")


def _version(codex_bin: Path) -> str:
    completed = subprocess.run(
        [str(codex_bin), "--version"],
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    return completed.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--codex-bin",
        type=Path,
        default=Path(os.environ.get("AXONLLM_CODEX_BIN", "codex")),
    )
    args = parser.parse_args()
    version = _version(args.codex_bin)
    if version != f"codex-cli {SUPPORTED_CODEX_VERSION}":
        raise SystemExit(
            f"unsupported Codex CLI: {version!r}; "
            f"expected codex-cli {SUPPORTED_CODEX_VERSION}"
        )

    state = _State()
    api = OpenAICompatAPI(_FakeClientAgent())  # type: ignore[arg-type]
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        _make_handler(state, api),
    )
    server_thread = threading.Thread(
        target=server.serve_forever,
        daemon=True,
    )
    server_thread.start()

    try:
        with tempfile.TemporaryDirectory(
            prefix="axonllm-codex-",
        ) as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            codex_home = root / "codex-home"
            workspace.mkdir()
            codex_home.mkdir()
            environment = _environment(codex_home)

            success_output = root / "success.txt"
            try:
                _run_success(
                    _codex_command(
                        codex_bin=args.codex_bin,
                        workspace=workspace,
                        port=server.server_port,
                        output=success_output,
                        prompt=(
                            f"{SUCCESS_PROMPT}: use one harmless shell tool, "
                            "then return the server-provided final answer."
                        ),
                    ),
                    environment=environment,
                    output=success_output,
                )
            except AssertionError as exc:
                raise AssertionError(
                    f"{exc}\nrequest metadata:\n"
                    f"{_request_diagnostics(state.snapshot())}"
                ) from exc
            _assert_request_contract(state.snapshot())

            _run_error(
                _codex_command(
                    codex_bin=args.codex_bin,
                    workspace=workspace,
                    port=server.server_port,
                    output=root / "error.txt",
                    prompt=f"{ERROR_PROMPT}: verify error propagation.",
                ),
                environment=environment,
            )
            _run_cancellation(
                _codex_command(
                    codex_bin=args.codex_bin,
                    workspace=workspace,
                    port=server.server_port,
                    output=root / "cancel.txt",
                    prompt=f"{CANCEL_PROMPT}: keep this stream open.",
                ),
                environment=environment,
                state=state,
            )
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    print(
        "Codex 0.150.1 request, tool-loop, streaming, error, and "
        "cancellation conformance passed"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
