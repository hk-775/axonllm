#!/usr/bin/env python3
"""Compatibility launcher for the packaged AgentCore deployment command."""

from src.gateway.deployment.agentcore_deploy import main


if __name__ == "__main__":
    raise SystemExit(main())
