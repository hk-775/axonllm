"""AxonLLM CLI — start the gateway, run demos, and manage configuration."""

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def cmd_demo(args):
    """Start the server and generate real traffic for a live demo."""
    script = ROOT / "scripts" / "demo.sh"
    os.execvp("bash", ["bash", str(script)])


def cmd_serve(args):
    """Start the AxonLLM gateway server."""
    os.environ["AXON_LOAD_DEMO_DATA"] = "true" if args.demo_data else ""
    os.chdir(ROOT)
    import shutil
    if shutil.which("uv"):
        os.execvp("uv", ["uv", "run", "python", "serve_dashboard.py"])
    else:
        os.execvp(sys.executable, [sys.executable, "serve_dashboard.py"])


def cmd_issue_key(args):
    """Mint an API key directly (in-process), bypassing the admin HTTP endpoint.

    Solves the bootstrap chicken-and-egg: under ENFORCE, POST /admin/projects/{id}/keys
    itself requires an admin credential, so there's no way to get the *first* key over
    HTTP. This mints one via APIKeyService against the same persistence the server uses.
    """
    import asyncio

    os.chdir(ROOT)
    from src.gateway.auth.api_key_service import APIKeyService
    from src.gateway.persistence import DynamoPersistence

    persistence = DynamoPersistence(region=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
    service = APIKeyService(persistence=persistence)

    async def _issue():
        if persistence.enabled:
            await persistence.create_table_if_not_exists()
        scopes = args.scopes.split(",") if args.scopes else ["chat"]
        _, raw_key = await service.issue_key(
            project_id=args.project,
            name=args.name,
            scopes=[s.strip() for s in scopes if s.strip()],
            created_by="cli",
        )
        return raw_key

    raw_key = asyncio.run(_issue())
    if not persistence.enabled:
        print(
            "\033[33mWarning:\033[0m LLM_ROUTER_DYNAMODB_ENABLED is not 'true', so this key was "
            "NOT persisted and will not be recognized by a running server. Enable persistence "
            "(and point AXON_DYNAMODB_TABLE at the server's table) to mint a usable key.",
            file=sys.stderr,
        )
    print(f"\033[1mAPI key issued for project '{args.project}':\033[0m")
    print(raw_key)
    print("\n\033[2mStore this now — it is shown only once. Use it as:")
    print("  Authorization: Bearer <key>   or   X-Api-Key: <key>\033[0m")


def cmd_chat(args):
    """Send a quick chat message via the gateway."""
    import json
    import urllib.request

    base = f"http://localhost:{args.port}"
    payload = json.dumps({
        "model": args.model,
        "messages": [{"role": "user", "content": " ".join(args.message)}],
    }).encode()

    headers = {"Content-Type": "application/json"}
    api_key = args.api_key or os.environ.get("AXON_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        f"{base}/api/chat",
        data=payload,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            print(f"\033[1m{data.get('model', args.model)}\033[0m ({data.get('provider', '?')})")
            print(data.get("content", data.get("error", {}).get("message", "No response")))
            usage = data.get("usage", {})
            if usage:
                print(f"\n\033[2m{usage.get('total_tokens', 0)} tokens\033[0m")
    except Exception as e:
        print(f"Error: {e}")
        print("Is the server running? Try: axon serve")
        sys.exit(1)


def cmd_models(args):
    """List available models."""
    import json
    import urllib.request

    base = f"http://localhost:{args.port}"
    api_key = args.api_key or os.environ.get("AXON_API_KEY")
    models_req = urllib.request.Request(f"{base}/api/models")
    if api_key:
        models_req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(models_req, timeout=5) as resp:
            models = json.loads(resp.read())
            print(f"\033[1m{len(models)} models available:\033[0m\n")
            for m in models:
                providers = ", ".join(m.get("providers", []))
                strategy = m.get("routing_strategy", "")
                print(f"  {m['name']:<28} {providers:<20} ({strategy})")
    except Exception as e:
        print(f"Error: {e}\nIs the server running? Try: axon serve")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="axon",
        description="AxonLLM — The neural control plane for enterprise LLMs",
    )
    sub = parser.add_subparsers(dest="command")

    # demo
    sub.add_parser("demo", help="Start server + generate real traffic for a live demo")

    # serve
    p_serve = sub.add_parser("serve", help="Start the AxonLLM gateway server")
    p_serve.add_argument("--demo-data", action="store_true", default=True, help="Load demo seed data (default: true)")
    p_serve.add_argument("--no-demo-data", dest="demo_data", action="store_false")

    # issue-key
    p_key = sub.add_parser("issue-key", help="Mint an API key (in-process; works under ENFORCE)")
    p_key.add_argument("-P", "--project", default="default", help="Project ID to scope the key to")
    p_key.add_argument("-n", "--name", default="cli-issued", help="Human-readable key name")
    p_key.add_argument(
        "-s", "--scopes", default="chat",
        help="Comma-separated scopes (default: chat). Admin scopes take an "
             "optional access level: 'admin:quotas:read' for read-only, "
             "'admin:quotas:write' or bare 'admin:quotas' for both, "
             "'admin:*' for everything, 'admin:*:read' to read everything",
    )

    # chat
    p_chat = sub.add_parser("chat", help="Send a chat message")
    p_chat.add_argument("message", nargs="+", help="The message to send")
    p_chat.add_argument("-m", "--model", default="claude-sonnet", help="Model to use")
    p_chat.add_argument("-p", "--port", type=int, default=8000)
    p_chat.add_argument("-k", "--api-key", default=None, help="API key (or set AXON_API_KEY)")

    # models
    p_models = sub.add_parser("models", help="List available models")
    p_models.add_argument("-p", "--port", type=int, default=8000)
    p_models.add_argument("-k", "--api-key", default=None, help="API key (or set AXON_API_KEY)")

    args = parser.parse_args()

    if args.command == "demo":
        cmd_demo(args)
    elif args.command == "serve":
        cmd_serve(args)
    elif args.command == "issue-key":
        cmd_issue_key(args)
    elif args.command == "chat":
        cmd_chat(args)
    elif args.command == "models":
        cmd_models(args)
    else:
        parser.print_help()


def demo():
    """Direct entry point for `uv run axon-demo`."""
    cmd_demo(None)


if __name__ == "__main__":
    main()
