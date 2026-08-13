"""Production asset and CSP coverage for the browser chat surfaces."""

from __future__ import annotations

import asyncio
import hashlib
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.testclient import TestClient

from src.gateway.chat.routes import (
    ChatAPI,
    chat_static_asset,
    create_chat_routes,
)

REPO_ROOT = Path(__file__).parents[2]
CHAT_ROOT = REPO_ROOT / "src" / "gateway" / "chat"
STATIC_ROOT = CHAT_ROOT / "static"
PAGES = {
    "index.html": "chat.js",
    "playground.html": "playground.js",
    "routing.html": "routing.js",
}
PUBLIC_ASSETS = (
    "chat.js",
    "playground.js",
    "routing.js",
    "vendor/react.production.min.js",
    "vendor/react-dom.production.min.js",
)


class _HeadCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.resources: list[str] = []
        self.csp: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        attributes = dict(attrs)
        if tag == "script":
            self.scripts.append(attributes)
        for name in ("src", "href"):
            value = attributes.get(name)
            if value is not None:
                self.resources.append(value)
        if (
            tag == "meta"
            and attributes.get("http-equiv") == "Content-Security-Policy"
        ):
            self.csp = attributes.get("content")


def _parse_page(name: str) -> tuple[str, _HeadCollector]:
    html = (STATIC_ROOT / name).read_text(encoding="utf-8")
    parser = _HeadCollector()
    parser.feed(html)
    return html, parser


def _directives(policy: str) -> dict[str, list[str]]:
    directives: dict[str, list[str]] = {}
    for raw_directive in policy.split(";"):
        parts = raw_directive.strip().split()
        if parts:
            directives[parts[0]] = parts[1:]
    return directives


def test_chat_pages_use_only_precompiled_same_origin_scripts() -> None:
    for page_name, bundle_name in PAGES.items():
        html, parsed = _parse_page(page_name)

        assert len(parsed.scripts) == 3
        assert html.count("</script>") == len(parsed.scripts)
        assert all(script.get("src") for script in parsed.scripts)
        assert all(
            script["src"].startswith("/chat/static/")
            for script in parsed.scripts
        )
        assert parsed.scripts[-1]["src"].startswith(
            f"/chat/static/{bundle_name}?v="
        )
        assert not any(
            urlsplit(resource).scheme in {"http", "https"}
            for resource in parsed.resources
        )
        assert 'type="text/babel"' not in html
        assert "babel.min.js" not in html
        assert "fonts.googleapis.com" not in html
        assert "unpkg.com" not in html


def test_chat_page_csp_allows_no_remote_or_inline_scripts() -> None:
    for page_name in PAGES:
        _, parsed = _parse_page(page_name)

        assert parsed.csp is not None
        directives = _directives(parsed.csp)
        assert directives["default-src"] == ["'self'"]
        assert directives["connect-src"] == ["'self'"]
        assert directives["script-src"] == ["'self'"]
        assert directives["object-src"] == ["'none'"]
        assert directives["worker-src"] == ["'none'"]


def test_all_chat_asset_digests_match_the_served_files() -> None:
    for page_name in PAGES:
        _, parsed = _parse_page(page_name)

        for script in parsed.scripts:
            source = script["src"]
            assert source is not None
            parsed_source = urlsplit(source)
            assert parsed_source.path.startswith("/chat/static/")
            digest_match = re.fullmatch(r"v=([a-f0-9]{64})", parsed_source.query)
            assert digest_match is not None
            asset = STATIC_ROOT / parsed_source.path.removeprefix(
                "/chat/static/"
            )
            actual_digest = hashlib.sha256(asset.read_bytes()).hexdigest()
            assert digest_match.group(1) == actual_digest


def test_chat_generated_assets_are_current_and_valid_javascript() -> None:
    check = subprocess.run(
        ["node", "scripts/build_chat_assets.cjs", "--check"],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert check.returncode == 0, check.stdout + check.stderr

    for script in (
        "scripts/build_chat_assets.cjs",
        "src/gateway/chat/static/chat.js",
        "src/gateway/chat/static/playground.js",
        "src/gateway/chat/static/routing.js",
    ):
        syntax = subprocess.run(
            ["node", "--check", script],
            cwd=REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert syntax.returncode == 0, syntax.stdout + syntax.stderr


def test_chat_clients_echo_csrf_for_unsafe_session_requests() -> None:
    for name in ("chat", "playground", "routing"):
        source = (CHAT_ROOT / f"{name}.jsx").read_text(encoding="utf-8")
        compiled = (STATIC_ROOT / f"{name}.js").read_text(
            encoding="utf-8"
        )
        for content in (source, compiled):
            assert "const CSRF_COOKIE = '__Host-axon-csrf';" in content
            assert "headers['X-Axon-CSRF-Token'] = csrfToken;" in content
            assert "requestOptions.credentials = 'same-origin';" in content


def test_chat_static_route_serves_only_the_build_allowlist() -> None:
    app = Starlette(routes=create_chat_routes(ChatAPI(object())))

    with TestClient(app) as client:
        for relative_path in PUBLIC_ASSETS:
            response = client.get(f"/chat/static/{relative_path}")
            assert response.status_code == 200
            assert response.content == (STATIC_ROOT / relative_path).read_bytes()
            assert response.headers["content-type"].startswith(
                "application/javascript"
            )
            assert response.headers["cache-control"] == (
                "public, max-age=31536000, immutable"
            )

        assert client.get("/chat/static/index.html").status_code == 404
        assert (
            client.get("/chat/static/vendor/babel.min.js").status_code == 404
        )


def test_chat_static_route_rejects_traversal_before_filesystem_access() -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": "/chat/static/../routes.py",
        "raw_path": b"/chat/static/../routes.py",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 443),
        "path_params": {"path": "../routes.py"},
    }

    response = asyncio.run(chat_static_asset(Request(scope)))

    assert response.status_code == 404
