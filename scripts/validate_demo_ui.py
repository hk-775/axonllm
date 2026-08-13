#!/usr/bin/env python3
"""Browser smoke test for the fully seeded AxonLLM demo."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import time

from playwright.sync_api import Browser, Page, Playwright, sync_playwright


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_LIVE_MODEL = "groq-llama-3.1-8b"


def _launch_browser(playwright: Playwright) -> Browser:
    try:
        return playwright.chromium.launch(headless=True)
    except Exception as original:
        cache = Path.home() / "Library" / "Caches" / "ms-playwright"
        candidates = sorted(
            cache.glob(
                "chromium-*/chrome-mac-arm64/"
                "Google Chrome for Testing.app/Contents/MacOS/"
                "Google Chrome for Testing"
            ),
            reverse=True,
        )
        if not candidates:
            raise original
        return playwright.chromium.launch(
            headless=True,
            executable_path=str(candidates[0]),
        )


def _assert_no_page_overflow(page: Page, label: str) -> None:
    width = page.evaluate(
        """() => ({
            client: document.documentElement.clientWidth,
            scroll: document.documentElement.scrollWidth
        })"""
    )
    if width["scroll"] > width["client"] + 2:
        offenders = page.evaluate(
            """(clientWidth) => [...document.querySelectorAll('body *')]
                .map((element) => {
                    const rect = element.getBoundingClientRect();
                    return {
                        element: element.tagName.toLowerCase()
                            + (element.id ? `#${element.id}` : '')
                            + [...element.classList]
                                .map((name) => `.${name}`)
                                .join(''),
                        left: Math.round(rect.left),
                        right: Math.round(rect.right),
                        width: Math.round(rect.width),
                        scrollWidth: element.scrollWidth
                    };
                })
                .filter(({left, right}) => left < -2 || right > clientWidth + 2)
                .sort((a, b) => b.right - a.right)
                .slice(0, 12)""",
            width["client"],
        )
        raise AssertionError(
            f"{label} overflows horizontally: "
            f"{width['scroll']}px > {width['client']}px; "
            f"offenders={offenders}"
        )


def _wait_for_chat_result(frame, timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        errors = frame.locator(".message-bubble.error .message-content")
        if errors.count():
            error = errors.last.inner_text(timeout=1_000).strip()
            if error:
                raise AssertionError(f"Sandbox provider request failed: {error}")
        answers = frame.locator(
            ".message-bubble.assistant .message-content"
        )
        if answers.count():
            answer = answers.last.inner_text(timeout=1_000).strip()
            if answer:
                return answer
        time.sleep(0.25)
    raise AssertionError("timed out waiting for the sandbox provider result")


def _wait_for_routing_result(frame, timeout: float = 60.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        errors = frame.locator(".error-box")
        if errors.count():
            error = errors.last.inner_text(timeout=1_000).strip()
            if error:
                raise AssertionError(f"Smart routing request failed: {error}")
        responses = frame.locator(".response-text")
        if responses.count():
            response = responses.last.inner_text(timeout=1_000).strip()
            if response:
                return response
        time.sleep(0.25)
    raise AssertionError("timed out waiting for the smart routing result")


def _wait_for_flow_audio(page: Page, expected_file: str) -> None:
    page.wait_for_function(
        """expected => {
            const audio = document.querySelector("[data-flow-narration]");
            return audio
                && audio.currentSrc.endsWith("/narration/" + expected)
                && !audio.paused
                && audio.currentTime > 0;
        }""",
        arg=expected_file,
        timeout=15_000,
    )


def _validate_landing(page: Page, base_url: str, shots: Path | None) -> None:
    page.goto(base_url + "/", wait_until="domcontentloaded")
    page.locator("[data-axon-flow][data-ready=true]").wait_for()
    page.locator(".hero-preview img").wait_for()
    natural_width = page.locator(".hero-preview img").evaluate(
        "(image) => image.naturalWidth"
    )
    if natural_width < 1000:
        raise AssertionError("dashboard showcase image did not load")
    if page.get_by_text("Start Guided Showcase", exact=True).count() != 1:
        raise AssertionError("landing page is missing the guided showcase")

    flow = page.locator("[data-axon-flow]").first
    status = flow.locator("[data-flow-status]")
    page.get_by_role("tab", name="Provider fallback").click()
    if flow.locator("[data-flow-voice]").get_attribute("aria-pressed") != "true":
        raise AssertionError("interactive architecture narration is not on by default")
    before = status.inner_text()
    flow.get_by_role("button", name="Play scenario").click()
    _wait_for_flow_audio(page, "fallback-0.mp3")
    page.wait_for_function(
        """before => {
            const status = document.querySelector("[data-flow-status]");
            return status && status.textContent !== before;
        }""",
        arg=before,
        timeout=15_000,
    )
    flow.get_by_role("button", name="Pause scenario").click()
    if not flow.locator("[data-flow-narration]").evaluate("(audio) => audio.paused"):
        raise AssertionError("interactive architecture narration did not pause")

    page.get_by_role("button", name="Watch Product Film").click()
    dialog = page.locator("#demo-modal")
    if not dialog.evaluate("(element) => element.open"):
        raise AssertionError("product film dialog did not open")
    if not page.locator("#demo-video").get_attribute("src"):
        raise AssertionError("product film source was not attached on demand")
    page.get_by_role("button", name="Close demo").click()
    _assert_no_page_overflow(page, "desktop landing page")
    if shots:
        page.screenshot(path=str(shots / "landing-desktop.png"), full_page=True)


def _validate_architecture(
    page: Page,
    base_url: str,
    shots: Path | None,
) -> None:
    page.goto(
        base_url + "/architecture.html#interactive-flow",
        wait_until="domcontentloaded",
    )
    page.locator("[data-axon-flow][data-ready=true]").wait_for()
    flow = page.locator("[data-axon-flow]")
    for scenario in (
        "Smart chat routing",
        "Provider fallback",
        "Governed SQL SELECT",
        "Tenant admin RBAC",
    ):
        page.get_by_role("tab", name=scenario).click()
        if "STEP 1 /" not in page.locator("[data-flow-status]").inner_text():
            raise AssertionError(f"{scenario} did not reset to its first step")

    manifest_response = page.request.get(
        base_url + "/narration/interactive-flow-narration.json"
    )
    if not manifest_response.ok:
        raise AssertionError("interactive narration manifest was not served")
    tracks = manifest_response.json().get("tracks", [])
    if len(tracks) != 36:
        raise AssertionError(
            f"interactive narration has {len(tracks)} clips instead of 36"
        )
    for track in tracks:
        if track.get("duration", 0) <= 0:
            raise AssertionError(f"{track.get('id')} has no measured duration")
        response = page.request.get(
            base_url + f"/narration/{track['id']}.mp3"
        )
        if (
            not response.ok
            or "audio/mpeg" not in response.headers.get("content-type", "")
            or len(response.body()) < 1_000
        ):
            raise AssertionError(f"{track['id']} narration was not served")

    page.get_by_role("tab", name="Governed SQL SELECT").click()
    flow.get_by_role("button", name="Play scenario").click()
    _wait_for_flow_audio(page, "governed-sql-0.mp3")
    flow.get_by_role("button", name="Pause scenario").click()
    paused_at = flow.locator("[data-flow-narration]").evaluate(
        "(audio) => audio.currentTime"
    )
    if paused_at <= 0:
        raise AssertionError("architecture narration did not retain pause position")
    flow.get_by_role("button", name="Reset scenario").click()
    if "STEP 1 / 8" not in flow.locator("[data-flow-status]").inner_text():
        raise AssertionError("architecture narration reset changed the wrong step")
    reset_at = flow.locator("[data-flow-narration]").evaluate(
        "(audio) => audio.currentTime"
    )
    if reset_at > 0.05:
        raise AssertionError("architecture narration did not rewind on reset")

    page.get_by_role("tab", name="Tenant admin RBAC").click()
    flow.get_by_role("button", name="Play scenario").click()
    _wait_for_flow_audio(page, "tenant-rbac-0.mp3")
    flow.get_by_role("button", name="Mute narration").click()
    if flow.locator("[data-flow-voice]").get_attribute("aria-pressed") != "false":
        raise AssertionError("architecture narration mute state did not change")
    if not flow.locator("[data-flow-narration]").evaluate("(audio) => audio.paused"):
        raise AssertionError("architecture narration kept playing after mute")
    flow.get_by_role("button", name="Enable narration").click()
    _wait_for_flow_audio(page, "tenant-rbac-0.mp3")
    flow.get_by_role("button", name="Pause scenario").click()

    for tab_name in ("Infrastructure", "Request Pipeline", "Components"):
        page.get_by_role("tab", name=re.compile(tab_name)).last.click()
        selected = page.locator('.tab[aria-selected="true"]')
        panel_id = selected.get_attribute("aria-controls")
        page.locator(f"#{panel_id} svg").wait_for(timeout=20_000)
    if page.locator("#narration").get_attribute("hidden") is not None:
        raise AssertionError("architecture narration did not initialize")
    _assert_no_page_overflow(page, "desktop architecture page")
    if shots:
        page.screenshot(
            path=str(shots / "architecture-desktop.png"),
            full_page=True,
        )


def _validate_guided_showcase(page: Page, base_url: str) -> None:
    page.goto(
        base_url + "/admin/dashboard?tour=1",
        wait_until="domcontentloaded",
    )
    page.get_by_text(re.compile(r"Scene 1 of \d+")).wait_for(timeout=20_000)
    if page.get_by_text("One Gateway, Every Model", exact=True).count() == 0:
        raise AssertionError("guided showcase did not start on scene one")
    page.get_by_role("button", name=re.compile("End tour")).click()


def _validate_dashboard_pages(
    page: Page,
    base_url: str,
    shots: Path | None,
) -> list[str]:
    page.goto(base_url + "/admin/dashboard", wait_until="domcontentloaded")
    page.get_by_role("heading", name="Overview", exact=True).wait_for(
        timeout=20_000
    )
    overview_response = page.request.get(base_url + "/admin/overview")
    if not overview_response.ok:
        raise AssertionError(
            f"seeded Overview API returned {overview_response.status}"
        )
    overview = overview_response.json()
    expected_minima = {
        "total_requests": 66,
        "total_cost": 1.263,
        "cache_hit_rate": 0.874,
        "active_users": 3,
    }
    for field, minimum in expected_minima.items():
        if overview.get(field, 0) < minimum:
            raise AssertionError(
                f"seeded Overview {field} is below {minimum}: "
                f"{overview.get(field)}"
            )
    for field, expected in {"active_projects": 2}.items():
        if overview.get(field) != expected:
            raise AssertionError(
                f"seeded Overview {field} is not {expected}: "
                f"{overview.get(field)}"
            )

    visited: list[str] = []
    navigation = page.locator("button.nav-item")
    count = navigation.count()
    for index in range(count):
        item = navigation.nth(index)
        label = item.inner_text().strip().splitlines()[-1]
        item.click()
        page.locator("main h1").wait_for(timeout=15_000)
        page.wait_for_timeout(500)
        main_text = page.locator("main").inner_text().strip()
        frame_locator = page.locator("main iframe")
        minimum = 40 if frame_locator.count() else 70
        if len(main_text) < minimum:
            raise AssertionError(
                f"{label} rendered too little content ({len(main_text)} chars)"
            )
        lowered = main_text.lower()
        if "failed to load" in lowered or "unexpected error" in lowered:
            raise AssertionError(f"{label} rendered an error: {main_text[:240]}")

        if frame_locator.count():
            frame = frame_locator.content_frame
            frame.locator("body").wait_for(timeout=15_000)
            frame_text = frame.locator("body").inner_text().strip()
            if len(frame_text) < 80:
                raise AssertionError(
                    f"{label} iframe rendered too little content"
                )

        if label == "API Keys":
            page.get_by_text(re.compile(r"Keys for proj-")).wait_for(
                timeout=15_000
            )
            if page.locator("main tbody tr").count() == 0:
                raise AssertionError("API Keys selected a project but showed no keys")
        visited.append(label)

    expected_pages = {
        "Sandbox",
        "Overview",
        "Traces",
        "Efficiency",
        "Audit Log",
        "Models",
        "Projects",
        "Users",
        "API Keys",
        "Policies",
        "Hierarchy",
        "Quotas",
        "Regions",
        "Webhooks",
        "Health",
        "Configuration",
        "Architecture",
        "Pricing",
        "Catalogue",
        "Readiness",
    }
    if set(visited) != expected_pages:
        raise AssertionError(
            "dashboard navigation mismatch: "
            f"missing={sorted(expected_pages - set(visited))}, "
            f"extra={sorted(set(visited) - expected_pages)}"
        )

    page.get_by_role("button", name=re.compile("Sandbox")).click()
    page.get_by_role("heading", name="Sandbox", exact=True).wait_for()
    for tab_name, marker in (
        ("Chat", "Select a model and start a conversation"),
        ("Playground", "Type Prompt"),
        ("Routing", "AxonLLM picks the model and provider"),
    ):
        page.get_by_role("button", name=tab_name, exact=True).click()
        frame = page.locator("main iframe").content_frame
        frame.get_by_text(re.compile(marker)).wait_for(timeout=20_000)

    if shots:
        page.screenshot(
            path=str(shots / "sandbox-desktop.png"),
            full_page=True,
        )
    return visited


def _validate_live_sandbox(
    page: Page,
    requested_model: str,
) -> list[str]:
    results: list[str] = []

    # Chat exercises the streaming route and its provider metadata.
    page.get_by_role("button", name="Chat", exact=True).click()
    frame = page.locator("main iframe").content_frame
    model_select = frame.locator('select[aria-label="Select model"]')
    model_select.wait_for(timeout=20_000)
    options = model_select.locator("option").evaluate_all(
        "(items) => items.map((item) => item.value)"
    )
    model = requested_model if requested_model in options else options[0]
    model_select.select_option(model)
    frame.locator('textarea[aria-label="Message input"]').fill(
        "Respond with exactly: AxonLLM demo ready."
    )
    frame.get_by_role("button", name="Send message").click()
    answer = _wait_for_chat_result(frame, timeout=75)
    provider = frame.locator(".message-bubble.assistant .model-badge").last
    provider_text = provider.inner_text().strip() if provider.count() else "unknown"
    if "groq" not in provider_text.lower():
        raise AssertionError(
            f"Chat succeeded but did not show its Groq provider: {provider_text}"
        )
    results.append(f"Chat: {provider_text}: {answer[:80]}")

    # Playground exercises the non-streaming route and routing explanation.
    page.get_by_role("button", name="Playground", exact=True).click()
    frame = page.locator("main iframe").content_frame
    model_select = frame.locator('select[aria-label="Select model"]')
    model_select.wait_for(timeout=20_000)
    options = model_select.locator("option").evaluate_all(
        "(items) => items.map((item) => item.value)"
    )
    playground_model = model if model in options else options[0]
    model_select.select_option(playground_model)
    frame.locator('textarea[aria-label="Message input"]').fill(
        "Respond with exactly: AxonLLM playground ready."
    )
    frame.get_by_role("button", name="Send message").click()
    playground_answer = _wait_for_chat_result(frame, timeout=75)
    playground_badges = frame.locator(
        ".message-bubble.assistant .model-badge"
    )
    playground_metadata = " ".join(
        playground_badges.all_inner_texts()
    ).strip()
    if "groq" not in playground_metadata.lower():
        raise AssertionError(
            "Playground succeeded but did not show its Groq provider: "
            + playground_metadata
        )
    results.append(
        f"Playground: {playground_metadata}: {playground_answer[:80]}"
    )

    # Routing exercises intent classification and automatic model/provider
    # selection rather than another direct-model call.
    page.get_by_role("button", name="Routing", exact=True).click()
    frame = page.locator("main iframe").content_frame
    prompt = frame.locator('textarea[aria-label="Prompt input"]')
    prompt.wait_for(timeout=20_000)
    prompt.fill("Respond with exactly: AxonLLM routing ready.")
    frame.get_by_role("button", name=re.compile("Route & Send")).click()
    routing_answer = _wait_for_routing_result(frame, timeout=75)
    frame.locator(".smart-routing-card").wait_for(timeout=10_000)
    routing_text = frame.locator(".decision").first.inner_text().strip()
    if "unknown" in routing_text.lower():
        raise AssertionError(
            f"Smart routing did not identify its target: {routing_text}"
        )
    results.append(f"Routing: {routing_text}: {routing_answer[:80]}")

    return results


def _validate_mobile(
    browser: Browser,
    base_url: str,
    shots: Path | None,
) -> None:
    page = browser.new_page(
        viewport={"width": 390, "height": 844},
        device_scale_factor=1,
        is_mobile=True,
    )
    try:
        page.goto(base_url + "/", wait_until="domcontentloaded")
        page.locator("[data-axon-flow][data-ready=true]").wait_for()
        _assert_no_page_overflow(page, "mobile landing page")
        if shots:
            page.screenshot(
                path=str(shots / "landing-mobile.png"),
                full_page=True,
            )
        page.goto(
            base_url + "/architecture.html#interactive-flow",
            wait_until="domcontentloaded",
        )
        page.locator("[data-axon-flow][data-ready=true]").wait_for()
        _assert_no_page_overflow(page, "mobile architecture page")
        if shots:
            page.screenshot(
                path=str(shots / "architecture-mobile.png"),
                full_page=True,
            )
    finally:
        page.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--live-model", default=DEFAULT_LIVE_MODEL)
    parser.add_argument("--skip-live-provider", action="store_true")
    parser.add_argument("--screenshot-dir", type=Path)
    args = parser.parse_args()

    shots = args.screenshot_dir
    if shots:
        shots.mkdir(parents=True, exist_ok=True)

    console_errors: list[str] = []
    server_errors: list[str] = []
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(
            viewport={"width": 1440, "height": 900},
            device_scale_factor=1,
        )
        page.on(
            "console",
            lambda message: (
                console_errors.append(message.text)
                if message.type == "error"
                else None
            ),
        )
        page.on(
            "response",
            lambda response: (
                server_errors.append(f"{response.status} {response.url}")
                if response.status >= 400
                else None
            ),
        )
        try:
            _validate_landing(page, args.base_url, shots)
            _validate_architecture(page, args.base_url, shots)
            _validate_guided_showcase(page, args.base_url)
            visited = _validate_dashboard_pages(
                page,
                args.base_url,
                shots,
            )
            live = None
            if not args.skip_live_provider:
                live = _validate_live_sandbox(page, args.live_model)
            _validate_mobile(browser, args.base_url, shots)
        finally:
            page.close()
            browser.close()

    if server_errors:
        raise AssertionError(
            "server responses >=400:\n" + "\n".join(server_errors)
        )
    if console_errors:
        raise AssertionError(
            "browser console errors:\n" + "\n".join(console_errors)
        )

    print(f"Landing, architecture, and {len(visited)} dashboard pages passed.")
    if live:
        print("Sandbox live modes passed: " + " | ".join(live))
    if shots:
        print(f"Screenshots: {shots}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
