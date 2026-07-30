"""Unit tests for Bedrock Mantle model→API routing and tool translation."""

import asyncio
import json

import pytest

import src.gateway.mantle_provider as mp
from src.gateway.mantle_provider import (
    _is_anthropic_model,
    _is_unsupported_route_error,
    _prefers_responses_api,
    create_mantle_provider_fn,
)
from src.gateway.models import ChatCompletionRequest, ProviderModelMapping
from src.gateway.router import ProviderError


def _run(coro):
    return asyncio.run(coro)


class TestRouteSelection:
    def test_anthropic_prefix(self):
        assert _is_anthropic_model("anthropic.claude-sonnet-5")
        assert not _is_anthropic_model("openai.gpt-5.6-sol")

    def test_frontier_gpt_prefers_responses(self):
        assert _prefers_responses_api("openai.gpt-5.6-sol")
        assert _prefers_responses_api("openai.gpt-4.1")
        assert _prefers_responses_api("openai.o3")

    def test_open_weight_does_not_prefer_responses(self):
        # gpt-oss, deepseek, qwen route via chat completions
        assert not _prefers_responses_api("openai.gpt-oss-120b")
        assert not _prefers_responses_api("deepseek.v3.1")
        assert not _prefers_responses_api("qwen.qwen3-32b")


class TestUnsupportedRouteDetection:
    def test_detects_does_not_support(self):
        exc = ProviderError(400, "bedrock-mantle", "The model 'x' does not support the '/v1/responses' API")
        assert _is_unsupported_route_error(exc)

    def test_detects_isnt_supported_on_route(self):
        exc = ProviderError(400, "bedrock-mantle", "model `x` isn't supported on this route")
        assert _is_unsupported_route_error(exc)

    def test_ignores_other_errors(self):
        assert not _is_unsupported_route_error(ProviderError(404, "bedrock-mantle", "does not exist"))
        assert not _is_unsupported_route_error(ProviderError(400, "bedrock-mantle", "bad request"))
        assert not _is_unsupported_route_error(ProviderError(429, "bedrock-mantle", "does not support"))


def _make_provider(monkeypatch, calls):
    """Build a provider_fn while recording which API path each call takes."""
    # Avoid real AWS session/credential resolution.
    monkeypatch.setattr(mp.boto3, "Session", lambda: type("S", (), {"get_credentials": lambda self: None})())

    async def fake_responses(creds, endpoint, region, request, mapping):
        calls.append("responses")
        raise ProviderError(400, "bedrock-mantle", f"The model '{mapping.model_id}' does not support the '/v1/responses' API")

    async def fake_chat(creds, endpoint, region, request, mapping):
        calls.append("chat")
        return "CHAT_OK"

    async def fake_messages(creds, endpoint, region, request, mapping):
        calls.append("messages")
        return "MSG_OK"

    monkeypatch.setattr(mp, "_invoke_responses_api", fake_responses)
    monkeypatch.setattr(mp, "_invoke_chat_completions_api", fake_chat)
    monkeypatch.setattr(mp, "_invoke_messages_api", fake_messages)

    factory = create_mantle_provider_fn(region="us-east-1")
    request = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
    return factory(request)


class TestDispatch:
    def test_anthropic_uses_messages(self, monkeypatch):
        calls = []
        fn = _make_provider(monkeypatch, calls)
        result = _run(fn(ProviderModelMapping(provider="bedrock-mantle", model_id="anthropic.claude-sonnet-5")))
        assert result == "MSG_OK"
        assert calls == ["messages"]

    def test_open_weight_uses_chat_directly(self, monkeypatch):
        calls = []
        fn = _make_provider(monkeypatch, calls)
        result = _run(fn(ProviderModelMapping(provider="bedrock-mantle", model_id="qwen.qwen3-32b")))
        assert result == "CHAT_OK"
        assert calls == ["chat"]  # never tried responses

    def test_gpt_oss_falls_back_from_responses_to_chat(self, monkeypatch):
        # openai.gpt-oss-* is not in the responses-preferring set, so it should
        # go straight to chat completions.
        calls = []
        fn = _make_provider(monkeypatch, calls)
        result = _run(fn(ProviderModelMapping(provider="bedrock-mantle", model_id="openai.gpt-oss-120b")))
        assert result == "CHAT_OK"
        assert calls == ["chat"]

    def test_frontier_gpt_falls_back_to_chat_on_unsupported_route(self, monkeypatch):
        # openai.gpt-5.x prefers responses; our fake responses raises the
        # unsupported-route error, so it must fall back to chat.
        calls = []
        fn = _make_provider(monkeypatch, calls)
        result = _run(fn(ProviderModelMapping(provider="bedrock-mantle", model_id="openai.gpt-5.6-sol")))
        assert result == "CHAT_OK"
        assert calls == ["responses", "chat"]


class TestFallbackDoesNotMaskRealErrors:
    def test_non_route_error_propagates(self, monkeypatch):
        monkeypatch.setattr(mp.boto3, "Session", lambda: type("S", (), {"get_credentials": lambda self: None})())

        async def fake_responses(creds, endpoint, region, request, mapping):
            raise ProviderError(429, "bedrock-mantle", "rate limited")

        monkeypatch.setattr(mp, "_invoke_responses_api", fake_responses)
        factory = create_mantle_provider_fn(region="us-east-1")
        request = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
        fn = factory(request)
        with pytest.raises(ProviderError) as ei:
            _run(fn(ProviderModelMapping(provider="bedrock-mantle", model_id="openai.gpt-5.6-sol")))
        assert ei.value.status_code == 429


# --- Tool calling -----------------------------------------------------------
#
# This module hand-builds three payloads instead of going through the adapter
# layer, so the adapters' tool tests cover none of it. The dialects below were
# each verified against live Mantle; the shapes are not interchangeable, so the
# tests assert on the captured outbound body rather than on a return value —
# tools silently missing from the wire is exactly the bug being fixed.

WEATHER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a city",
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string"}},
            "required": ["city"],
        },
    },
}

# One full OpenAI-shaped tool round-trip, as an OpenAI-SDK client sends turn 2.
OPENAI_TOOL_HISTORY = [
    {"role": "user", "content": "weather in Paris?"},
    {"role": "assistant", "content": None, "tool_calls": [{
        "id": "call_abc123", "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
    }]},
    {"role": "tool", "tool_call_id": "call_abc123", "content": '{"temp_c":21}'},
]


def _capture(monkeypatch, response_data):
    """Stub the signed request, returning a dict that records url + parsed body.

    _sigv4_request is a module-level function, not a method, so it is patched on
    the module.
    """
    captured: dict = {}

    def fake_sigv4(credentials, region, url, body):
        captured["url"] = url
        captured["payload"] = json.loads(body)
        return response_data

    monkeypatch.setattr(mp, "_sigv4_request", fake_sigv4)
    return captured


def _invoke(fn, request, model_id):
    mapping = ProviderModelMapping(provider="bedrock-mantle", model_id=model_id)
    return _run(fn(credentials=None, endpoint="https://ep", region="us-east-1",
                   request=request, mapping=mapping))


class TestMessagesApiToolRequest:
    """Anthropic route: tools[].{name,input_schema}, content-block history."""

    def test_tools_are_sent_in_anthropic_dialect(self, monkeypatch):
        cap = _capture(monkeypatch, {"content": [], "usage": {}})
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="m", tools=[WEATHER_TOOL])
        _invoke(mp._invoke_messages_api, req, "anthropic.claude-sonnet-5")

        tools = cap["payload"]["tools"]
        assert len(tools) == 1
        assert tools[0]["name"] == "get_weather"
        # Anthropic names the schema input_schema; `parameters` is rejected.
        assert tools[0]["input_schema"]["properties"] == {"city": {"type": "string"}}
        assert "function" not in tools[0]
        assert "parameters" not in tools[0]

    def test_tool_choice_is_translated(self, monkeypatch):
        cap = _capture(monkeypatch, {"content": [], "usage": {}})
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="m", tools=[WEATHER_TOOL], tool_choice="required")
        _invoke(mp._invoke_messages_api, req, "anthropic.claude-sonnet-5")
        # OpenAI's "required" is Anthropic's {"type": "any"}.
        assert cap["payload"]["tool_choice"] == {"type": "any"}

    def test_tool_history_becomes_content_blocks(self, monkeypatch):
        cap = _capture(monkeypatch, {"content": [], "usage": {}})
        req = ChatCompletionRequest(messages=OPENAI_TOOL_HISTORY, model="m",
                                    tools=[WEATHER_TOOL])
        _invoke(mp._invoke_messages_api, req, "anthropic.claude-sonnet-5")

        msgs = cap["payload"]["messages"]
        # role:"tool" has no Anthropic equivalent — live Mantle answers
        # 400 Unexpected role "tool" if it is forwarded as-is.
        assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
        assert msgs[1]["content"][0]["type"] == "tool_use"
        # Anthropic wants parsed input, not OpenAI's JSON string.
        assert msgs[1]["content"][0]["input"] == {"city": "Paris"}
        assert msgs[2]["content"][0] == {
            "type": "tool_result", "tool_use_id": "call_abc123",
            "content": '{"temp_c":21}',
        }

    def test_no_tools_key_when_none_requested(self, monkeypatch):
        cap = _capture(monkeypatch, {"content": [], "usage": {}})
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
        _invoke(mp._invoke_messages_api, req, "anthropic.claude-sonnet-5")
        assert "tools" not in cap["payload"]
        assert "tool_choice" not in cap["payload"]


class TestMessagesApiToolResponse:
    def test_tool_use_becomes_openai_tool_calls(self, monkeypatch):
        _capture(monkeypatch, {
            "id": "msg_1",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                         "input": {"city": "Paris"}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="m", tools=[WEATHER_TOOL])
        res = _invoke(mp._invoke_messages_api, req, "anthropic.claude-sonnet-5")

        msg = res.choices[0]["message"]
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
        # Callers json.loads() arguments, so it must be a string, not a dict.
        assert msg["tool_calls"][0]["function"]["arguments"] == '{"city": "Paris"}'
        assert msg["tool_calls"][0]["id"] == "toolu_1"
        # OpenAI sends content=null alongside tool_calls.
        assert msg["content"] is None
        # A caller driving a tool loop branches on this; "tool_use" ends the loop.
        assert res.choices[0]["finish_reason"] == "tool_calls"

    def test_text_alongside_tool_call_is_kept(self, monkeypatch):
        _capture(monkeypatch, {
            "content": [{"type": "text", "text": "Checking."},
                        {"type": "tool_use", "id": "t1", "name": "get_weather", "input": {}}],
            "stop_reason": "tool_use", "usage": {},
        })
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
        res = _invoke(mp._invoke_messages_api, req, "anthropic.claude-sonnet-5")
        assert res.choices[0]["message"]["content"] == "Checking."
        assert len(res.choices[0]["message"]["tool_calls"]) == 1

    def test_plain_text_response_is_unchanged(self, monkeypatch):
        _capture(monkeypatch, {
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn", "usage": {},
        })
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
        res = _invoke(mp._invoke_messages_api, req, "anthropic.claude-sonnet-5")
        assert res.choices[0]["message"] == {"role": "assistant", "content": "hello"}
        assert res.choices[0]["finish_reason"] == "end_turn"


class TestResponsesApiToolRequest:
    """Responses route: FLAT tools, flat tool_choice, function_call input items."""

    def test_tools_are_flattened(self, monkeypatch):
        cap = _capture(monkeypatch, {"output": [], "usage": {}})
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="m", tools=[WEATHER_TOOL])
        _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")

        tool = cap["payload"]["tools"][0]
        # The nested Chat Completions form gets 400 "Invalid 'tools': missing
        # field `name`" here — name/parameters must be top-level.
        assert "function" not in tool
        assert tool["type"] == "function"
        assert tool["name"] == "get_weather"
        assert tool["parameters"]["required"] == ["city"]

    def test_tool_choice_dict_is_flattened(self, monkeypatch):
        cap = _capture(monkeypatch, {"output": [], "usage": {}})
        req = ChatCompletionRequest(
            messages=[{"role": "user", "content": "hi"}], model="m", tools=[WEATHER_TOOL],
            tool_choice={"type": "function", "function": {"name": "get_weather"}})
        _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")
        # Nested tool_choice is rejected: "value did not match any expected variant".
        assert cap["payload"]["tool_choice"] == {"type": "function", "name": "get_weather"}

    def test_tool_choice_string_passes_through(self, monkeypatch):
        cap = _capture(monkeypatch, {"output": [], "usage": {}})
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="m", tools=[WEATHER_TOOL], tool_choice="auto")
        _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")
        assert cap["payload"]["tool_choice"] == "auto"

    def test_tool_history_becomes_flat_input_items(self, monkeypatch):
        cap = _capture(monkeypatch, {"output": [], "usage": {}})
        req = ChatCompletionRequest(messages=OPENAI_TOOL_HISTORY, model="m",
                                    tools=[WEATHER_TOOL])
        _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")

        items = cap["payload"]["input"]
        # Tool traffic is sibling items, not fields on a message. Raw OpenAI
        # history gets 400 "Invalid 'input': value did not match any expected
        # variant" — content=null alone is enough to trigger it.
        assert items[0] == {"role": "user", "content": "weather in Paris?"}
        assert items[1] == {"type": "function_call", "call_id": "call_abc123",
                            "name": "get_weather", "arguments": '{"city":"Paris"}'}
        assert items[2] == {"type": "function_call_output", "call_id": "call_abc123",
                            "output": '{"temp_c":21}'}
        # No role message carries content=None (the tool-call items have no
        # content key at all, which is correct — hence the "role" in filter).
        assert not any(m["content"] is None for m in items if "role" in m)

    def test_single_user_turn_still_sent_as_bare_string(self, monkeypatch):
        # Pre-existing shorthand — kept so ordinary traffic is unchanged.
        cap = _capture(monkeypatch, {"output": [], "usage": {}})
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
        _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")
        assert cap["payload"]["input"] == "hi"

    def test_lone_tool_result_is_not_sent_as_bare_string(self, monkeypatch):
        # The shorthand only applies to a real user message; a single
        # function_call_output has no string form.
        cap = _capture(monkeypatch, {"output": [], "usage": {}})
        req = ChatCompletionRequest(
            messages=[{"role": "tool", "tool_call_id": "c1", "content": "{}"}], model="m")
        _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")
        assert isinstance(cap["payload"]["input"], list)
        assert cap["payload"]["input"][0]["type"] == "function_call_output"


class TestResponsesApiToolResponse:
    def test_function_call_output_items_become_tool_calls(self, monkeypatch):
        _capture(monkeypatch, {
            "id": "resp_1",
            "output": [{"type": "function_call", "call_id": "call_x",
                        "id": "fc_1", "name": "get_weather",
                        "arguments": '{"city":"Paris"}', "status": "completed"}],
            "status": "completed",
            "usage": {"input_tokens": 58, "output_tokens": 18},
        })
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="m", tools=[WEATHER_TOOL])
        res = _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")

        msg = res.choices[0]["message"]
        # call_id is what a function_call_output must echo back, so it — not the
        # item's own id — is the identifier callers need.
        assert msg["tool_calls"][0]["id"] == "call_x"
        assert msg["tool_calls"][0]["function"]["arguments"] == '{"city":"Paris"}'
        assert msg["content"] is None
        # This API reports lifecycle status; "completed" would end a tool loop.
        assert res.choices[0]["finish_reason"] == "tool_calls"

    def test_plain_response_keeps_status_as_finish_reason(self, monkeypatch):
        _capture(monkeypatch, {
            "output": [{"type": "message",
                        "content": [{"type": "output_text", "text": "hello"}]}],
            "status": "completed", "usage": {},
        })
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
        res = _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")
        assert res.choices[0]["message"] == {"role": "assistant", "content": "hello"}
        assert res.choices[0]["finish_reason"] == "completed"

    def test_reasoning_items_are_ignored(self, monkeypatch):
        # Live gpt-5.x emits a reasoning item before the answer.
        _capture(monkeypatch, {
            "output": [{"type": "reasoning", "id": "rs_1", "summary": []},
                       {"type": "message",
                        "content": [{"type": "output_text", "text": "hi there"}]}],
            "status": "completed", "usage": {},
        })
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
        res = _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")
        assert res.choices[0]["message"]["content"] == "hi there"
        assert "tool_calls" not in res.choices[0]["message"]


class TestChatCompletionsApiTools:
    """Chat Completions route: the gateway's own dialect — passthrough."""

    def test_tools_pass_through_unchanged(self, monkeypatch):
        cap = _capture(monkeypatch, {"choices": [], "usage": {}})
        req = ChatCompletionRequest(messages=OPENAI_TOOL_HISTORY, model="m",
                                    tools=[WEATHER_TOOL], tool_choice="auto")
        _invoke(mp._invoke_chat_completions_api, req, "openai.gpt-oss-120b")

        assert cap["payload"]["tools"] == [WEATHER_TOOL]
        assert cap["payload"]["tool_choice"] == "auto"
        # History needs no reshaping on this route.
        assert cap["payload"]["messages"] == OPENAI_TOOL_HISTORY

    def test_tool_calls_survive_the_response_rebuild(self, monkeypatch):
        # The response is rebuilt rather than forwarded, so tool_calls have to be
        # carried over explicitly even though no translation is needed.
        _capture(monkeypatch, {
            "id": "chatcmpl-1",
            "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
                "role": "assistant", "content": None, "tool_calls": [{
                    "id": "chatcmpl-tool-1", "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                }]}}],
            "usage": {"prompt_tokens": 134, "completion_tokens": 39, "total_tokens": 173},
        })
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="m", tools=[WEATHER_TOOL])
        res = _invoke(mp._invoke_chat_completions_api, req, "openai.gpt-oss-120b")

        msg = res.choices[0]["message"]
        assert msg["tool_calls"][0]["id"] == "chatcmpl-tool-1"
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
        assert msg["content"] is None
        assert res.choices[0]["finish_reason"] == "tool_calls"

    def test_no_tools_key_when_none_requested(self, monkeypatch):
        cap = _capture(monkeypatch, {"choices": [], "usage": {}})
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}], model="m")
        _invoke(mp._invoke_chat_completions_api, req, "openai.gpt-oss-120b")
        assert "tools" not in cap["payload"]
        assert "tool_choice" not in cap["payload"]


class TestToolTranslationEdgeCases:
    def test_tool_with_no_parameters_still_gets_a_schema(self, monkeypatch):
        # Both routes reject a tool with no schema, so an empty object is required
        # rather than an omitted key.
        bare = {"type": "function", "function": {"name": "ping"}}
        cap = _capture(monkeypatch, {"output": [], "usage": {}})
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="m", tools=[bare])
        _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")
        assert cap["payload"]["tools"][0]["parameters"] == {"type": "object", "properties": {}}

    def test_already_flat_tool_is_accepted(self, monkeypatch):
        # Not every caller of this gateway is OpenAI-native; failing a tool that
        # is already in the target shape would be a pure loss.
        flat = {"type": "function", "name": "ping", "parameters": {"type": "object"}}
        cap = _capture(monkeypatch, {"output": [], "usage": {}})
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="m", tools=[flat])
        _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")
        assert cap["payload"]["tools"][0]["name"] == "ping"

    def test_malformed_arguments_do_not_fail_the_request(self, monkeypatch):
        # A model can emit invalid JSON. On the Anthropic route that must degrade
        # to {} rather than raise, or one bad call takes down the whole request.
        history = [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "get_weather", "arguments": "{not json"},
        }]}]
        cap = _capture(monkeypatch, {"content": [], "usage": {}})
        req = ChatCompletionRequest(messages=history, model="m")
        _invoke(mp._invoke_messages_api, req, "anthropic.claude-sonnet-5")
        assert cap["payload"]["messages"][0]["content"][0]["input"] == {}

    def test_malformed_arguments_forwarded_verbatim_on_responses_route(self, monkeypatch):
        # This API wants the JSON string, so there is nothing to parse — passing
        # it through keeps the failure at the tool, not at the request.
        history = [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "get_weather", "arguments": "{not json"},
        }]}]
        cap = _capture(monkeypatch, {"output": [], "usage": {}})
        req = ChatCompletionRequest(messages=history, model="m")
        _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")
        assert cap["payload"]["input"][0]["arguments"] == "{not json"

    def test_dict_arguments_are_encoded_for_responses_route(self, monkeypatch):
        # A non-OpenAI-native caller may pass a parsed dict; the API needs a string.
        history = [{"role": "assistant", "content": None, "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "get_weather", "arguments": {"city": "Paris"}},
        }]}]
        cap = _capture(monkeypatch, {"output": [], "usage": {}})
        req = ChatCompletionRequest(messages=history, model="m")
        _invoke(mp._invoke_responses_api, req, "openai.gpt-5.6-sol")
        assert cap["payload"]["input"][0]["arguments"] == '{"city": "Paris"}'

    def test_tool_choice_none_is_omitted_on_anthropic_route(self, monkeypatch):
        # "none" has no Anthropic equivalent; sending it would be rejected.
        cap = _capture(monkeypatch, {"content": [], "usage": {}})
        req = ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                    model="m", tools=[WEATHER_TOOL], tool_choice="none")
        _invoke(mp._invoke_messages_api, req, "anthropic.claude-sonnet-5")
        assert "tool_choice" not in cap["payload"]
        assert "tools" in cap["payload"]
