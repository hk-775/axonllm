#!/usr/bin/env python3
"""Stage 2 of 4: film the gateway as PNG frame sequences, one dir per scene.

    python3 scripts/demo/record.py

Requires a gateway already serving seeded demo data at paths.BASE_URL -- the
narration quotes the numbers these pages render, so filming an empty gateway
produces a film that contradicts its own voice-over. Start one with
``AXON_LOAD_DEMO_DATA=true python serve_dashboard.py`` first.

Design notes, written against what an earlier cut got wrong:

* 1600x812 is not a real delivery format. Captures at 1440x810 and encodes
  1920x1080, so the file plays full-frame on any screen instead of letterboxing
  into an odd size.
* Scenes cut hard from one page to another. Each scene now fades in and out, so
  a surface change reads as an edit rather than a glitch.
* The landing scroll stopped mid-section, leaving a heading sliced across the
  frame top. Targets are section tops, held briefly before moving.
* The dashboard was filmed as a still with the page scrolled to 0, so the
  sidebar's active item and the content below the fold never showed together.
  Each dashboard scene now settles, then drifts far enough to reveal its table.

Chrome is driven over the DevTools protocol: real clicks, real scrolling, one
PNG per frame. Deterministic and headless, so a rerun produces the same film.
"""
from __future__ import annotations

import base64
import json
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import websocket

from paths import BASE_URL as BASE
from paths import FRAMES as OUT
from paths import NARRATION, WORK

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333
# 16:9 at capture time so the encode to 1080p is a clean upscale with no bars.
W, H = 1440, 810
FPS = 30


class Tab:
    def __init__(self, ws_url: str) -> None:
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.n = 0

    def cmd(self, method: str, **params):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.n:
                if "error" in m:
                    raise RuntimeError(f"{method}: {m['error']}")
                return m.get("result", {})

    def js(self, expr: str):
        r = self.cmd("Runtime.evaluate", expression=expr, awaitPromise=True,
                     returnByValue=True)
        return r.get("result", {}).get("value")

    def shot(self, path: Path) -> None:
        r = self.cmd("Page.captureScreenshot", format="png")
        path.write_bytes(base64.b64decode(r["data"]))

    def goto(self, url: str, settle: float = 4.0) -> None:
        self.cmd("Page.navigate", url=url)
        time.sleep(settle)
        self.js("document.fonts.ready.then(()=>1)")
        time.sleep(0.5)


def ease(t: float) -> float:
    """easeInOutCubic. Linear scrolling reads as a machine, not a presenter."""
    return 4 * t**3 if t < 0.5 else 1 - pow(-2 * t + 2, 3) / 2


class Scene:
    """Accumulates frames for one narration segment."""

    def __init__(self, tab: Tab, name: str, seconds: float) -> None:
        self.tab, self.dir = tab, OUT / name
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True)
        self.total = max(1, int(seconds * FPS))
        self.i = 0

    @property
    def left(self) -> int:
        return self.total - self.i

    def _grab(self) -> None:
        if self.i < self.total:
            self.tab.shot(self.dir / f"{self.i:05d}.png")
            self.i += 1

    def hold(self, frames: int, setter=None, y: float = 0) -> None:
        """A still beat. Captures once and copies — screenshotting an unchanged
        page hundreds of times only costs wall-clock."""
        if setter:
            setter(y)
            time.sleep(0.45)
        n = min(frames, self.left)
        if n <= 0:
            return
        first = self.dir / f"{self.i:05d}.png"
        self.tab.shot(first)
        self.i += 1
        for _ in range(n - 1):
            shutil.copyfile(first, self.dir / f"{self.i:05d}.png")
            self.i += 1

    def glide(self, setter, y0: float, y1: float, frames: int) -> None:
        n = min(frames, self.left)
        for k in range(n):
            setter(y0 + (y1 - y0) * ease(k / max(1, n - 1)))
            self._grab()

    def fill(self, setter=None, y: float = 0) -> None:
        """Pad to the exact frame count the narration needs."""
        if self.left > 0:
            self.hold(self.left, setter, y)


def win(tab: Tab):
    return lambda y: tab.js(f"scrollTo(0,{y:.1f})")


# Marks the element this page actually scrolls, so the setters below can target
# it without re-running the search on every frame.
_FIND_SCROLLER = """(()=>{
  let best=null,room=0;
  for (const e of document.querySelectorAll('*')) {
    const r=e.scrollHeight-e.clientHeight;
    if (r<=room) continue;
    const o=getComputedStyle(e).overflowY;
    if (o!=='auto' && o!=='scroll') continue;
    // Content only. The sidebar is also overflow:auto and with eighteen nav
    // items it has scroll room, so widest-wins alone would film the menu
    // sliding past instead of the table being talked about.
    if (e.clientWidth < innerWidth * 0.5) continue;
    best=e; room=r;
  }
  const doc=document.documentElement.scrollHeight-innerHeight;
  document.querySelectorAll('[data-axon-scroll]').forEach(e=>e.removeAttribute('data-axon-scroll'));
  if (best && room>doc) { best.setAttribute('data-axon-scroll','1'); return room; }
  return doc;
})()"""


def scroller(tab: Tab):
    """Scroll whichever element the page actually scrolls.

    Three of the dashboard pages scroll the document, but Traces puts its table
    in a `maxHeight: calc(100vh - 380px); overflow: auto` box, so the document
    has no room at all and a window scroll is a no-op — which is how the first
    cut ended up holding a still frame over fifteen seconds of narration about
    scrolling through a trace log.
    """
    room = tab.js(_FIND_SCROLLER) or 0
    inner = tab.js("!!document.querySelector('[data-axon-scroll]')")

    if inner:
        def set_y(y: float) -> None:
            tab.js(f"document.querySelector('[data-axon-scroll]').scrollTop={y:.1f}")
    else:
        def set_y(y: float) -> None:
            tab.js(f"scrollTo(0,{y:.1f})")
    return set_y, int(room), bool(inner)


def frame_doc(tab: Tab):
    return lambda y: tab.js(
        "(()=>{const f=document.querySelector('iframe');"
        f"f.contentDocument.scrollingElement.scrollTop={y:.1f};}})()"
    )


# Bottom of the *contiguous* run of trafficked rows at the top of the table.
#
# Only some of the 48 catalogue rows carry seeded traffic, so a fixed scroll
# distance walks off the end of them into a wall of "0  0  $0.0000" — which is
# what the customer reads while the narration talks about routing strategy.
#
# Contiguous rather than last-trafficked: the rows are not sorted by traffic.
# gemini-2.5-pro has 6 requests but sits after two dozen empty rows, so scrolling
# to the last trafficked row crosses exactly the dead zone this is meant to
# avoid. Stopping at the first zero keeps every visible row populated.
#
# Measured rather than a constant: row height depends on the webfont and the
# two-line name cell, and which rows are populated changes with the seed.
LAST_LIVE_ROW = """(()=>{
  const rows=[...document.querySelectorAll('tbody tr')];
  let last=null;
  for (const tr of rows) {
    const tds=[...tr.querySelectorAll('td')].map(td=>td.textContent.trim());
    // Requests is the first cell that is a bare integer: the name cell and
    // $0.0000 are non-numeric, and the strategy cell is a <select>'s options.
    const req=tds.find(c=>/^[\\d,]+$/.test(c));
    if (req===undefined || req==='0') break;
    last=tr;
  }
  if (!last) return 0;
  return Math.round(last.getBoundingClientRect().bottom + scrollY);
})()"""


def click_nav(tab: Tab, label: str) -> bool:
    got = tab.js(
        "(()=>{const b=[...document.querySelectorAll('.nav-item')]"
        f".find(x=>x.textContent.trim().includes({label!r}));"
        "if(!b)return 'no';b.click();return 'yes';})()"
    )
    time.sleep(2.4)
    return got == "yes"


def main() -> None:
    scenes = json.loads(NARRATION.read_text())["scenes"]
    dur = {s["id"]: s["duration"] for s in scenes}
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)

    proc = subprocess.Popen(
        [CHROME, f"--remote-debugging-port={PORT}", "--headless=new",
         f"--remote-allow-origins=http://127.0.0.1:{PORT}",
         "--disable-gpu", "--hide-scrollbars", f"--window-size={W},{H}",
         "--force-device-scale-factor=1", "--no-first-run",
         f"--user-data-dir={WORK / 'chrome-profile'}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        ws = None
        for _ in range(40):
            try:
                ts = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/json", timeout=2).read())
                pg = [t for t in ts if t["type"] == "page"]
                if pg:
                    ws = pg[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not ws:
            raise SystemExit("devtools never came up")

        tab = Tab(ws)
        tab.cmd("Page.enable")
        tab.cmd("Runtime.enable")
        W_ = win(tab)

        # ---------------- landing ----------------
        tab.goto(BASE + "/", settle=5.0)
        # The nav is position:fixed, so scrolling a section to scrollY == its
        # offsetTop puts its heading *behind* the nav bar. Measured rather than
        # assumed: the value is padding + a 1.25rem logo line + a border, which
        # changes if any of those do.
        nav = tab.js("(()=>{const n=document.querySelector('nav');"
                     "return n?Math.ceil(n.getBoundingClientRect().height):0})()") or 0
        pos = tab.js("""(()=>{const y=s=>{const e=document.querySelector(s);
            return e?Math.round(e.getBoundingClientRect().top+scrollY):0};
            return {stats:y('.stats'),pipeline:y('.pipeline'),features:y('.features'),
                    code:y('.code-section'),providers:y('.providers'),cta:y('.cta-section'),
                    h:document.body.scrollHeight};})()""")
        # 20px of air below the nav so a heading is not flush against it.
        clear = nav + 20
        for k in ("stats", "pipeline", "features", "code", "providers", "cta"):
            pos[k] = max(0, pos[k] - clear)
        print(f"  landing: nav {nav}px, targets cleared by {clear}px -> {pos}")

        # Every landing target below is already nav-cleared, so the scenes use
        # them as-is. The old per-scene nudges (-120, -40, -60) were guesses at
        # the same correction and would now double-count it.

        # 1 — hero. Dwell on the lockup, then ease down toward the stat band,
        # stopping short so scene 2 has somewhere left to travel.
        approach = max(0, pos["stats"] - 150)
        s = Scene(tab, "01-hero", dur["01-hero"])
        s.hold(int(s.total * 0.45), W_, 0)
        s.glide(W_, 0, approach, int(s.total * 0.40))
        s.fill(W_, approach)
        print(f"  01-hero {s.i}f")

        # 2 — the stat band, landing squarely on it.
        s = Scene(tab, "02-stats", dur["02-stats"])
        s.glide(W_, approach, pos["stats"], int(s.total * 0.5))
        s.fill(W_, pos["stats"])
        print(f"  02-stats {s.i}f")

        # 3 — the pipeline diagram: arrive at its heading, then reveal the trace.
        s = Scene(tab, "03-pipeline", dur["03-pipeline"])
        s.glide(W_, pos["stats"], pos["pipeline"], int(s.total * 0.22))
        s.hold(int(s.total * 0.18))
        s.glide(W_, pos["pipeline"], pos["pipeline"] + 400, int(s.total * 0.45))
        s.fill(W_, pos["pipeline"] + 400)
        print(f"  03-pipeline {s.i}f")

        # 4 — the one-line change, held on the code block.
        s = Scene(tab, "04-integration", dur["04-integration"])
        s.glide(W_, pos["pipeline"] + 400, pos["code"], int(s.total * 0.40))
        s.hold(int(s.total * 0.25))
        s.glide(W_, pos["code"], pos["code"] + 300, int(s.total * 0.30))
        s.fill(W_, pos["code"] + 300)
        print(f"  04-integration {s.i}f")

        # 5 — the provider wall.
        s = Scene(tab, "05-providers", dur["05-providers"])
        s.glide(W_, pos["code"] + 300, pos["providers"], int(s.total * 0.45))
        s.fill(W_, pos["providers"])
        print(f"  05-providers {s.i}f")

        # ---------------- dashboard ----------------
        tab.goto(BASE + "/admin/dashboard", settle=7.0)
        n = tab.js("document.querySelectorAll('.nav-item').length")
        if not n:
            raise SystemExit("dashboard rendered no nav — React/JSX failed")
        print(f"  dashboard: {n} nav items")

        for sid, label, reveal in [
            ("06-overview", "Overview", 520),
            ("07-traces", "Traces", 560),
        ]:
            if not click_nav(tab, label):
                raise SystemExit(f"nav {label!r} missing")
            W_(0)
            time.sleep(0.7)
            set_y, room, inner = scroller(tab)
            set_y(0)
            time.sleep(0.4)
            if reveal is None:
                # Stop with the last trafficked row just above the fold rather
                # than scrolled past it.
                bottom = tab.js(LAST_LIVE_ROW) or 0
                reveal = max(0, bottom - H + 90)
                print(f"    {sid}: last live row ends at {bottom}px -> reveal {reveal}")
            end = min(reveal, max(0, room))
            s = Scene(tab, sid, dur[sid])
            # Settle on the page header first so the customer sees which page
            # this is before it starts moving.
            s.hold(int(s.total * 0.30), set_y, 0)
            if end > 30:
                s.glide(set_y, 0, end, int(s.total * 0.55))
                s.fill(set_y, end)
            else:
                # No room anywhere on the page is a real possibility (Overview
                # fits), but it must be a deliberate still, not a silent one.
                print(f"    ! {sid}: nothing to scroll (room {room}) — holding")
                s.fill()
            print(f"  {sid} {s.i}f (0->{end} of {room}, {'inner box' if inner else 'document'})")

        # 8 — the catalogue. Not a scroll: every trafficked row already fits
        # above the fold (the last one ends at ~611px of an 810px viewport), so
        # scrolling here would leave the populated rows behind and spend the
        # narration on empty ones. The narration is about per-model routing
        # strategy, so film that instead — open the strategy select on the row
        # being described and let the four options be the motion.
        if not click_nav(tab, "Models"):
            raise SystemExit("nav 'Models' missing")
        set_y, room, _ = scroller(tab)
        set_y(0)
        time.sleep(0.6)
        s = Scene(tab, "08-models", dur["08-models"])
        s.hold(int(s.total * 0.26), set_y, 0)
        # Nudge down just enough to centre the trafficked block, no further.
        s.glide(set_y, 0, 120, int(s.total * 0.16))
        # Focus-ring the selects in turn. A headless Chrome will not paint a
        # native dropdown's popup, so the beat is the focus ring landing on each
        # row's control — visible, and it does not change any routing.
        rows = tab.js("document.querySelectorAll('tbody tr select').length") or 0
        beats = min(4, rows)
        per = max(1, int(s.total * 0.44) // max(1, beats))
        for r in range(beats):
            tab.js(f"(()=>{{const e=document.querySelectorAll('tbody tr select')[{r}];"
                   "if(e){e.focus();e.scrollIntoView({block:'center'});}})()")
            time.sleep(0.35)
            s.hold(per)
        tab.js("document.activeElement && document.activeElement.blur()")
        s.fill()
        print(f"  08-models {s.i}f ({beats} strategy beats, room {room})")

        # 9 — the framed report page; its own document scrolls.
        if not click_nav(tab, "Pricing"):
            raise SystemExit("nav 'Pricing' missing")
        F_ = frame_doc(tab)
        W_(0)
        F_(0)
        time.sleep(0.8)
        room = tab.js("(()=>{const d=document.querySelector('iframe')"
                      ".contentDocument.scrollingElement;"
                      "return d.scrollHeight-d.clientHeight})()") or 0
        s = Scene(tab, "09-pricing", dur["09-pricing"])
        s.hold(int(s.total * 0.32), F_, 0)
        s.glide(F_, 0, min(430, room), int(s.total * 0.50))
        s.fill(F_, min(430, room))
        print(f"  09-pricing {s.i}f (room {room})")

        # 10 — close on the CTA.
        tab.goto(BASE + "/", settle=5.0)
        s = Scene(tab, "10-close", dur["10-close"])
        s.glide(W_, pos["cta"] - 80, pos["cta"] + 200, int(s.total * 0.55))
        s.fill(W_, pos["cta"] + 200)
        print(f"  10-close {s.i}f")

        print(f"\n  total {sum(1 for _ in OUT.rglob('*.png'))} frames")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    main()
