"""The "-pro" tier is served only by /v1/responses, not Chat Completions.

`gpt-5.5-pro` was configured to route over `/v1/chat/completions`, where OpenAI
answers 400 "This is not a chat model and thus not supported in the
v1/chat/completions endpoint" — so it could never have worked. This is a class of
models rather than one bad config line (`gpt-5-pro` reports "This model is only
supported in v1/responses"), which is why detection is by tier suffix and lives
in code rather than being a deleted YAML entry.

The two easy ways to get this wrong are both asserted directly: diverting a
chat-capable model (`gpt-5`, `gpt-5.1`) onto the second path, and diverting a
`-pro`-looking id on an OpenAI-*compatible* provider that has no /v1/responses
route at all.
"""

from __future__ import annotations

import asyncio

import pytest

from src.gateway.adapters.openai_adapter import OpenAIAdapter
from src.gateway.adapters.openai_responses import (
    ResponsesStreamError,
    build_responses_payload,
    is_responses_only_model,
    translate_responses_reply,
    translate_responses_stream_event,
)
from src.gateway.adapters.xai_adapter import XAIAdapter
from src.gateway.models import ChatCompletionRequest, ProviderModelMapping
from src.gateway.provider_config import ProviderConfig, build_provider_url

_TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def _req(**kwargs) -> ChatCompletionRequest:
    kwargs.setdefault("model", "gpt-5.5-pro")
    kwargs.setdefault("messages", [{"role": "user", "content": "hi"}])
    return ChatCompletionRequest(**kwargs)


class TestModelDetection:
    @pytest.mark.parametrize("model_id", ["gpt-5.5-pro", "gpt-5-pro", "o3-pro"])
    def test_pro_tier_is_responses_only(self, model_id):
        assert is_responses_only_model(model_id) is True

    @pytest.mark.parametrize(
        "model_id", ["gpt-5", "gpt-5.1", "gpt-5.5", "gpt-4o", "gpt-4o-mini", "o3", "o4-mini"]
    )
    def test_chat_capable_models_are_not_diverted(self, model_id):
        """gpt-5 and gpt-5.1 accept Chat Completions; matching the family prefix
        instead of the -pro suffix would break models that already work."""
        assert is_responses_only_model(model_id) is False

    def test_empty_and_none_safe(self):
        assert is_responses_only_model("") is False
        assert is_responses_only_model(None) is False  # type: ignore[arg-type]

    def test_case_and_whitespace_tolerant(self):
        assert is_responses_only_model("  GPT-5.5-PRO  ") is True

    def test_substring_pro_does_not_match(self):
        """"pro" inside a word is not the -pro tier."""
        assert is_responses_only_model("gpt-4-prometheus") is False
        assert is_responses_only_model("provider-model") is False


class TestUrlRouting:
    def _cfg(self, name: str, base: str) -> ProviderConfig:
        return ProviderConfig(
            provider_name=name, base_url=base, auth_type="api_key",
            credentials={"api_key": "k"},
        )

    def _url(self, name: str, base: str, model_id: str) -> str:
        return build_provider_url(
            self._cfg(name, base),
            ProviderModelMapping(provider=name, model_id=model_id),
        )

    def test_pro_model_routes_to_responses(self):
        url = self._url("openai", "https://api.openai.com", "gpt-5.5-pro")
        assert url == "https://api.openai.com/v1/responses"

    def test_chat_model_stays_on_chat_completions(self):
        url = self._url("openai", "https://api.openai.com", "gpt-4o")
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_openai_compatible_provider_never_routes_to_responses(self):
        """xAI/Groq/Together/Fireworks/AI21 share the OpenAI URL builder but have
        no /v1/responses route — sending one there would 404 a working request.

        Uses a model id that *does* satisfy is_responses_only_model, so the guard
        being tested is the provider-name check. An id like "grok-3-pro" would
        pass this test even with the guard removed, since it isn't a gpt-*/o[134]
        family name and never matches in the first place.
        """
        assert is_responses_only_model("gpt-5.5-pro") is True  # guard the premise
        for provider, base in [
            ("xai", "https://api.x.ai"),
            ("groq", "https://api.groq.com/openai"),
            ("together", "https://api.together.xyz"),
            ("fireworks", "https://api.fireworks.ai/inference"),
            ("ai21", "https://api.ai21.com/studio"),
        ]:
            assert self._url(provider, base, "gpt-5.5-pro").endswith("/v1/chat/completions")


class TestRequestPayload:
    def test_sampling_params_are_dropped(self):
        """These models reject temperature/top_p with a 400 rather than ignoring
        them, so forwarding a caller's default would fail every request and trip
        the provider's circuit breaker."""
        payload = build_responses_payload(_req(temperature=0.7, top_p=0.9), "gpt-5.5-pro")
        assert "temperature" not in payload
        assert "top_p" not in payload

    def test_max_tokens_becomes_max_output_tokens(self):
        payload = build_responses_payload(_req(max_tokens=256), "gpt-5.5-pro")
        assert payload["max_output_tokens"] == 256
        assert "max_tokens" not in payload

    def test_lone_user_turn_sent_as_bare_string(self):
        payload = build_responses_payload(_req(), "gpt-5.5-pro")
        assert payload["input"] == "hi"

    def test_system_message_becomes_instructions(self):
        payload = build_responses_payload(
            _req(messages=[{"role": "system", "content": "be terse"},
                           {"role": "user", "content": "hi"}]),
            "gpt-5.5-pro",
        )
        assert payload["instructions"] == "be terse"
        assert payload["input"] == "hi"

    def test_tools_are_flattened(self):
        """Chat Completions nests under `function`; the Responses API puts
        name/parameters at the top level and rejects the nested form."""
        payload = build_responses_payload(_req(tools=[_TOOL]), "gpt-5.5-pro")
        spec = payload["tools"][0]
        assert spec["type"] == "function"
        assert spec["name"] == "get_weather"
        assert "function" not in spec
        assert spec["parameters"]["properties"] == {"city": {"type": "string"}}

    def test_tool_choice_flattened(self):
        payload = build_responses_payload(
            _req(tools=[_TOOL],
                 tool_choice={"type": "function", "function": {"name": "get_weather"}}),
            "gpt-5.5-pro",
        )
        assert payload["tool_choice"] == {"type": "function", "name": "get_weather"}

    def test_no_tools_means_no_keys(self):
        payload = build_responses_payload(_req(), "gpt-5.5-pro")
        assert "tools" not in payload
        assert "tool_choice" not in payload

    def test_tool_result_history_becomes_function_call_items(self):
        """Tool traffic is top-level items here, not messages — raw OpenAI
        history gets 400 Invalid 'input'."""
        payload = build_responses_payload(
            _req(messages=[
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]},
                {"role": "tool", "tool_call_id": "call_1", "content": "18C"},
            ]),
            "gpt-5.5-pro",
        )
        types = [i.get("type") for i in payload["input"]]
        assert "function_call" in types
        assert "function_call_output" in types

    def test_stream_flag_set_without_stream_options(self):
        """stream_options is a Chat Completions field; the Responses API rejects it."""
        payload = build_responses_payload(_req(stream=True), "gpt-5.5-pro")
        assert payload["stream"] is True
        assert "stream_options" not in payload


class TestResponseTranslation:
    def _reply(self, **kwargs) -> dict:
        base = {
            "id": "resp_1", "object": "response", "status": "completed",
            "model": "gpt-5.5-pro",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "hello"}]}],
            "usage": {"input_tokens": 3, "output_tokens": 5, "total_tokens": 8},
        }
        base.update(kwargs)
        return base

    def test_text_and_usage(self):
        r = translate_responses_reply(self._reply(), "openai")
        assert r.choices[0]["message"]["content"] == "hello"
        assert r.choices[0]["finish_reason"] == "stop"
        assert (r.usage.prompt_tokens, r.usage.completion_tokens) == (3, 5)

    def test_reasoning_items_contribute_no_text(self):
        """Reasoning content is encrypted and not user-visible; including it
        would emit ciphertext as the assistant's answer."""
        r = translate_responses_reply(
            self._reply(output=[
                {"type": "reasoning", "content": [], "encrypted_content": "gAAAA..."},
                {"type": "message", "content": [{"type": "output_text", "text": "hi"}]},
            ]), "openai")
        assert r.choices[0]["message"]["content"] == "hi"

    def test_tool_call_extracted_with_content_null(self):
        r = translate_responses_reply(
            self._reply(output=[{"type": "function_call", "call_id": "call_1",
                                 "name": "get_weather", "arguments": '{"city":"Paris"}'}]),
            "openai")
        msg = r.choices[0]["message"]
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
        assert msg["tool_calls"][0]["function"]["arguments"] == '{"city":"Paris"}'
        assert msg["tool_calls"][0]["id"] == "call_1"
        assert msg["content"] is None

    def test_tool_call_wins_over_lifecycle_status(self):
        """status is "completed" even when the model called a tool. A caller
        driving a tool loop reads that as "nothing left to do" and never runs it."""
        r = translate_responses_reply(
            self._reply(status="completed",
                        output=[{"type": "function_call", "call_id": "c",
                                 "name": "f", "arguments": "{}"}]),
            "openai")
        assert r.choices[0]["finish_reason"] == "tool_calls"

    def test_text_alongside_tool_call_keeps_content(self):
        r = translate_responses_reply(
            self._reply(output=[
                {"type": "message", "content": [{"type": "output_text", "text": "checking"}]},
                {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"},
            ]), "openai")
        msg = r.choices[0]["message"]
        assert msg["content"] == "checking"
        assert msg["tool_calls"]

    def test_incomplete_max_output_tokens_maps_to_length(self):
        r = translate_responses_reply(
            self._reply(status="incomplete",
                        incomplete_details={"reason": "max_output_tokens"}),
            "openai")
        assert r.choices[0]["finish_reason"] == "length"

    def test_incomplete_content_filter_maps_to_content_filter(self):
        r = translate_responses_reply(
            self._reply(status="incomplete", incomplete_details={"reason": "content_filter"}),
            "openai")
        assert r.choices[0]["finish_reason"] == "content_filter"

    def test_incomplete_unknown_reason_defaults_to_length(self):
        r = translate_responses_reply(
            self._reply(status="incomplete", incomplete_details={"reason": "who_knows"}),
            "openai")
        assert r.choices[0]["finish_reason"] == "length"

    def test_empty_output_does_not_crash(self):
        r = translate_responses_reply(self._reply(output=[]), "openai")
        assert r.choices[0]["message"]["content"] == ""

    def test_provider_name_recorded(self):
        assert translate_responses_reply(self._reply(), "openai").provider == "openai"


class TestStreamTranslation:
    def test_text_delta(self):
        chunk = translate_responses_stream_event(
            {"type": "response.output_text.delta", "delta": "abc", "item_id": "msg_1"})
        assert chunk.choices[0]["delta"]["content"] == "abc"
        assert chunk.is_final is False

    def test_empty_delta_ignored(self):
        assert translate_responses_stream_event(
            {"type": "response.output_text.delta", "delta": ""}) is None

    def test_lifecycle_events_ignored(self):
        for t in ("response.created", "response.in_progress",
                  "response.content_part.added", "response.content_part.done",
                  "response.output_text.done", "response.output_item.added"):
            assert translate_responses_stream_event({"type": t}) is None

    def test_function_call_arrives_whole_on_output_item_done(self):
        """No cross-chunk accumulation needed, unlike the hand-built translators:
        output_item.done carries call_id, name and the complete arguments string,
        so the arguments are never split into unparseable fragments."""
        chunk = translate_responses_stream_event({
            "type": "response.output_item.done",
            "item": {"type": "function_call", "id": "fc_1", "call_id": "call_1",
                     "name": "get_weather", "arguments": '{"city":"Paris"}'},
        })
        tc = chunk.choices[0]["delta"]["tool_calls"][0]
        assert tc["id"] == "call_1"
        assert tc["function"]["name"] == "get_weather"
        assert tc["function"]["arguments"] == '{"city":"Paris"}'
        assert chunk.choices[0]["delta"]["content"] is None

    def test_arguments_delta_event_ignored(self):
        """function_call_arguments.delta duplicates data that also arrives whole
        on output_item.done; emitting both would call the tool twice."""
        assert translate_responses_stream_event({
            "type": "response.function_call_arguments.delta",
            "delta": '{"city":', "item_id": "fc_1"}) is None

    def test_arguments_done_event_ignored(self):
        """Same duplication, and this one carries the *complete* arguments — so
        treating it like output_item.done would emit the tool call twice and a
        client appending each delta would invoke the tool twice.

        The payload mirrors the real event, which has `arguments` at the top level
        and no `item` key; a stub without those fields would pass even if this
        event were wrongly handled.
        """
        assert translate_responses_stream_event({
            "type": "response.function_call_arguments.done",
            "arguments": '{"city":"Paris"}', "item_id": "fc_1",
            "output_index": 1, "sequence_number": 6}) is None

    def test_message_item_done_ignored(self):
        """The text already arrived as deltas; re-emitting it would duplicate
        the whole response."""
        assert translate_responses_stream_event({
            "type": "response.output_item.done",
            "item": {"type": "message", "content": [
                {"type": "output_text", "text": "hello"}]}}) is None

    def test_completed_carries_finish_reason_and_usage(self):
        chunk = translate_responses_stream_event({
            "type": "response.completed",
            "response": {"id": "resp_1", "status": "completed", "model": "gpt-5.5-pro",
                         "output": [],
                         "usage": {"input_tokens": 2, "output_tokens": 4, "total_tokens": 6}},
        })
        assert chunk.is_final is True
        assert chunk.choices[0]["finish_reason"] == "stop"
        assert chunk.usage.total_tokens == 6

    def test_completed_with_tool_call_reports_tool_calls(self):
        chunk = translate_responses_stream_event({
            "type": "response.completed",
            "response": {"id": "r", "status": "completed", "output": [
                {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"}]},
        })
        assert chunk.choices[0]["finish_reason"] == "tool_calls"

    def test_incomplete_event_is_final_with_length(self):
        chunk = translate_responses_stream_event({
            "type": "response.incomplete",
            "response": {"id": "r", "status": "incomplete",
                         "incomplete_details": {"reason": "max_output_tokens"},
                         "output": []},
        })
        assert chunk.is_final is True
        assert chunk.choices[0]["finish_reason"] == "length"

    def test_failed_event_raises(self):
        """An empty stream that ends cleanly is indistinguishable from a short
        answer, so a failure has to surface as an error."""
        with pytest.raises(ResponsesStreamError, match="boom"):
            translate_responses_stream_event({
                "type": "response.failed",
                "response": {"error": {"message": "boom"}}})

    def test_unknown_event_type_ignored(self):
        assert translate_responses_stream_event({"type": "response.something_new"}) is None


class TestAdapterDispatch:
    def test_openai_adapter_uses_responses_payload_for_pro(self):
        payload = asyncio.run(OpenAIAdapter().translate_request(_req(temperature=0.5)))
        assert "input" in payload
        assert "messages" not in payload
        assert "temperature" not in payload

    def test_openai_adapter_uses_chat_payload_for_normal_model(self):
        payload = asyncio.run(
            OpenAIAdapter().translate_request(_req(model="gpt-4o", temperature=0.5)))
        assert "messages" in payload
        assert "input" not in payload
        assert payload["temperature"] == 0.5

    def test_compatible_provider_never_uses_responses_payload(self):
        """xAI has no /v1/responses route, so even an id that looks responses-only
        must stay on the Chat Completions payload or the request 404s.

        Deliberately uses "gpt-5.5-pro" rather than a grok id: the point under
        test is the per-provider opt-in, and a grok id never matches the detector
        so it would pass even with _SUPPORTS_RESPONSES_API flipped on.
        """
        assert is_responses_only_model("gpt-5.5-pro") is True  # guard the premise
        payload = asyncio.run(
            XAIAdapter().translate_request(_req(model="gpt-5.5-pro", temperature=0.5)))
        assert "messages" in payload
        assert "input" not in payload
        assert payload["temperature"] == 0.5

    def test_response_shape_detected_without_model_id(self):
        """translate_response never receives the model id, so it keys on the
        payload; mistaking one shape for the other silently empties the reply."""
        adapter = OpenAIAdapter()
        responses_reply = {
            "id": "r", "object": "response", "status": "completed", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "hi"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        assert adapter.translate_response(responses_reply).choices[0]["message"]["content"] == "hi"

        chat_reply = {
            "id": "c", "object": "chat.completion", "model": "gpt-4o",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "yo"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        assert adapter.translate_response(chat_reply).choices[0]["message"]["content"] == "yo"

    def test_stream_event_dispatch_by_type(self):
        adapter = OpenAIAdapter()
        chunk = adapter.translate_stream_chunk(
            {"type": "response.output_text.delta", "delta": "x", "item_id": "m"})
        assert chunk.choices[0]["delta"]["content"] == "x"

    def test_ignored_stream_event_yields_empty_choices(self):
        """agent.py drops chunks with empty choices, so a lifecycle event
        becomes a no-op rather than an empty SSE frame reaching the client."""
        chunk = OpenAIAdapter().translate_stream_chunk({"type": "response.created"})
        assert chunk.choices == []

    def test_chat_completions_stream_chunk_still_works(self):
        chunk = OpenAIAdapter().translate_stream_chunk({
            "id": "c", "model": "gpt-4o",
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]})
        assert chunk.choices[0]["delta"]["content"] == "hi"
        assert chunk.is_final is False
