#!/usr/bin/env python3
"""Simple demo client that sends chat requests through AxonLLM.

Sends a batch of requests across different models and users to generate
dashboard metrics. Points at the local AxonLLM gateway (not directly at Bedrock).

Usage:
    python scripts/demo_client.py
    python scripts/demo_client.py --base-url http://localhost:8000 --count 5
"""

import argparse
import json
import sys
import urllib.request
import urllib.error


def send_chat(base_url: str, model: str, message: str, user_id: str = "demo-user") -> dict:
    """Send a non-streaming chat request through AxonLLM."""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": message}],
        "user_id": user_id,
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return {"error": body, "status": e.code}
    except Exception as e:
        return {"error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="Send demo requests through AxonLLM")
    parser.add_argument("--base-url", default="http://localhost:8000", help="AxonLLM base URL")
    parser.add_argument("--count", type=int, default=3, help="Requests per model")
    args = parser.parse_args()

    # Models available in proj-alpha (Bedrock-backed, no API keys needed)
    scenarios = [
        ("claude-opus", "user-alice", "Explain quantum computing in one sentence."),
        ("claude-sonnet", "user-bob", "What is the capital of France?"),
        ("nova-pro", "user-alice", "Write a haiku about cloud computing."),
        ("nova-lite", "user-bob", "What is 2+2?"),
        ("deepseek-r1", "user-alice", "Explain why the sky is blue."),
    ]

    total = 0
    success = 0

    for i in range(args.count):
        for model, user, prompt in scenarios:
            total += 1
            print(f"[{total}] {model} ({user}): {prompt[:50]}...", end=" ")
            result = send_chat(args.base_url, model, prompt, user_id=user)

            if "error" in result:
                print(f"ERROR: {result.get('error', {})}")
            else:
                content = result.get("content", "")[:60]
                provider = result.get("provider", "?")
                print(f"OK ({provider}) → {content}...")
                success += 1

    print(f"\nDone: {success}/{total} succeeded")
    print(f"Check dashboard: {args.base_url}/admin/dashboard")


if __name__ == "__main__":
    main()
