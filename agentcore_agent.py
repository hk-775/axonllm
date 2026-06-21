"""AgentCore entrypoint for deploying AxonLLM on Bedrock AgentCore Runtime.

This file wraps the GatewayAgent with the real BedrockAgentCoreApp SDK.
Use this as the --entrypoint when running `agentcore configure`.

Local dev: python serve_dashboard.py (uses the stub, no SDK needed)
AgentCore: agentcore configure --entrypoint agentcore_agent.py
"""

from bedrock_agentcore.runtime import BedrockAgentCoreApp

from src.gateway.agent import GatewayAgent, _error_response
from src.gateway.bootstrap import build_gateway_agent
from src.gateway.config_loader import load_app_config

app = BedrockAgentCoreApp()

# ---------------------------------------------------------------------------
# Bootstrap the gateway agent
# ---------------------------------------------------------------------------

_agent: GatewayAgent | None = None


def _get_agent() -> GatewayAgent:
    """Lazy-initialize the GatewayAgent singleton."""
    global _agent
    if _agent is not None:
        return _agent

    app_config = load_app_config()
    _agent = build_gateway_agent(app_config)
    return _agent


# ---------------------------------------------------------------------------
# AgentCore entrypoint
# ---------------------------------------------------------------------------


@app.entrypoint
async def invoke(payload, context):
    """Main AgentCore entrypoint for chat completions.

    Payload format:
    {
        "action": "chat" | "list_models" | "health",
        "model": "claude-opus",
        "messages": [{"role": "user", "content": "Hello"}],
        "user_id": "user-123",
        "project_id": "default",
        "temperature": 0.7,
        "max_tokens": 1024,
        "stream": false
    }
    """
    agent = _get_agent()

    action = payload.get("action", "chat")

    if action == "list_models":
        result = await agent.handle_list_models(
            project_id=payload.get("project_id"),
            user_id=payload.get("user_id"),
        )
        return result

    if action == "health":
        return await agent.handle_health_check()

    # Default: chat completion
    request_data = {
        "model": payload.get("model", ""),
        "messages": payload.get("messages", []),
        "stream": payload.get("stream", False),
    }
    if "temperature" in payload:
        request_data["temperature"] = payload["temperature"]
    if "max_tokens" in payload:
        request_data["max_tokens"] = payload["max_tokens"]

    ctx = {
        "user_id": payload.get("user_id", "agentcore-user"),
        "project_id": payload.get("project_id", "default"),
    }

    result = await agent.handle_chat_completion(request_data, ctx)

    # If streaming, collect chunks into a single response
    if hasattr(result, "__aiter__"):
        chunks = []
        async for chunk in result:
            data = chunk.get("data")
            if data and data != "[DONE]" and isinstance(data, dict) and "error" not in data:
                choices = data.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        chunks.append(content)
        return {"content": "".join(chunks)}

    return result


if __name__ == "__main__":
    app.run()
