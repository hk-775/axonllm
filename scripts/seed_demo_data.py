#!/usr/bin/env python3
"""Seed AxonLLM with realistic demo data via the Admin API.

Usage:
    python scripts/seed_demo_data.py [--base-url http://localhost:8000]

Creates projects, users, usage records (via real chat requests), and Cedar policies.
"""

from __future__ import annotations

import argparse
import random
import sys
import time

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ALL_MODELS = [
    "claude-opus",
    "nova-pro",
    "nova-lite",
    "nova-micro",
    "deepseek-r1",
]

CHEAP_MODELS = ["nova-micro", "nova-lite"]

PROJECTS = [
    {
        "project_id": "proj-alpha",
        "name": "Alpha Team",
        "budget_limit": 500,
        "alert_threshold": 400,
        "allowed_models": ALL_MODELS,
        "cache_enabled": True,
        "members": ["user-alice", "user-bob", "chat-user", "test-user"],
        "guardrail_rules": [
            {
                "name": "block-harmful",
                "rule_type": "keyword_block",
                "pattern": "harmful|dangerous|illegal",
                "action": "block",
                "applies_to": "both",
            }
        ],
    },
    {
        "project_id": "proj-beta",
        "name": "Beta Research",
        "budget_limit": 200,
        "alert_threshold": 150,
        "members": ["user-carol"],
    },
]

USER_BUDGETS = [
    {"user_id": "user-alice", "budget_limit": 50, "alert_threshold": 40},
    {"user_id": "user-bob", "budget_limit": 100, "alert_threshold": 80},
    {"user_id": "user-carol", "budget_limit": 75, "alert_threshold": 60},
    {"user_id": "chat-user", "budget_limit": 200, "alert_threshold": 150},
    {"user_id": "test-user", "budget_limit": 0.01, "alert_threshold": 0.005},
]

RESTRICTED_USERS = [
    {"user_id": "test-user", "allowed_models": ["nova-micro", "nova-lite"]},
]

POLICIES = [
    {
        "name": "allow-all-read",
        "description": "Allow all principals to perform read actions",
        "mode": "ENFORCE",
        "policy_text": 'permit(principal, action == Action::"read", resource);',
    },
    {
        "name": "restrict-expensive-models",
        "description": "Restrict expensive model usage to senior roles (log only)",
        "mode": "LOG_ONLY",
        "policy_text": 'forbid(principal, action, resource) unless { principal.role == "senior" };',
    },
]

# Short prompts across different topics to keep costs low
CHAT_PROMPTS = [
    # Coding
    {"topic": "coding", "message": "What is a Python list comprehension?"},
    {"topic": "coding", "message": "Explain the difference between == and is in Python."},
    {"topic": "coding", "message": "What does async/await do?"},
    {"topic": "coding", "message": "How do you handle exceptions in Python?"},
    {"topic": "coding", "message": "What is a decorator in Python?"},
    # Science
    {"topic": "science", "message": "What is photosynthesis?"},
    {"topic": "science", "message": "How does gravity work?"},
    {"topic": "science", "message": "What is DNA?"},
    {"topic": "science", "message": "Explain the water cycle briefly."},
    {"topic": "science", "message": "What causes lightning?"},
    # History
    {"topic": "history", "message": "Who invented the telephone?"},
    {"topic": "history", "message": "When was the internet created?"},
    {"topic": "history", "message": "What was the Renaissance?"},
    # Math
    {"topic": "math", "message": "What is the Pythagorean theorem?"},
    {"topic": "math", "message": "Explain what a prime number is."},
    {"topic": "math", "message": "What is calculus used for?"},
    # Creative writing
    {"topic": "creative", "message": "Write a haiku about the ocean."},
    {"topic": "creative", "message": "Give me a one-sentence story."},
    {"topic": "creative", "message": "Describe a sunset in ten words."},
    {"topic": "creative", "message": "Write a limerick about coding."},
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _post(base_url: str, path: str, json_body: dict) -> dict | None:
    """POST helper with error handling. Returns response JSON or None on failure."""
    url = f"{base_url}{path}"
    try:
        resp = requests.post(url, json=json_body, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        print(f"  [ERROR] POST {path} failed: {exc}")
        return None


def _put(base_url: str, path: str, json_body: dict) -> dict | None:
    """PUT helper with error handling. Returns response JSON or None on failure."""
    url = f"{base_url}{path}"
    try:
        resp = requests.put(url, json=json_body, timeout=30)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        print(f"  [ERROR] PUT {path} failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Seeding functions
# ---------------------------------------------------------------------------


def seed_projects(base_url: str) -> int:
    """Create demo projects. Returns count of successfully created projects."""
    print("\n=== Creating Projects ===")
    created = 0
    for proj in PROJECTS:
        result = _post(base_url, "/admin/projects", proj)
        if result:
            print(f"  Created project: {proj['project_id']} ({proj['name']})")
            created += 1
    return created


def seed_user_budgets(base_url: str) -> int:
    """Set user budgets. Returns count of successfully configured users."""
    print("\n=== Setting User Budgets ===")
    configured = 0
    for user in USER_BUDGETS:
        uid = user["user_id"]
        body = {
            "budget_limit": user["budget_limit"],
            "alert_threshold": user["alert_threshold"],
        }
        result = _put(base_url, f"/admin/users/{uid}/budget", body)
        if result:
            print(f"  Set budget for {uid}: limit={user['budget_limit']}, alert={user['alert_threshold']}")
            configured += 1
    return configured


def seed_user_allowed_models(base_url: str) -> int:
    """Set restricted allowed_models for specific users. Returns count configured."""
    print("\n=== Setting User Model Restrictions ===")
    configured = 0
    for entry in RESTRICTED_USERS:
        uid = entry["user_id"]
        body = {"allowed_models": entry["allowed_models"]}
        result = _put(base_url, f"/admin/users/{uid}/allowed-models", body)
        if result:
            print(f"  Restricted {uid} to models: {entry['allowed_models']}")
            configured += 1
    return configured


def seed_usage_records(base_url: str, target_count: int = 55) -> int:
    """Generate usage records by sending real chat requests. Returns count created."""
    print(f"\n=== Generating {target_count} Usage Records via Chat API ===")
    print("  (Sending real requests to Bedrock — using cheap models to minimize cost)")

    # Users that are members of proj-alpha (the default chat-project won't be used;
    # the gateway uses the ClientAgent default project_id which is "chat-project").
    # We spread across users to get varied data.
    users = ["user-alice", "user-bob", "chat-user", "user-carol", "test-user"]
    created = 0
    errors = 0

    for i in range(target_count):
        # Pick user — weight toward alice/bob/chat-user for more data
        if i < 3:
            user_id = users[i % 3]  # Ensure first 3 hit alice, bob, chat-user
        else:
            user_id = random.choice(users[:4])  # Exclude test-user most of the time

        # Pick model — mostly cheap models
        if i % 10 == 0 and user_id != "test-user":
            model = "nova-pro"  # Occasionally use a slightly pricier model
        else:
            model = random.choice(CHEAP_MODELS)

        # Pick prompt
        prompt = random.choice(CHAT_PROMPTS)

        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt["message"]}],
            "user_id": user_id,
            "max_tokens": 50,  # Keep responses short to minimize cost
        }

        result = _post(base_url, "/api/chat", body)
        if result and "error" not in result:
            tokens = result.get("usage", {})
            print(
                f"  [{created + 1}/{target_count}] user={user_id} model={model} "
                f"topic={prompt['topic']} tokens={tokens.get('total_tokens', '?')}"
            )
            created += 1
        else:
            error_detail = ""
            if result and "error" in result:
                error_detail = f" — {result['error']}"
            print(f"  [{i + 1}/{target_count}] FAILED user={user_id} model={model}{error_detail}")
            errors += 1

        # Small delay to avoid rate limiting
        time.sleep(0.3)

    print(f"  Completed: {created} records created, {errors} errors")
    return created


def seed_policies(base_url: str) -> int:
    """Create Cedar policies. Returns count of successfully created policies."""
    print("\n=== Creating Cedar Policies ===")
    created = 0
    for policy in POLICIES:
        result = _post(base_url, "/admin/policies", policy)
        if result:
            print(f"  Created policy: {policy['name']} (mode={policy['mode']})")
            created += 1
    return created


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed AxonLLM with realistic demo data."
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of the LLM Router (default: http://localhost:8000)",
    )
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    print(f"Seeding demo data against: {base_url}")

    # 1. Projects
    projects_created = seed_projects(base_url)

    # 2. User budgets
    users_configured = seed_user_budgets(base_url)

    # 3. User model restrictions
    restrictions_set = seed_user_allowed_models(base_url)

    # 4. Usage records via real chat requests
    records_created = seed_usage_records(base_url)

    # 5. Cedar policies
    policies_created = seed_policies(base_url)

    # Summary
    print("\n" + "=" * 50)
    print("SEED SUMMARY")
    print("=" * 50)
    print(f"  Projects created:       {projects_created}")
    print(f"  Users configured:       {users_configured}")
    print(f"  Model restrictions set: {restrictions_set}")
    print(f"  Usage records created:  {records_created}")
    print(f"  Policies created:       {policies_created}")
    print("=" * 50)

    if projects_created == 0 or records_created == 0:
        print("\n[WARNING] Some seeding steps failed. Is the LLM Router running?")
        sys.exit(1)

    print("\nDone! Dashboard should now show demo data.")


if __name__ == "__main__":
    main()
