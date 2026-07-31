"""The shared look of the standalone admin report pages.

These pages drifted because each carried its own copy of a near-identical
stylesheet: all three were still the old AWS Console palette (`#232F3E` navy,
`#FF9900` orange) long after the dashboard became stone-and-violet. Nobody
recolors the same CSS three times.

Deduping into `page_style` fixes today's drift. These tests are what stops the
next one: they assert against the palette and the ribbon rather than against a
snapshot, so a page that stops using the shared sheet fails here instead of
silently looking like a different product.
"""

from __future__ import annotations

import functools
import re
import xml.etree.ElementTree as ET

import pytest

from src.gateway.admin import page_style
from src.gateway.admin.page_style import BASE_STYLE, FAVICON, ribbon
from src.gateway.admin.pricing_drift import (
    OrphanPricingEntry,
    PricingDriftReport,
    UnpricedMapping,
    render_drift_page,
)
from src.gateway.admin.catalog_drift import (
    CatalogDriftReport,
    DormantModel,
    UndeclaredTraffic,
    UndescribedMapping,
    UnroutableCatalogEntry,
    render_catalog_drift_page,
)
from src.gateway.admin.production_checklist import (
    CheckResult,
    ChecklistReport,
    Status,
    render_checklist_page,
)

# Every color the pages are allowed to use. Sourced from the dashboard's
# --awsui-color-* variables, plus the status shades one step darker (see the
# page_style docstring for why) and the mark's gradient stops.
PALETTE = {
    "#fafaf9", "#ffffff", "#e7e5e4", "#f5f5f4", "#d6d3d1",   # stone
    "#1c1917", "#0c0a09", "#78716c", "#57534e",              # text
    "#7c3aed", "#6d28d9", "#8b5cf6",                          # violet
    "#15803d", "#f0fdf4", "#bbf7d0",                          # success
    "#b91c1c", "#fef2f2", "#fecaca",                          # error
    "#b45309", "#fffbeb", "#fde68a",                          # warning
    "#1d4ed8", "#f0f9ff",                                     # info
}

# The palette these pages used to be, and must never drift back to.
AWS_CONSOLE = {
    "#232f3e", "#ff9900", "#ec7211", "#5f6b7a", "#e3e6eb", "#16191f",
    "#f8f9fa", "#0972d3", "#d13212", "#1d8102", "#8d99a8", "#f2f3f5",
    "#eff1f3",
}


def _hexes(css: str) -> set[str]:
    return {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}\b", css)}


def _styles(html: str) -> str:
    return "".join(re.findall(r"<style>(.*?)</style>", html, re.S))


def _body(html: str) -> str:
    return re.sub(r"<style>.*?</style>", "", html, flags=re.S)


def _drift_page(*, embed: bool = False) -> str:
    report = PricingDriftReport(
        total_mappings=49, priced_mappings=41,
        unpriced=[
            UnpricedMapping(model="llama-3", provider="groq",
                            model_id="llama-3-70b", suggestion=None),
            # One with a suggestion, so the <pre> YAML block renders too.
            UnpricedMapping(model="mixtral", provider="fireworks",
                            model_id="mixtral-8x7b",
                            suggestion="  mixtral-8x7b:\n    prompt: 0.5"),
        ],
        orphans=[OrphanPricingEntry(provider="openai", model_id="gpt-3.5-old")],
        providers_missing_section=["groq"],
        models_fully_unpriced=["llama-3"],
    )
    return render_drift_page(report, "config/pricing.yaml", embed=embed)


def _checklist_page(*, embed: bool = False) -> str:
    """One check per status, so every .check.* and .pill.* rule renders.

    The live page in demo mode emits none of them — it returns a
    did-not-run banner — so exercising the real report is the only way these
    rules are covered at all.
    """
    checks = [
        CheckResult(key="a", title="Auth mode", status=Status.FAIL,
                    summary="LOG_ONLY admits every request.", detail="d",
                    fix="Set AXON_AUTH_MODE=ENFORCE.", rows=[]),
        CheckResult(key="b", title="Pricing", status=Status.WARN,
                    summary="8 mappings bill at $0.00.", detail="d",
                    fix="f", rows=[{"provider": "groq", "model": "llama-3"}]),
        CheckResult(key="c", title="Persistence", status=Status.PASS,
                    summary="Reachable.", detail="", fix="", rows=[]),
        CheckResult(key="d", title="Catalogue", status=Status.UNKNOWN,
                    summary="Could not be checked.", detail="", fix="", rows=[]),
    ]
    return render_checklist_page(
        ChecklistReport(checks=checks, did_not_run=None, availability=None),
        embed=embed,
    )


def _catalog_page(*, embed: bool = False) -> str:
    """One of every finding, so each table and banner variant renders.

    The undeclared-traffic entry matters most: it is the only path that reaches
    the .banner.fail branch, and a report built from the real config has none of
    them — so without a synthetic one, that branch is never styled-checked.
    """
    report = CatalogDriftReport(
        total_mappings=49, described_mappings=10, total_models=46,
        undescribed=[
            UndescribedMapping(model="gpt-4", provider="openai",
                               model_id="gpt-4-turbo"),
            UndescribedMapping(model="llama-3", provider="groq",
                               model_id="llama-3-70b",
                               provider_section_missing=True),
        ],
        unroutable=[UnroutableCatalogEntry(provider="openai",
                                           model_id="gpt-3.5-old",
                                           display_name="GPT-3.5 (retired)")],
        dormant=[DormantModel(model="claude-opus", providers=("anthropic",))],
        undeclared=[UndeclaredTraffic(model="mystery-v2", provider="xai",
                                      requests=3)],
        providers_missing_section=["groq"],
        models_without_capabilities=["gpt-4", "llama-3"],
        observed_models=9,
    )
    return render_catalog_drift_page(
        report, "config/models.yaml", "config/catalog.yaml", embed=embed
    )


PAGES = [
    ("pricing-drift", _drift_page),
    ("catalog-drift", _catalog_page),
    ("production-checklist", _checklist_page),
]

# The same pages framed in the dashboard shell. Same fixtures, so a content
# assertion that holds standalone is being made against identical input.
EMBEDDED = [
    (name, functools.partial(render, embed=True)) for name, render in PAGES
]


class TestPalette:
    @pytest.mark.parametrize("name,render", PAGES, ids=[p[0] for p in PAGES])
    def test_no_aws_console_colors(self, name, render):
        """The specific regression: these were navy-and-orange."""
        leaked = _hexes(_styles(render())) & AWS_CONSOLE
        assert not leaked, f"{name} still uses AWS Console colors: {sorted(leaked)}"

    @pytest.mark.parametrize("name,render", PAGES, ids=[p[0] for p in PAGES])
    def test_every_color_is_in_the_palette(self, name, render):
        """Stricter than the above: no off-palette color at all, not just old ones.

        A new page that invents its own blue is the same failure as one that
        kept the old orange.
        """
        stray = _hexes(_styles(render())) - PALETTE
        assert not stray, f"{name} uses off-palette colors: {sorted(stray)}"

    @pytest.mark.parametrize("name,render", PAGES, ids=[p[0] for p in PAGES])
    def test_the_page_uses_the_shared_sheet(self, name, render):
        """A page could pass the palette checks with its own duplicated CSS.

        This is the check that keeps them deduped, which is the actual fix —
        the palette only stayed wrong because it was written down three times.
        """
        assert BASE_STYLE in _styles(render())

    def test_the_primary_matches_the_dashboard(self):
        """violet-600, the dashboard's --awsui-color-primary. Same value or the
        pages read as a different product."""
        assert page_style.PRIMARY == "#7c3aed"

    def test_status_tones_are_darker_than_the_dashboards(self):
        """Deliberate divergence, pinned so it is not "corrected" back.

        The dashboard's -600 status shades measure 3.07-4.41 on their own tint
        backgrounds. These pages set status as 10-13px pills and table text —
        body text, needing 4.5 — so they use the -700 shades.
        """
        assert page_style.OK != "#16a34a"
        assert page_style.ERR != "#dc2626"
        assert page_style.WARN != "#d97706"

    def test_unknown_is_not_a_status_hue(self):
        """A check that could not run has no result.

        Coloring UNKNOWN green, amber or red asserts an outcome nobody measured
        — the exact failure the readiness page exists to surface.
        """
        assert page_style.UNKNOWN not in {
            page_style.OK, page_style.WARN, page_style.ERR, page_style.INFO,
        }
        assert page_style.UNKNOWN == page_style.TEXT_DIM


class TestContrast:
    """Contrast as a test, not a one-off measurement.

    Every pairing here was computed at the time it was chosen; without a test
    the next palette tweak silently reverts one of them.
    """

    @staticmethod
    def _ratio(fg: str, bg: str) -> float:
        def lum(h: str) -> float:
            h = h.lstrip("#")
            ch = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
            ch = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
                  for c in ch]
            return 0.2126 * ch[0] + 0.7152 * ch[1] + 0.0722 * ch[2]
        a, b = lum(fg), lum(bg)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    # All of it is body text at 10-19px, so 4.5 applies throughout —
    # the 3.0 large-text allowance never does.
    PAIRS = [
        ("body", page_style.TEXT, page_style.BG),
        ("heading", page_style.TEXT_HEADING, page_style.SURFACE),
        ("secondary on white", page_style.TEXT_DIM, page_style.SURFACE),
        ("secondary on bg", page_style.TEXT_DIM, page_style.BG),
        ("table header", page_style.TEXT_MUTED, page_style.BORDER_SOFT),
        ("ribbon action", page_style.SURFACE, page_style.PRIMARY),
        ("ok banner", page_style.OK, page_style.OK_BG),
        ("warn banner", page_style.WARN, page_style.WARN_BG),
        ("fail banner", page_style.ERR, page_style.ERR_BG),
        ("pass pill", page_style.SURFACE, page_style.OK),
        ("warn pill", page_style.SURFACE, page_style.WARN),
        ("fail pill", page_style.SURFACE, page_style.ERR),
        ("unknown pill", page_style.SURFACE, page_style.UNKNOWN),
        ("fix link", page_style.INFO, page_style.SURFACE),
        ("code", page_style.TEXT, page_style.BORDER_SOFT),
    ]

    @pytest.mark.parametrize("label,fg,bg", PAIRS, ids=[p[0] for p in PAIRS])
    def test_meets_wcag_aa_body_text(self, label, fg, bg):
        r = self._ratio(fg, bg)
        assert r >= 4.5, f"{label}: {fg} on {bg} is {r:.2f}, needs 4.5"

    def test_the_focus_ring_meets_the_non_text_bar(self):
        """WCAG 1.4.11 wants 3.0 for a focus indicator.

        The dashboard's own focus border (violet-400) is 2.61 against stone-50,
        which is why these pages use violet-600 instead.
        """
        assert self._ratio(page_style.PRIMARY, page_style.BG) >= 3.0


class TestRibbon:
    """The ribbon the three pages were missing entirely."""

    @pytest.mark.parametrize("name,render", PAGES, ids=[p[0] for p in PAGES])
    def test_the_page_has_a_ribbon(self, name, render):
        assert '<div class="toolbar">' in _body(render())

    @pytest.mark.parametrize("name,render", PAGES, ids=[p[0] for p in PAGES])
    def test_the_ribbon_carries_the_axonllm_mark(self, name, render):
        """A text-only bar is what these had; the mark is the point."""
        body = _body(render())
        assert "axon-grad" in body
        assert 'class="brand"' in body

    @pytest.mark.parametrize("name,render", PAGES, ids=[p[0] for p in PAGES])
    def test_every_page_can_get_back_to_the_dashboard(self, name, render):
        assert 'class="action" href="/admin/dashboard"' in _body(render())

    @pytest.mark.parametrize("name,render", PAGES, ids=[p[0] for p in PAGES])
    def test_the_mark_links_to_the_landing_page(self, name, render):
        assert 'class="brand" href="/"' in _body(render())

    @pytest.mark.parametrize("name,render", PAGES, ids=[p[0] for p in PAGES])
    def test_the_page_has_a_favicon(self, name, render):
        assert 'rel="icon"' in render()

    def test_the_inlined_mark_is_valid_svg(self):
        """It was hand-converted from the dashboard's JSX.

        A missed camelCase attribute (strokeWidth, stopColor) renders nothing
        and raises nothing — the browser just draws an empty box.
        """
        svg = re.search(r"<svg viewBox.*?</svg>", ribbon("t"), re.S)
        assert svg, "no mark in the ribbon"
        ET.fromstring(svg.group(0))
        assert not re.search(
            r"\s(strokeWidth|strokeLinecap|strokeLinejoin|stopColor)=",
            svg.group(0),
        )

    def test_the_favicon_is_a_valid_svg_data_uri(self):
        import urllib.parse
        raw = re.search(r'href="data:image/svg\+xml,([^"]+)"', FAVICON)
        assert raw
        ET.fromstring(urllib.parse.unquote(raw.group(1)))

    def test_extra_links_render_before_the_dashboard_action(self):
        """One filled action per page.

        Secondary links are outlined; two filled buttons is two primary actions,
        which is none.
        """
        html = ribbon("Title", ("/admin/pricing-drift", "Pricing detail"))
        assert 'class="quiet" href="/admin/pricing-drift"' in html
        assert html.index("quiet") < html.index('class="action"')

    def test_the_title_is_the_page_not_the_product(self):
        """The brand block already says AxonLLM; repeating it wastes the slot."""
        html = ribbon("Pricing Coverage")
        assert '<span class="title">Pricing Coverage</span>' in html


class TestEmbedMode:
    """?embed=1 — the same pages rendered inside the dashboard shell.

    These were plain sidebar links that navigated the browser away, so clicking
    Architecture, Pricing or Readiness took the sidebar with it and the only way
    back was the ribbon. Framed in the main pane instead, they behave like every
    other nav item — which means suppressing the chrome the shell already has.
    """

    @pytest.mark.parametrize("name,render", EMBEDDED, ids=[p[0] for p in EMBEDDED])
    def test_the_ribbon_is_suppressed(self, name, render):
        """The shell's sidebar and topbar already carry the brand and the nav.

        Two stacked toolbars is the tell that a page was framed rather than
        built in.
        """
        assert '<div class="toolbar">' not in _body(render())

    @pytest.mark.parametrize("name,render", EMBEDDED, ids=[p[0] for p in EMBEDDED])
    def test_the_page_framing_is_reset(self, name, render):
        """A centered 1000px column inside an already-centered, already-padded
        pane reads as a misaligned card.

        Asserted as the CSS the page must actually contain, not as
        ``EMBED_STYLE in styles`` — that compares the constant to itself and so
        holds for any value, including an empty one.
        """
        styles = _styles(render())
        assert ".wrap{max-width:none;margin:0;padding:0}" in styles
        assert "body{background:transparent}" in styles

    @pytest.mark.parametrize("name,render", PAGES, ids=[p[0] for p in PAGES])
    def test_standalone_still_has_its_chrome(self, name, render):
        """The default. An operator who opens one of these from an alert, or
        from a bookmark, must still get a way back."""
        html = render()
        assert '<div class="toolbar">' in _body(html)
        assert ".wrap{max-width:none" not in _styles(html)
        # The wrap keeps its own centering and padding.
        assert ".wrap{max-width:1000px" in _styles(html)

    @pytest.mark.parametrize("name,render", EMBEDDED, ids=[p[0] for p in EMBEDDED])
    def test_embedding_only_removes_chrome_not_content(self, name, render):
        """The report itself must be identical either way.

        Embedding is a presentation change; a page that quietly dropped a
        failing check when framed would hide exactly what it exists to show.
        """
        assert '<div class="wrap">' in _body(render())
        assert "</body></html>" in render()


class TestNoUnstyledClasses:
    @pytest.mark.parametrize("name,render", PAGES, ids=[p[0] for p in PAGES])
    def test_every_class_used_has_a_rule(self, name, render):
        """Catches a rule renamed in the shared sheet but not in a page.

        An unstyled class is invisible in a passing test suite and obvious only
        to whoever opens the page.
        """
        html = render()
        used = {c for m in re.findall(r'class="([^"]+)"', _body(html))
                for c in m.split()}
        ruled = set(re.findall(r"\.([a-zA-Z][\w-]*)", _styles(html)))
        assert not used - ruled, f"{name}: unstyled classes {sorted(used - ruled)}"
