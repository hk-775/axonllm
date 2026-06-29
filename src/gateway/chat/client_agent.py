"""ClientAgent — thin translation layer between HTTP-oriented dicts and GatewayAgent."""

from __future__ import annotations

from typing import Any, AsyncIterator


class ClientAgent:
    """Translates between the Chat API's HTTP format and GatewayAgent's internal API."""

    def __init__(
        self,
        gateway_agent: Any,
        default_user_id: str = "chat-user",
        default_project_id: str = "chat-project",
    ) -> None:
        self.gateway_agent = gateway_agent
        self.default_user_id = default_user_id
        self.default_project_id = default_project_id

    async def list_models(self, project_id: str | None = None, user_id: str | None = None) -> list[dict]:
        """Return available models filtered by access context.

        Passes project_id and user_id through to GatewayAgent.handle_list_models
        so that only models the caller is permitted to use are returned.
        """
        pid = project_id or self.default_project_id
        uid = user_id or self.default_user_id
        result = await self.gateway_agent.handle_list_models(project_id=pid, user_id=uid)
        return result.get("models", [])

    async def chat(
        self,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        user_id: str | None = None,
        provider: str | None = None,
        smart_routing: bool = False,
    ) -> dict:
        """Non-streaming chat completion. Returns simplified response dict."""
        request_data = self._build_request_data(
            model, messages, stream=False,
            temperature=temperature, max_tokens=max_tokens,
        )
        context = self._build_context(user_id=user_id, provider=provider, smart_routing=smart_routing)
        response = await self.gateway_agent.handle_chat_completion(request_data, context)

        # Extract rate limit headers to pass through
        rate_limit_headers = response.pop("_rate_limit_headers", None)

        if "error" in response:
            result: dict[str, Any] = {
                "error": response["error"],
                "status_code": response.get("status_code", 500),
            }
            if rate_limit_headers:
                result["_rate_limit_headers"] = rate_limit_headers
            return result

        result = {
            "id": response["id"],
            "model": response["model"],
            "provider": response["provider"],
            "content": response["choices"][0]["message"]["content"],
            "usage": response["usage"],
        }
        # Include smart_routing metadata if present
        if "smart_routing" in response:
            result["smart_routing"] = response["smart_routing"]
        # Include ensemble metadata if present
        if "ensemble" in response:
            result["ensemble"] = response["ensemble"]
        # Include backward-compat note when ensemble was requested but unavailable
        if "ensemble_unavailable" in response:
            result["ensemble_unavailable"] = response["ensemble_unavailable"]
        if rate_limit_headers:
            result["_rate_limit_headers"] = rate_limit_headers
        return result

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float | None = None,
        max_tokens: int | None = None,
        user_id: str | None = None,
        provider: str | None = None,
    ) -> AsyncIterator[dict]:
        """Streaming chat completion. Yields chunk dicts."""
        request_data = self._build_request_data(
            model, messages, stream=True,
            temperature=temperature, max_tokens=max_tokens,
        )
        context = self._build_context(user_id=user_id, provider=provider)
        result = await self.gateway_agent.handle_chat_completion(request_data, context)

        # If the gateway returned an error dict directly (e.g. rate limit)
        if isinstance(result, dict) and "error" in result:
            chunk: dict[str, Any] = {
                "error": result["error"],
                "status_code": result.get("status_code", 500),
            }
            # Pass through rate limit headers
            if "_rate_limit_headers" in result:
                chunk["_rate_limit_headers"] = result["_rate_limit_headers"]
            yield chunk
            return

        # result is an async iterator of chunks
        async for chunk in result:
            # Pass through rate limit headers metadata chunk
            if "_rate_limit_headers" in chunk:
                yield {"_rate_limit_headers": chunk["_rate_limit_headers"]}
                continue

            data = chunk.get("data")
            if data is None:
                continue

            # "[DONE]" sentinel
            if data == "[DONE]":
                yield {"done": True}
                return

            # Error during streaming
            if isinstance(data, dict) and "error" in data:
                yield {"error": data["error"]}
                continue

            # Normal content chunk
            content = ""
            choices = data.get("choices", [])
            if choices:
                delta = choices[0].get("delta", {})
                content = delta.get("content", "")

            yield {
                "id": data.get("id", ""),
                "model": data.get("model", ""),
                "content": content,
                "is_final": data.get("is_final", False),
            }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_request_data(
        self,
        model: str,
        messages: list[dict],
        stream: bool,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict:
        request_data: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if temperature is not None:
            request_data["temperature"] = temperature
        if max_tokens is not None:
            request_data["max_tokens"] = max_tokens
        return request_data

    def _build_context(self, user_id: str | None = None, provider: str | None = None, smart_routing: bool = False) -> dict:
        ctx: dict[str, Any] = {
            "user_id": user_id or self.default_user_id,
            "project_id": self.default_project_id,
        }
        if provider:
            ctx["provider"] = provider
        if smart_routing:
            ctx["smart_routing"] = True
        return ctx

    def get_available_users(self) -> list[str]:
        """Return list of known user IDs from the cost tracker."""
        records = self.gateway_agent.cost_tracker._records
        user_ids = sorted({r.user_id for r in records})
        # Also include users from user_configs that may not have records yet
        for uid in self.gateway_agent._user_configs:
            if uid not in user_ids:
                user_ids.append(uid)
        # Always include the default user
        if self.default_user_id not in user_ids:
            user_ids.append(self.default_user_id)
        return sorted(set(user_ids))
