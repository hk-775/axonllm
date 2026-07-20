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


def cmd_chat(args):
    """Send a quick chat message via the gateway."""
    import json
    import urllib.request

    base = f"http://localhost:{args.port}"
    payload = json.dumps({
        "model": args.model,
        "messages": [{"role": "user", "content": " ".join(args.message)}],
    }).encode()

    req = urllib.request.Request(
        f"{base}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
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
    try:
        with urllib.request.urlopen(f"{base}/api/models", timeout=5) as resp:
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

    # chat
    p_chat = sub.add_parser("chat", help="Send a chat message")
    p_chat.add_argument("message", nargs="+", help="The message to send")
    p_chat.add_argument("-m", "--model", default="groq-llama-3.3-70b", help="Model to use")
    p_chat.add_argument("-p", "--port", type=int, default=8000)

    # models
    p_models = sub.add_parser("models", help="List available models")
    p_models.add_argument("-p", "--port", type=int, default=8000)

    args = parser.parse_args()

    if args.command == "demo":
        cmd_demo(args)
    elif args.command == "serve":
        cmd_serve(args)
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
