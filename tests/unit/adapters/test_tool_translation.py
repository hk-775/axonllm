"""Tool-calling pass-through across every provider dialect.

The gateway's request model carries ``tools``/``tool_choice`` and each adapter
translates them into its provider's own shape. The failure these tests guard
against is *silent*: when a tool spec is dropped, the provider still returns a
fluent HTTP 200 — the model simply answers that it has no such capability — so
nothing errors and the whole tool-use loop disappears. Every assertion below is
therefore about the specs actually arriving, and about the response coming back
in a shape a caller's tool loop branches on.

Two cross-cutting invariants recur per provider:

  * **arguments encoding** — OpenAI carries tool arguments as a JSON *string*;
    Anthropic, Gemini, Bedrock and Cohere use objects. Callers ``json.loads()``
    the field, so a dict leaking through raises at the caller, not here.
  * **finish_reason** — callers branch on ``"tool_calls"``. Anthropic says
    ``tool_use``, Bedrock says ``tool_use`` in ``stopReason``, and Gemini says
    ``STOP`` even while calling a function. Each has to be normalized.
"""

import json

import pytest

from src.gateway.adapters.anthropic_adapter import AnthropicAdapter
from src.gateway.adapters.cohere_adapter import CohereAdapter
from src.gateway.adapters.google_ai_adapter import GoogleAIAdapter
from src.gateway.adapters.openai_adapter import OpenAIAdapter
from src.gateway.adapters.vertex_adapter import VertexAIAdapter
from src.gateway.models import ChatCompletionRequest

# One tool spec reused across providers, deliberately carrying schema keys
# (additionalProperties) that Gemini rejects outright.
TOOL = {
    "type": "function",
    "function": {
        "name": "db_query",
        "description": "Run a SQL query",
        "parameters": {
            "type": "object",
            "properties": {"sql": {"type": "string", "description": "the query"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
}

# A two-round tool loop: user asks, assistant calls, tool answers.
TOOL_LOOP = [
    {"role": "user", "content": "how many orders?"},
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_1", "type": "function",
        "function": {"name": "db_query", "arguments": '{"sql": "select count(*) from orders"}'},
    }]},
    {"role": "tool", "tool_call_id": "call_1", "content": '{"row_count": 42}'},
]
NAMED_TOOL_LOOP = [
    *TOOL_LOOP[:-1],
    {**TOOL_LOOP[-1], "name": "db_query"},
]


def _req(**kw) -> ChatCompletionRequest:
    kw.setdefault("messages", [{"role": "user", "content": "how many orders?"}])
    kw.setdefault("model", "m")
    return ChatCompletionRequest(**kw)


class TestRequestModel:
    """The field that started it all — absent, ``_parse_request`` had nothing to read."""

    def test_tools_default_to_none(self):
        assert _req().tools is None
        assert _req().tool_choice is None

    def test_tools_are_carried(self):
        req = _req(tools=[TOOL], tool_choice="auto")
        assert req.tools == [TOOL]
        assert req.tool_choice == "auto"


class TestOpenAIStyle:
    """OpenAI is the native dialect — pass-through, but it must actually happen."""

    @pytest.mark.asyncio
    async def test_tools_pass_through_unchanged(self):
        payload = await OpenAIAdapter().translate_request(_req(tools=[TOOL], tool_choice="auto"))
        assert payload["tools"] == [TOOL]
        assert payload["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_no_tool_keys_when_no_tools(self):
        """An empty ``tools: []`` is rejected by OpenAI, so the key must be absent."""
        payload = await OpenAIAdapter().translate_request(_req())
        assert "tools" not in payload
        assert "tool_choice" not in payload

    @pytest.mark.asyncio
    async def test_tool_choice_omitted_when_unset(self):
        payload = await OpenAIAdapter().translate_request(_req(tools=[TOOL]))
        assert payload["tools"] == [TOOL]
        assert "tool_choice" not in payload


class TestAnthropicTools:
    @pytest.fixture
    def adapter(self):
        return AnthropicAdapter()

    @pytest.mark.asyncio
    async def test_tool_spec_becomes_input_schema(self, adapter):
        payload = await adapter.translate_request(_req(tools=[TOOL]))
        tool = payload["tools"][0]
        assert tool["name"] == "db_query"
        assert tool["description"] == "Run a SQL query"
        # Anthropic calls it input_schema, not parameters.
        assert tool["input_schema"]["properties"]["sql"]["type"] == "string"
        assert "parameters" not in tool

    @pytest.mark.asyncio
    async def test_bare_anthropic_tool_is_accepted_as_is(self, adapter):
        """Ostiari's /v1/messages shim forwards Anthropic-shaped tools verbatim."""
        native = {"name": "db_query", "description": "d",
                  "input_schema": {"type": "object", "properties": {}}}
        payload = await adapter.translate_request(_req(tools=[native]))
        assert payload["tools"][0]["input_schema"] == {"type": "object", "properties": {}}

    @pytest.mark.asyncio
    async def test_schemaless_tool_gets_an_empty_object_schema(self, adapter):
        """Anthropic 400s on a missing input_schema; a tool with no args is legal."""
        payload = await adapter.translate_request(
            _req(tools=[{"type": "function", "function": {"name": "ping"}}]))
        assert payload["tools"][0]["input_schema"] == {"type": "object", "properties": {}}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("choice,expected", [
        ("auto", {"type": "auto"}),
        ("required", {"type": "any"}),
        ("any", {"type": "any"}),
        ({"type": "function", "function": {"name": "db_query"}},
         {"type": "tool", "name": "db_query"}),
    ])
    async def test_tool_choice_translation(self, adapter, choice, expected):
        payload = await adapter.translate_request(_req(tools=[TOOL], tool_choice=choice))
        assert payload["tool_choice"] == expected

    @pytest.mark.asyncio
    async def test_tool_choice_none_omits_tools(self, adapter):
        """Anthropic expresses "none" by omitting the tool set entirely."""
        payload = await adapter.translate_request(_req(tools=[TOOL], tool_choice="none"))
        assert "tool_choice" not in payload
        assert "tools" not in payload

    @pytest.mark.asyncio
    async def test_tool_loop_messages_become_blocks(self, adapter):
        payload = await adapter.translate_request(_req(messages=TOOL_LOOP, tools=[TOOL]))
        msgs = payload["messages"]
        assert len(msgs) == 3

        # The assistant's call becomes a tool_use block with a parsed input object.
        use = msgs[1]["content"][0]
        assert msgs[1]["role"] == "assistant"
        assert use["type"] == "tool_use" and use["id"] == "call_1"
        assert use["name"] == "db_query"
        assert use["input"] == {"sql": "select count(*) from orders"}

        # The tool result becomes a *user* turn — Anthropic has no "tool" role.
        assert msgs[2]["role"] == "user"
        result = msgs[2]["content"][0]
        assert result["type"] == "tool_result"
        assert result["tool_use_id"] == "call_1"

    @pytest.mark.asyncio
    async def test_malformed_tool_arguments_do_not_fail_the_request(self, adapter):
        """A model can emit broken JSON; the tool should report that, not the gateway."""
        msgs = [{"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "function": {"name": "db_query", "arguments": "{not json"}}]}]
        payload = await adapter.translate_request(_req(messages=msgs, tools=[TOOL]))
        assert payload["messages"][0]["content"][0]["input"] == {}

    def test_response_tool_use_becomes_openai_tool_calls(self, adapter):
        res = adapter.translate_response({
            "id": "msg_1", "model": "claude-sonnet-4-6", "stop_reason": "tool_use",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "db_query",
                         "input": {"sql": "select 1"}}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        msg = res.choices[0]["message"]
        assert msg["tool_calls"][0]["id"] == "toolu_1"
        assert msg["tool_calls"][0]["function"]["name"] == "db_query"
        # A JSON string, not a dict — callers json.loads() this.
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"sql": "select 1"}
        assert msg["content"] is None            # OpenAI sends null alongside tool_calls
        assert res.choices[0]["finish_reason"] == "tool_calls"

    def test_text_alongside_tool_use_is_kept(self, adapter):
        res = adapter.translate_response({
            "id": "m", "model": "m", "stop_reason": "tool_use",
            "content": [{"type": "text", "text": "Let me check."},
                        {"type": "tool_use", "id": "t1", "name": "db_query", "input": {}}],
            "usage": {},
        })
        msg = res.choices[0]["message"]
        assert msg["content"] == "Let me check."
        assert len(msg["tool_calls"]) == 1

    def test_plain_response_keeps_empty_string_content(self, adapter):
        """Only tool-call responses get content=None; nothing else changes shape."""
        res = adapter.translate_response({
            "id": "m", "model": "m", "stop_reason": "end_turn",
            "content": [], "usage": {},
        })
        assert res.choices[0]["message"]["content"] == ""
        assert res.choices[0]["finish_reason"] == "end_turn"


class TestGeminiTools:
    """Google AI and Vertex share one dialect and one translation module."""

    @pytest.fixture(params=[GoogleAIAdapter, VertexAIAdapter])
    def adapter(self, request):
        return request.param()

    @pytest.mark.asyncio
    async def test_tools_become_function_declarations(self, adapter):
        payload = await adapter.translate_request(_req(tools=[TOOL]))
        # One tools entry holding every declaration, not one entry per tool.
        assert len(payload["tools"]) == 1
        decls = payload["tools"][0]["functionDeclarations"]
        assert [d["name"] for d in decls] == ["db_query"]

    @pytest.mark.asyncio
    async def test_unsupported_schema_keys_are_stripped(self, adapter):
        """Gemini 400s on additionalProperties rather than ignoring it."""
        payload = await adapter.translate_request(_req(tools=[TOOL]))
        schema = payload["tools"][0]["functionDeclarations"][0]["parameters"]
        assert "additionalProperties" not in schema
        assert schema["properties"]["sql"]["type"] == "string"   # the useful parts survive
        assert schema["required"] == ["sql"]

    @pytest.mark.asyncio
    async def test_nested_schema_keys_are_stripped_too(self, adapter):
        nested = {"type": "function", "function": {"name": "f", "parameters": {
            "type": "object", "title": "Args", "properties": {
                "rows": {"type": "array", "default": [],
                         "items": {"type": "object", "additionalProperties": False,
                                   "properties": {"id": {"type": "integer", "title": "Id"}}}},
            }}}}
        payload = await adapter.translate_request(_req(tools=[nested]))
        schema = payload["tools"][0]["functionDeclarations"][0]["parameters"]
        assert "title" not in schema
        rows = schema["properties"]["rows"]
        assert "default" not in rows
        assert "additionalProperties" not in rows["items"]
        assert "title" not in rows["items"]["properties"]["id"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("choice,mode", [
        ("auto", "AUTO"), ("required", "ANY"), ("any", "ANY"), ("none", "NONE"),
    ])
    async def test_tool_choice_modes(self, adapter, choice, mode):
        payload = await adapter.translate_request(_req(tools=[TOOL], tool_choice=choice))
        assert payload["toolConfig"]["functionCallingConfig"]["mode"] == mode

    @pytest.mark.asyncio
    async def test_named_tool_choice_uses_an_allowlist(self, adapter):
        payload = await adapter.translate_request(_req(
            tools=[TOOL], tool_choice={"type": "function", "function": {"name": "db_query"}}))
        cfg = payload["toolConfig"]["functionCallingConfig"]
        assert cfg["mode"] == "ANY" and cfg["allowedFunctionNames"] == ["db_query"]

    @pytest.mark.asyncio
    async def test_tool_loop_becomes_function_call_and_response_parts(self, adapter):
        payload = await adapter.translate_request(_req(messages=TOOL_LOOP, tools=[TOOL]))
        contents = payload["contents"]
        assert len(contents) == 3

        # Gemini's assistant role is "model".
        assert contents[1]["role"] == "model"
        call = contents[1]["parts"][0]["functionCall"]
        assert call["name"] == "db_query"
        assert call["args"] == {"sql": "select count(*) from orders"}

        # A tool result is a user-role functionResponse keyed by function name.
        assert contents[2]["role"] == "user"
        resp = contents[2]["parts"][0]["functionResponse"]
        assert resp["name"] == "db_query"
        assert resp["response"]["content"] == '{"row_count": 42}'

    @pytest.mark.asyncio
    async def test_explicit_tool_result_name_remains_supported(self, adapter):
        payload = await adapter.translate_request(
            _req(messages=NAMED_TOOL_LOOP, tools=[TOOL])
        )

        response = payload["contents"][2]["parts"][0]["functionResponse"]
        assert response["name"] == "db_query"

    def test_thinking_and_cached_tokens_are_accounted(self, adapter):
        response = adapter.translate_response({
            "responseId": "gemini-response",
            "modelVersion": "gemini-2.5-pro-001",
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": "answer"}]},
            }],
            "usageMetadata": {
                "promptTokenCount": 12,
                "candidatesTokenCount": 5,
                "thoughtsTokenCount": 7,
                "cachedContentTokenCount": 3,
                "totalTokenCount": 24,
            },
        })

        assert response.usage.prompt_tokens == 12
        assert response.usage.completion_tokens == 12
        assert response.usage.total_tokens == 24
        assert response.usage.cached_tokens == 3

    def test_function_call_part_forces_tool_calls_finish_reason(self, adapter):
        """Gemini returns STOP while calling a function — the part is the only signal."""
        res = adapter.translate_response({
            "model": "gemini-2.5-pro",
            "candidates": [{"finishReason": "STOP", "content": {"parts": [
                {"functionCall": {"name": "db_query", "args": {"sql": "select 1"}}}]}}],
            "usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 4},
        })
        assert res.choices[0]["finish_reason"] == "tool_calls"
        tc = res.choices[0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "db_query"
        assert json.loads(tc["function"]["arguments"]) == {"sql": "select 1"}
        assert res.choices[0]["message"]["content"] is None

    @pytest.mark.asyncio
    async def test_gemini_3_thought_signature_survives_tool_round_trip(
        self,
        adapter,
    ):
        response = adapter.translate_response({
            "model": "gemini-3.5-flash",
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{
                    "functionCall": {
                        "id": "provider-call-1",
                        "name": "db_query",
                        "args": {"sql": "select 1"},
                    },
                    "thoughtSignature": "opaque-provider-signature",
                }]},
            }],
            "usageMetadata": {},
        })
        message = response.choices[0]["message"]
        tool_call = message["tool_calls"][0]

        payload = await adapter.translate_request(_req(
            messages=[
                {"role": "user", "content": "Run the query"},
                message,
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": '{"row_count": 1}',
                },
            ],
            tools=[TOOL],
        ))

        part = payload["contents"][1]["parts"][0]
        assert part["functionCall"]["id"] == "provider-call-1"
        assert part["functionCall"]["name"] == "db_query"
        assert part["thoughtSignature"] == "opaque-provider-signature"
        assert payload["contents"][2]["parts"][0]["functionResponse"][
            "name"
        ] == "db_query"

    def test_plain_text_response_is_untouched(self, adapter):
        res = adapter.translate_response({
            "model": "m",
            "candidates": [{"finishReason": "STOP",
                            "content": {"parts": [{"text": "42 rows"}]}}],
            "usageMetadata": {},
        })
        assert res.choices[0]["message"]["content"] == "42 rows"
        assert res.choices[0]["finish_reason"] == "stop"
        assert "tool_calls" not in res.choices[0]["message"]


class TestCohereTools:
    @pytest.fixture
    def adapter(self):
        return CohereAdapter()

    @pytest.mark.asyncio
    async def test_schema_is_unrolled_into_parameter_definitions(self, adapter):
        """Cohere describes each parameter individually instead of taking a schema."""
        payload = await adapter.translate_request(_req(tools=[TOOL]))
        tool = payload["tools"][0]
        assert tool["name"] == "db_query"
        sql = tool["parameter_definitions"]["sql"]
        assert sql["type"] == "str"          # Python-ish type names, not JSON Schema's
        assert sql["description"] == "the query"
        assert sql["required"] is True

    @pytest.mark.asyncio
    async def test_json_schema_types_map_to_cohere_types(self, adapter):
        tool = {"type": "function", "function": {"name": "f", "parameters": {
            "type": "object", "properties": {
                "s": {"type": "string"}, "i": {"type": "integer"},
                "n": {"type": "number"}, "b": {"type": "boolean"},
                "a": {"type": "array"}, "o": {"type": "object"},
                "weird": {"type": "nonesuch"},
            }}}}
        payload = await adapter.translate_request(_req(tools=[tool]))
        defs = payload["tools"][0]["parameter_definitions"]
        assert [defs[k]["type"] for k in ("s", "i", "n", "b", "a", "o")] == [
            "str", "int", "float", "bool", "list", "dict"]
        # An unrecognized type still yields a fillable parameter.
        assert defs["weird"]["type"] == "str"

    @pytest.mark.asyncio
    async def test_tool_result_goes_to_top_level_tool_results(self, adapter):
        payload = await adapter.translate_request(_req(messages=TOOL_LOOP, tools=[TOOL]))
        assert payload["tool_results"][0]["call"]["name"] == "db_query"
        assert payload["tool_results"][0]["call"]["parameters"] == {
            "sql": "select count(*) from orders"
        }
        assert payload["tool_results"][0]["outputs"][0]["output"] == '{"row_count": 42}'
        # Kept out of chat_history, or it would be read as the user's next message.
        assert all("row_count" not in str(h.get("message", "")) for h in
                   payload.get("chat_history", []))

    @pytest.mark.asyncio
    async def test_explicit_tool_result_name_remains_supported(self, adapter):
        payload = await adapter.translate_request(
            _req(messages=NAMED_TOOL_LOOP, tools=[TOOL])
        )

        assert payload["tool_results"][0]["call"]["name"] == "db_query"

    @pytest.mark.asyncio
    async def test_assistant_tool_calls_recorded_in_history(self, adapter):
        payload = await adapter.translate_request(_req(messages=TOOL_LOOP, tools=[TOOL]))
        chatbot = [h for h in payload["chat_history"] if h["role"] == "CHATBOT"]
        assert chatbot[0]["tool_calls"][0]["name"] == "db_query"
        # Cohere wants an object here, not OpenAI's JSON string.
        assert chatbot[0]["tool_calls"][0]["parameters"] == {
            "sql": "select count(*) from orders"}

    @pytest.mark.asyncio
    async def test_none_tool_choice_omits_tools(self, adapter):
        payload = await adapter.translate_request(
            _req(tools=[TOOL], tool_choice="none")
        )

        assert "tools" not in payload
        assert "tool_choice" not in payload
        assert "force_single_step" not in payload

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool_choice", "unsupported_control"),
        [
            ("required", "required-tool selection"),
            (
                {"type": "function", "function": {"name": "db_query"}},
                "named-tool selection",
            ),
        ],
    )
    async def test_unsupported_tool_choice_is_rejected(
        self,
        adapter,
        tool_choice,
        unsupported_control,
    ):
        from src.gateway.router import ProviderError

        with pytest.raises(ProviderError, match=unsupported_control) as exc:
            await adapter.translate_request(
                _req(tools=[TOOL], tool_choice=tool_choice)
            )
        assert exc.value.status_code == 400
        assert exc.value.retryable is False

    @pytest.mark.asyncio
    async def test_auto_tool_choice_needs_no_warning(self, adapter):
        """"auto" is what Cohere already does, so there's nothing to warn about."""
        payload = await adapter.translate_request(_req(tools=[TOOL], tool_choice="auto"))
        assert payload["tools"]
        assert "_warnings" not in payload

    def test_response_parameters_become_an_arguments_string(self, adapter):
        res = adapter.translate_response({
            "id": "c1", "model": "command-r-plus", "text": "",
            "tool_calls": [{"name": "db_query", "parameters": {"sql": "select 1"}}],
            "meta": {"tokens": {"input_tokens": 6, "output_tokens": 3}},
        })
        tc = res.choices[0]["message"]["tool_calls"][0]
        assert json.loads(tc["function"]["arguments"]) == {"sql": "select 1"}
        assert tc["id"], "a call id must be synthesized — Cohere sends none"
        assert res.choices[0]["finish_reason"] == "tool_calls"


class TestBedrockConverseTools:
    """Bedrock's Converse API has its own tool shape again (toolSpec/toolUse)."""

    async def _converse(self, request, response=None):
        """Run _invoke_converse against a stub boto3 client, returning (kwargs, result).

        The payload is built inside the coroutine, so capturing what boto3 would
        have been called with is the only way to assert on it.
        """
        from unittest.mock import MagicMock

        from src.gateway.bedrock_provider import _invoke_converse
        from src.gateway.models import ProviderModelMapping

        captured: dict = {}

        def _converse_call(**kwargs):
            captured.update(kwargs)
            return response or {
                "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
                "stopReason": "end_turn", "usage": {"inputTokens": 1, "outputTokens": 1},
            }

        client = MagicMock()
        client.converse = _converse_call
        mapping = ProviderModelMapping(provider="bedrock", model_id="amazon.nova-pro-v1:0")
        result = await _invoke_converse(client, request, mapping)
        return captured, result

    @pytest.mark.asyncio
    async def test_tools_become_tool_config_specs(self):
        kwargs, _ = await self._converse(_req(tools=[TOOL]))
        spec = kwargs["toolConfig"]["tools"][0]["toolSpec"]
        assert spec["name"] == "db_query"
        assert spec["description"] == "Run a SQL query"
        # Converse nests the schema under inputSchema.json.
        assert spec["inputSchema"]["json"]["properties"]["sql"]["type"] == "string"

    @pytest.mark.asyncio
    async def test_no_tool_config_when_no_tools(self):
        kwargs, _ = await self._converse(_req())
        assert "toolConfig" not in kwargs

    @pytest.mark.asyncio
    @pytest.mark.parametrize("choice,expected", [
        ("auto", {"auto": {}}),
        ("required", {"any": {}}),
        ("any", {"any": {}}),
        ({"type": "function", "function": {"name": "db_query"}},
         {"tool": {"name": "db_query"}}),
    ])
    async def test_tool_choice_translation(self, choice, expected):
        kwargs, _ = await self._converse(_req(tools=[TOOL], tool_choice=choice))
        assert kwargs["toolConfig"]["toolChoice"] == expected

    @pytest.mark.asyncio
    async def test_tool_choice_none_omits_tool_config(self):
        kwargs, _ = await self._converse(
            _req(tools=[TOOL], tool_choice="none")
        )
        assert "toolConfig" not in kwargs

    @pytest.mark.asyncio
    async def test_tool_loop_becomes_tool_use_and_tool_result_blocks(self):
        kwargs, _ = await self._converse(_req(messages=TOOL_LOOP, tools=[TOOL]))
        msgs = kwargs["messages"]
        use = msgs[1]["content"][0]["toolUse"]
        assert use["name"] == "db_query"
        assert use["input"] == {"sql": "select count(*) from orders"}
        # Converse carries results on a user turn, like Anthropic.
        assert msgs[2]["role"] == "user"
        assert msgs[2]["content"][0]["toolResult"]["toolUseId"] == "call_1"

    @pytest.mark.asyncio
    async def test_response_tool_use_maps_to_openai_tool_calls(self):
        _, res = await self._converse(_req(tools=[TOOL]), response={
            "output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "tu_1", "name": "db_query",
                             "input": {"sql": "select 1"}}}]}},
            "stopReason": "tool_use",
            "usage": {"inputTokens": 9, "outputTokens": 4},
        })
        choice = res.choices[0]
        tc = choice["message"]["tool_calls"][0]
        assert tc["id"] == "tu_1"
        assert json.loads(tc["function"]["arguments"]) == {"sql": "select 1"}
        assert choice["message"]["content"] is None
        assert choice["finish_reason"] == "tool_calls"


class TestCacheKeyIncludesTools:
    """The same prompt answers differently with and without a tool list."""

    def _key(self, **kw):
        from src.gateway.cache_manager import CacheManager
        return CacheManager().compute_cache_key(_req(**kw), "proj")

    def test_tools_change_the_key(self):
        """Otherwise a cached tool-free reply is served to a call needing a tool."""
        assert self._key() != self._key(tools=[TOOL])

    def test_tool_choice_changes_the_key(self):
        assert self._key(tools=[TOOL]) != self._key(tools=[TOOL], tool_choice="required")

    def test_identical_tool_requests_share_a_key(self):
        assert self._key(tools=[TOOL], tool_choice="auto") == self._key(
            tools=[TOOL], tool_choice="auto")


class TestRequestValidation:
    """An assistant turn that only calls tools has no ``content`` at all."""

    def _errors(self, messages):
        from unittest.mock import MagicMock

        from src.gateway.request_validator import RequestValidator

        registry = MagicMock()
        registry.get_model.return_value = None      # skip model-specific checks
        return RequestValidator(registry).validate(_req(messages=messages, tools=[TOOL]))

    def test_tool_only_assistant_turn_is_valid(self):
        """Rejecting it would break every tool loop at round two."""
        errors = self._errors(TOOL_LOOP)
        assert not [e for e in errors if "content" in e.field], errors

    def test_contentless_message_without_tool_calls_is_still_invalid(self):
        errors = self._errors([{"role": "user"}])
        assert any("content" in e.message or "content" in e.field for e in errors)
