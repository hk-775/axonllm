#!/usr/bin/env python3
"""Create the local DynamoDB table used by the canonical demo."""

from __future__ import annotations

import asyncio
import os
import sys

from src.gateway.persistence import DynamoPersistence


async def _initialize() -> None:
    persistence = DynamoPersistence()
    for _attempt in range(120):
        await persistence.create_table_if_not_exists()
        status = await persistence.health_status()
        if status.get("reachable") is True:
            return
        await asyncio.sleep(0.5)
    raise RuntimeError("DynamoDB Local did not become ready within 60 seconds")


def main() -> int:
    os.environ["LLM_ROUTER_DYNAMODB_ENABLED"] = "true"
    os.environ["AXON_DEPLOYMENT_PROFILE"] = "development"
    try:
        asyncio.run(_initialize())
    except Exception as exc:
        print(f"Local demo state initialization failed: {exc}", file=sys.stderr)
        return 1
    print("Local demo state table is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
