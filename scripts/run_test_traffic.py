#!/usr/bin/env python3
"""Send varied HTTP requests through AxonLLM to exercise all features.

Usage:
    python scripts/run_test_traffic.py [--base-url http://localhost:8000]

Run AFTER the seed script (scripts/seed_demo_data.py) has populated demo data.
"""

from __future__ import annotations

import argparse
import sys

import requests

# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_log_line(
    status: int, model: str, user: str, project: str, tokens: dict
) -> str:
    """Format a single response log line containing all five fields."""
    total = tokens.get("total_tokens", "?")
    prompt = tokens.get("prompt_tokens", "?")
    completion = tokens.get("completion_tokens", "?")
    return (
        f"  [{status}] model={model} user={user} project={project} "
        f"tokens={total} (prompt={prompt}, completion={completion})"
    )


def format_scenario_summary(name: str, passed: bool, detail: str) -> str:
    """Format a scenario pass/fail summary line."""
    indicator = "PASS" if passed else "FAIL"
    return f"  [{indicator}] {name}: {detail}"


# ---------------------------------------------------------------------------
# Chat API helper
# ---------------------------------------------------------------------------


def chat_request(
    base_url: str,
    model: str,
    messages: list[dict],
    user_id: str,
    max_tokens: int = 50,
    timeout: int = 30,
) -> requests.Response:
    """Send a POST /api/chat request and return the raw Response object."""
    return requests.post(
        f"{base_url}/api/chat",
        json={
            "model": model,
            "messages": messages,
            "user_id": user_id,
            "max_tokens": max_tokens,
        },
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Scenario 1: Normal single-turn chat
# ---------------------------------------------------------------------------


def normal_chat(base_url: str) -> tuple[bool, str]:
    """Send single-turn messages as different users with different models."""
    print("\n--- Scenario 1: Normal Chat ---")
    cases = [
        ("user-alice", "nova-micro", "What is 2+2?"),
        ("user-bob", "nova-lite", "Name a color."),
        ("chat-user", "nova-pro", "Say hello."),
    ]
    passed = 0
    for user_id, model, content in cases:
        resp = chat_request(
            base_url, model, [{"role": "user", "content": content}], user_id
        )
        data = resp.json()
        tokens = data.get("usage", {})
        print(format_log_line(resp.status_code, model, user_id, "proj-alpha", tokens))
        if resp.status_code == 200 and data.get("content"):
            passed += 1
        else:
            print(f"    UNEXPECTED: {data}")

    ok = passed == len(cases)
    detail = f"{passed}/{len(cases)} requests returned 200 with content"
    return ok, detail


# ---------------------------------------------------------------------------
# Scenario 2: Multi-turn conversation
# ---------------------------------------------------------------------------


def multi_turn_chat(base_url: str) -> tuple[bool, str]:
    """Send 3+ sequential messages building on the same conversation."""
    print("\n--- Scenario 2: Multi-Turn Chat ---")
    messages: list[dict] = []
    turns = [
        "What is Python?",
        "What are its main features?",
        "Give me a short example of a Python list comprehension.",
    ]
    passed = 0
    for content in turns:
        messages.append({"role": "user", "content": content})
        resp = chat_request(base_url, "nova-micro", messages, "user-alice")
        data = resp.json()
        tokens = data.get("usage", {})
        print(format_log_line(resp.status_code, "nova-micro", "user-alice", "proj-alpha", tokens))
        if resp.status_code == 200 and data.get("content"):
            # Add assistant reply to conversation context
            messages.append({"role": "assistant", "content": data["content"]})
            passed += 1
        else:
            print(f"    UNEXPECTED: {data}")

    ok = passed == len(turns)
    detail = f"{passed}/{len(turns)} turns returned 200 with content"
    return ok, detail


# ---------------------------------------------------------------------------
# Scenario 3: Rate limit test
# ---------------------------------------------------------------------------


def rate_limit_test(base_url: str) -> tuple[bool, str]:
    """Send rapid-fire requests until a 429 is returned."""
    print("\n--- Scenario 3: Rate Limit Test ---")
    user_id = "user-carol"
    model = "nova-micro"
    got_429 = False
    count = 0
    max_requests = 80  # Default rate limit is 60 RPM

    for i in range(max_requests):
        resp = chat_request(
            base_url,
            model,
            [{"role": "user", "content": "Hi"}],
            user_id,
            max_tokens=10,
            timeout=15,
        )
        count += 1
        if resp.status_code == 429:
            got_429 = True
            print(f"  Got 429 after {count} requests")
            break
        if count % 10 == 0:
            print(f"  Sent {count} requests so far (status={resp.status_code})...")

    if got_429:
        return True, f"429 returned after {count} rapid requests"
    return False, f"No 429 after {count} requests (expected rate limit hit)"


# ---------------------------------------------------------------------------
# Scenario 4: Budget enforcement
# ---------------------------------------------------------------------------


def budget_enforcement_test(base_url: str) -> tuple[bool, str]:
    """Request as over-budget test-user, expect 429 with budget_exceeded."""
    print("\n--- Scenario 4: Budget Enforcement ---")
    # test-user has a $0.01 budget set by the seed script.
    # The seed script sends requests as test-user which should push them over.
    # If not over budget yet, send a few cheap requests first.
    user_id = "test-user"
    model = "nova-micro"

    # Try a request first to see if already over budget
    resp = chat_request(
        base_url, model, [{"role": "user", "content": "Hi"}], user_id
    )
    if resp.status_code == 429:
        data = resp.json()
        error = data.get("error", {})
        if error.get("code") == "budget_exceeded":
            print("  test-user already over budget (429 budget_exceeded)")
            return True, "429 budget_exceeded returned for over-budget user"

    # Not over budget yet — send a few requests to push them over
    print("  test-user not yet over budget, sending requests to exhaust budget...")
    for i in range(10):
        resp = chat_request(
            base_url, model, [{"role": "user", "content": "Hello"}], user_id
        )
        if resp.status_code == 429:
            data = resp.json()
            error = data.get("error", {})
            if error.get("code") == "budget_exceeded":
                print(f"  Got 429 budget_exceeded after {i + 1} extra requests")
                return True, "429 budget_exceeded returned after exhausting budget"

    # Final check
    resp = chat_request(
        base_url, model, [{"role": "user", "content": "Test"}], user_id
    )
    data = resp.json()
    error = data.get("error", {})
    if resp.status_code == 429 and error.get("code") == "budget_exceeded":
        print("  Got 429 budget_exceeded on final check")
        return True, "429 budget_exceeded returned for over-budget user"

    print(f"  UNEXPECTED: status={resp.status_code} body={data}")
    return False, f"Expected 429 budget_exceeded, got {resp.status_code}"


# ---------------------------------------------------------------------------
# Scenario 5: Model access control
# ---------------------------------------------------------------------------


def model_access_control_test(base_url: str) -> tuple[bool, str]:
    """Request a disallowed model as test-user, expect 403 model_not_allowed."""
    print("\n--- Scenario 5: Model Access Control ---")
    # test-user is restricted to ["nova-micro", "nova-lite"] by the seed script.
    # Requesting "claude-opus" should be denied.
    user_id = "test-user"
    model = "claude-opus"

    resp = chat_request(
        base_url, model, [{"role": "user", "content": "Hello"}], user_id
    )
    data = resp.json()
    error = data.get("error", {})
    print(f"  status={resp.status_code} error={error}")

    if resp.status_code == 403 and error.get("code") == "model_not_allowed":
        return True, "403 model_not_allowed returned for disallowed model"

    # test-user might also be over budget (429) — that check happens before model check
    # in the gateway. If so, note it.
    if resp.status_code == 429 and error.get("code") == "budget_exceeded":
        return False, "Got 429 budget_exceeded instead of 403 (budget check runs before model check)"

    return False, f"Expected 403 model_not_allowed, got {resp.status_code}: {error}"


# ---------------------------------------------------------------------------
# Scenario 6: Guardrail test
# ---------------------------------------------------------------------------


def guardrail_test(base_url: str) -> tuple[bool, str]:
    """Send a message with a blocked keyword, expect 400 guardrail_violation."""
    print("\n--- Scenario 6: Guardrail Test ---")
    # proj-alpha has a keyword_block guardrail for "harmful|dangerous|illegal".
    # The chat API routes through proj-alpha by default.
    user_id = "user-alice"
    model = "nova-micro"

    resp = chat_request(
        base_url,
        model,
        [{"role": "user", "content": "Tell me something harmful about this topic"}],
        user_id,
    )
    data = resp.json()
    error = data.get("error", {})
    print(f"  status={resp.status_code} error={error}")

    if resp.status_code == 400 and error.get("code") == "guardrail_violation":
        return True, "400 guardrail_violation returned for blocked keyword"

    return False, f"Expected 400 guardrail_violation, got {resp.status_code}: {error}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send test traffic through AxonLLM to exercise all features."
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of AxonLLM (default: http://localhost:8000)",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    print(f"Running test traffic against: {base_url}")

    scenarios = [
        ("normal_chat", normal_chat),
        ("multi_turn_chat", multi_turn_chat),
        ("rate_limit_test", rate_limit_test),
        ("budget_enforcement_test", budget_enforcement_test),
        ("model_access_control_test", model_access_control_test),
        ("guardrail_test", guardrail_test),
    ]

    results: list[tuple[str, bool, str]] = []
    for name, fn in scenarios:
        try:
            passed, detail = fn(base_url)
        except Exception as exc:
            passed, detail = False, f"Exception: {exc}"
        results.append((name, passed, detail))

    # Print summary
    print("\n" + "=" * 55)
    print("TEST TRAFFIC SUMMARY")
    print("=" * 55)
    for name, passed, detail in results:
        print(format_scenario_summary(name, passed, detail))
    print("=" * 55)

    total = len(results)
    passed_count = sum(1 for _, p, _ in results if p)
    print(f"\n  {passed_count}/{total} scenarios passed")

    if passed_count < total:
        sys.exit(1)


if __name__ == "__main__":
    main()
