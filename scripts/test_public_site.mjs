#!/usr/bin/env node

import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { createServer, request as httpRequest } from "node:http";
import { tmpdir } from "node:os";
import { dirname, extname, join, resolve, sep } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const publicRoot = resolve(
  process.argv[2] || join(repositoryRoot, "build", "public-site"),
);
const publicBase = "/axonllm/";
const screenshotDirectory = process.env.AXONLLM_E2E_SCREENSHOT_DIR;

if (typeof WebSocket !== "function") {
  throw new Error("The public-site browser test requires Node.js 22 or newer.");
}
if (!existsSync(join(publicRoot, "index.html"))) {
  throw new Error(`Public-site build not found at ${publicRoot}. Build it before testing.`);
}

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".drawio": "application/xml; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".mp3": "audio/mpeg",
  ".mp4": "video/mp4",
  ".png": "image/png",
  ".svg": "image/svg+xml",
  ".vtt": "text/vtt; charset=utf-8",
};

function findChrome() {
  const candidates = [
    process.env.CHROME_BIN,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
  ].filter(Boolean);

  for (const candidate of candidates) {
    if (existsSync(candidate)) return candidate;
  }

  for (const command of [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
  ]) {
    const found = spawnSync("which", [command], { encoding: "utf8" });
    if (found.status === 0 && found.stdout.trim()) return found.stdout.trim();
  }

  throw new Error("Chrome or Chromium is required for the public-site browser test.");
}

function parseRange(value, length) {
  if (!value) return null;
  const match = /^bytes=(\d*)-(\d*)$/.exec(value);
  if (!match || (!match[1] && !match[2])) return { invalid: true };

  let start;
  let end;
  if (!match[1]) {
    const suffixLength = Number(match[2]);
    if (!Number.isSafeInteger(suffixLength) || suffixLength <= 0) {
      return { invalid: true };
    }
    start = Math.max(0, length - suffixLength);
    end = length - 1;
  } else {
    start = Number(match[1]);
    end = match[2] ? Number(match[2]) : length - 1;
  }

  if (
    !Number.isSafeInteger(start)
    || !Number.isSafeInteger(end)
    || start < 0
    || end < start
    || start >= length
  ) {
    return { invalid: true };
  }
  return { start, end: Math.min(end, length - 1) };
}

async function startStaticServer() {
  const requests = [];
  const server = createServer(async (request, response) => {
    let pathname = "/";
    const finish = (status, headers = {}, body = "") => {
      requests.push({ method: request.method || "GET", pathname, status });
      response.writeHead(status, headers);
      if (request.method === "HEAD") response.end();
      else response.end(body);
    };

    try {
      const url = new URL(request.url || "/", "http://127.0.0.1");
      pathname = decodeURIComponent(url.pathname);
      if (pathname === publicBase.slice(0, -1) || pathname === publicBase) {
        pathname = `${publicBase}index.html`;
      }
      if (!pathname.startsWith(publicBase)) {
        finish(404, { "content-type": "text/plain; charset=utf-8" }, "Not found");
        return;
      }

      const relativePath = pathname.slice(publicBase.length);
      const filePath = resolve(publicRoot, relativePath);
      if (filePath !== publicRoot && !filePath.startsWith(`${publicRoot}${sep}`)) {
        finish(403, { "content-type": "text/plain; charset=utf-8" }, "Forbidden");
        return;
      }

      const body = await readFile(filePath);
      const headers = {
        "accept-ranges": "bytes",
        "cache-control": "no-store",
        "content-type": contentTypes[extname(filePath)] || "application/octet-stream",
      };
      const range = parseRange(request.headers.range, body.length);
      if (range?.invalid) {
        finish(416, {
          ...headers,
          "content-range": `bytes */${body.length}`,
          "content-length": "0",
        });
        return;
      }
      if (range) {
        const partial = body.subarray(range.start, range.end + 1);
        finish(206, {
          ...headers,
          "content-range": `bytes ${range.start}-${range.end}/${body.length}`,
          "content-length": String(partial.length),
        }, partial);
        return;
      }
      finish(200, { ...headers, "content-length": String(body.length) }, body);
    } catch {
      finish(404, { "content-type": "text/plain; charset=utf-8" }, "Not found");
    }
  });

  await new Promise((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolveListen);
  });
  const address = server.address();
  const port = typeof address === "object" && address ? address.port : 0;
  return { server, origin: `http://127.0.0.1:${port}`, requests };
}

function requestJson(url, method = "GET") {
  return new Promise((resolveRequest, reject) => {
    const request = httpRequest(url, { method }, (response) => {
      let body = "";
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        body += chunk;
      });
      response.on("end", () => {
        if (!response.statusCode || response.statusCode < 200 || response.statusCode >= 300) {
          reject(new Error(`HTTP ${response.statusCode || "unknown"}: ${body}`));
          return;
        }
        try {
          resolveRequest(JSON.parse(body));
        } catch (error) {
          reject(new Error(`Invalid JSON from ${url}: ${error}`));
        }
      });
    });
    request.setTimeout(2_000, () => {
      request.destroy(new Error(`Timed out requesting ${url}`));
    });
    request.once("error", reject);
    request.end();
  });
}

function requestStatus(url, method = "HEAD") {
  return new Promise((resolveRequest, reject) => {
    const request = httpRequest(url, { method }, (response) => {
      response.resume();
      response.on("end", () => resolveRequest(response.statusCode || 0));
    });
    request.setTimeout(4_000, () => {
      request.destroy(new Error(`Timed out requesting ${url}`));
    });
    request.once("error", reject);
    request.end();
  });
}

async function pollJson(url, chrome) {
  let lastError;
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (chrome.exitCode !== null) {
      throw new Error(`Chrome exited before DevTools became available (code ${chrome.exitCode}).`);
    }
    try {
      return await requestJson(url);
    } catch (error) {
      lastError = error;
    }
    await delay(100);
  }
  throw new Error(`Timed out waiting for Chrome DevTools: ${lastError}`);
}

async function waitForDevToolsUrl(chrome, getOutput) {
  for (let attempt = 0; attempt < 120; attempt += 1) {
    if (chrome.exitCode !== null) {
      throw new Error(
        `Chrome exited before DevTools became available (code ${chrome.exitCode}).`,
      );
    }
    const match = getOutput().match(/DevTools listening on (ws:\/\/\S+)/);
    if (match) return match[1];
    await delay(100);
  }
  throw new Error("Timed out waiting for Chrome to announce its DevTools endpoint.");
}

class CdpSession {
  constructor(socket) {
    this.socket = socket;
    this.nextId = 1;
    this.pending = new Map();
    this.listeners = new Map();

    socket.addEventListener("message", (event) => {
      const message = JSON.parse(String(event.data));
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(message.error.message));
        else pending.resolve(message.result || {});
        return;
      }
      const listeners = this.listeners.get(message.method);
      if (!listeners) return;
      for (const listener of [...listeners]) listener(message.params || {});
    });
  }

  static async connect(url) {
    const socket = new WebSocket(url);
    await new Promise((resolveOpen, reject) => {
      socket.addEventListener("open", resolveOpen, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    return new CdpSession(socket);
  }

  send(method, params = {}) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolveResult, reject) => {
      this.pending.set(id, { resolve: resolveResult, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  on(method, listener) {
    const listeners = this.listeners.get(method) || new Set();
    listeners.add(listener);
    this.listeners.set(method, listeners);
    return () => listeners.delete(listener);
  }

  once(method, timeoutMs = 10_000) {
    return new Promise((resolveEvent, reject) => {
      const timer = setTimeout(() => {
        unsubscribe();
        reject(new Error(`Timed out waiting for Chrome event ${method}`));
      }, timeoutMs);
      const unsubscribe = this.on(method, (params) => {
        clearTimeout(timer);
        unsubscribe();
        resolveEvent(params);
      });
    });
  }

  close() {
    this.socket.close();
  }
}

async function evaluate(cdp, expression) {
  const result = await cdp.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
  if (result.exceptionDetails) {
    const description = result.exceptionDetails.exception?.description
      || result.exceptionDetails.text
      || "Browser evaluation failed";
    throw new Error(description);
  }
  return result.result?.value;
}

async function waitFor(cdp, expression, description, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  let lastError;
  while (Date.now() < deadline) {
    try {
      const value = await evaluate(cdp, expression);
      if (value) return value;
    } catch (error) {
      lastError = error;
    }
    await delay(50);
  }
  throw new Error(`Timed out waiting for ${description}${lastError ? `: ${lastError}` : ""}`);
}

async function click(cdp, selector) {
  const serialized = JSON.stringify(selector);
  await evaluate(cdp, `(() => {
    const element = document.querySelector(${serialized});
    if (!element) return false;
    element.scrollIntoView({ block: "center", inline: "center" });
    return true;
  })()`);
  await delay(30);
  const rect = await evaluate(cdp, `(() => {
    const element = document.querySelector(${serialized});
    if (!element) return null;
    const bounds = element.getBoundingClientRect();
    return {
      x: bounds.left + bounds.width / 2,
      y: bounds.top + bounds.height / 2,
      disabled: Boolean(element.disabled),
    };
  })()`);
  assert.ok(rect, `Missing clickable element ${selector}`);
  assert.equal(rect.disabled, false, `Element is disabled: ${selector}`);
  await cdp.send("Input.dispatchMouseEvent", {
    type: "mousePressed",
    x: rect.x,
    y: rect.y,
    button: "left",
    clickCount: 1,
  });
  await cdp.send("Input.dispatchMouseEvent", {
    type: "mouseReleased",
    x: rect.x,
    y: rect.y,
    button: "left",
    clickCount: 1,
  });
  await delay(50);
}

async function navigate(cdp, url) {
  const loaded = cdp.once("Page.loadEventFired");
  await cdp.send("Page.navigate", { url });
  await loaded;
}

async function captureScreenshot(cdp, name) {
  if (!screenshotDirectory) return;
  const result = await cdp.send("Page.captureScreenshot", {
    captureBeyondViewport: false,
    format: "png",
    fromSurface: true,
  });
  await writeFile(join(screenshotDirectory, name), Buffer.from(result.data, "base64"));
}

function hasSuccessfulResponse(responses, suffix) {
  return responses.some(({ status, url }) => (
    url.endsWith(suffix) && (status === 200 || status === 206)
  ));
}

const { server, origin, requests: serverRequests } = await startStaticServer();
const profileDirectory = await mkdtemp(join(tmpdir(), "axonllm-pages-chrome-"));
const chromePath = findChrome();
let chromeOutput = "";
const chromeArgs = [
  "--headless",
  "--autoplay-policy=no-user-gesture-required",
  "--disable-background-networking",
  "--disable-component-update",
  "--disable-default-apps",
  "--disable-dev-shm-usage",
  "--disable-extensions",
  "--disable-gpu",
  "--disable-sync",
  "--metrics-recording-only",
  "--mute-audio",
  "--no-default-browser-check",
  "--no-first-run",
  "--remote-debugging-address=127.0.0.1",
  "--remote-debugging-port=0",
  `--user-data-dir=${profileDirectory}`,
  "--window-size=1440,1000",
  "about:blank",
];
if (process.platform === "linux") chromeArgs.unshift("--no-sandbox");

const chrome = spawn(chromePath, chromeArgs, {
  stdio: ["ignore", "pipe", "pipe"],
});
for (const stream of [chrome.stdout, chrome.stderr]) {
  stream.setEncoding("utf8");
  stream.on("data", (chunk) => {
    chromeOutput = `${chromeOutput}${chunk}`.slice(-12_000);
  });
}

let cdp;
const browserExceptions = [];
const consoleErrors = [];
const responses = [];

try {
  for (const asset of [
    "",
    "architecture.html",
    "architecture-flow.css",
    "architecture-flow.js",
    "architecture-infrastructure.svg",
    "architecture-pipeline.svg",
    "architecture-components.svg",
    "narration/architecture-narration.json",
    "narration/smart-chat-0.mp3",
    "axonllm-demo.mp4",
    "axonllm-demo.vtt",
  ]) {
    assert.equal(
      await requestStatus(`${origin}${publicBase}${asset}`),
      200,
      `Public asset did not resolve: ${asset || "index.html"}`,
    );
  }

  const browserWebSocketUrl = await waitForDevToolsUrl(chrome, () => chromeOutput);
  const devToolsOrigin = `http://${new URL(browserWebSocketUrl).host}`;
  await pollJson(`${devToolsOrigin}/json/version`, chrome);
  const target = await requestJson(
    `${devToolsOrigin}/json/new?${encodeURIComponent("about:blank")}`,
    "PUT",
  );
  cdp = await CdpSession.connect(target.webSocketDebuggerUrl);

  await cdp.send("Page.enable");
  await cdp.send("Runtime.enable");
  await cdp.send("Network.enable");
  await cdp.send("Network.setCacheDisabled", { cacheDisabled: true });
  await cdp.send("Network.setBlockedURLs", {
    urls: ["https://fonts.googleapis.com/*", "https://fonts.gstatic.com/*"],
  });
  cdp.on("Runtime.exceptionThrown", ({ exceptionDetails }) => {
    browserExceptions.push(
      exceptionDetails?.exception?.description || exceptionDetails?.text || "Unknown exception",
    );
  });
  cdp.on("Runtime.consoleAPICalled", ({ type, args }) => {
    if (type !== "error") return;
    consoleErrors.push(args.map((argument) => argument.value || argument.description || "").join(" "));
  });
  cdp.on("Network.responseReceived", ({ response }) => {
    if (response?.url) responses.push({ status: response.status, url: response.url });
  });

  await navigate(cdp, `${origin}${publicBase}`);
  await waitFor(
    cdp,
    `document.querySelector('[data-axon-flow]')?.dataset.ready === "true"`,
    "the landing-page request flow",
  );
  assert.equal(
    await evaluate(cdp, "document.title"),
    "AxonLLM — The Neural Control Plane for Enterprise LLMs",
  );
  const landing = await evaluate(cdp, `(() => {
    const image = document.querySelector('img[src="dashboard-showcase.png"]');
    const links = [...document.querySelectorAll("a")].map((anchor) => ({
      href: anchor.getAttribute("href"),
      resolved: anchor.href,
      text: anchor.textContent.trim(),
    }));
    return {
      copy: document.body.textContent,
      imageComplete: Boolean(image?.complete && image?.naturalWidth > 0),
      imageUrl: image?.src || "",
      links,
      scenarioCount: document.querySelectorAll("[data-scenario]").length,
      currentNode: document.querySelector(".axon-flow__node.is-current")?.dataset.node,
    };
  })()`);
  assert.match(landing.copy, /One API\.\s*Every model\.\s*Full control\./);
  assert.match(landing.copy, /Interactive Architecture/);
  assert.match(landing.copy, /Watch Product Film/);
  assert.equal(landing.imageComplete, true);
  assert.match(landing.imageUrl, /\/axonllm\/dashboard-showcase\.png$/);
  assert.equal(landing.scenarioCount, 4);
  assert.equal(landing.currentNode, "client-chat");
  assert.equal(
    landing.links.some(({ href }) => typeof href === "string" && href.startsWith("/")),
    false,
    "The public build still contains a root-relative link.",
  );
  assert.ok(
    landing.links.some(({ text, resolved }) => (
      text.includes("Architecture") && resolved.endsWith("/axonllm/architecture.html")
    )),
    "The landing page does not link to the published architecture page.",
  );
  assert.ok(
    landing.links.some(({ text, resolved }) => (
      text.includes("Open Seeded Dashboard")
      && resolved === "https://github.com/hk-775/axonllm#quick-start"
    )),
    "The static dashboard CTA does not explain how to run the live gateway.",
  );

  await click(cdp, "[data-flow-next]");
  await waitFor(
    cdp,
    `document.querySelector("[data-flow-step-num]")?.textContent === "02"`,
    "the animated request flow to advance",
  );
  assert.match(
    await evaluate(
      cdp,
      "getComputedStyle(document.querySelector('.axon-flow__edge.is-current')).animationName",
    ),
    /axon-flow-dash/,
  );
  const packetAnimation = await evaluate(cdp, `(() => {
    const packet = document.querySelector("[data-flow-packet]");
    return {
      display: packet ? getComputedStyle(packet).display : "",
      hasMotion: Boolean(packet?.querySelector("animateMotion")),
      hiddenAttribute: Boolean(packet?.hasAttribute("hidden")),
      hiddenProperty: Boolean(packet?.hidden),
    };
  })()`);
  assert.equal(packetAnimation.hasMotion, true, "The animated request packet is missing.");
  assert.notEqual(
    packetAnimation.display,
    "none",
    `The animated request packet is hidden: ${JSON.stringify(packetAnimation)}`,
  );
  await click(cdp, "[data-flow-reset]");

  await click(cdp, "[data-flow-play]");
  await waitFor(
    cdp,
    `document.querySelector("[data-axon-flow]")?.dataset.narrationState === "playing"`,
    "interactive narration playback",
  );
  await waitFor(
    cdp,
    `(() => {
      const audio = document.querySelector("[data-flow-narration]");
      return audio && !audio.paused && audio.readyState >= 2 && audio.currentTime > 0;
    })()`,
    "interactive narration audio data",
  );
  const flowAudio = await evaluate(cdp, `(() => {
    const audio = document.querySelector("[data-flow-narration]");
    return {
      currentSrc: audio.currentSrc,
      paused: audio.paused,
      readyState: audio.readyState,
    };
  })()`);
  assert.match(flowAudio.currentSrc, /\/axonllm\/narration\/smart-chat-0\.mp3$/);
  assert.equal(flowAudio.paused, false);
  assert.ok(flowAudio.readyState >= 2);
  assert.equal(hasSuccessfulResponse(responses, "/narration/smart-chat-0.mp3"), true);
  await click(cdp, "[data-flow-play]");
  await waitFor(
    cdp,
    `document.querySelector("[data-flow-narration]")?.paused === true`,
    "interactive narration to pause",
  );
  await click(cdp, '[data-scenario="1"]');
  assert.equal(
    await evaluate(cdp, `document.querySelector('[data-scenario="1"]')?.getAttribute("aria-selected")`),
    "true",
  );

  await click(cdp, "[data-demo-open]");
  assert.equal(await evaluate(cdp, "document.getElementById('demo-modal')?.open"), true);
  assert.equal(
    await evaluate(cdp, "document.getElementById('demo-video')?.getAttribute('src')"),
    "axonllm-demo.mp4",
  );
  await click(cdp, "[data-demo-play]");
  await waitFor(
    cdp,
    `(() => {
      const video = document.getElementById("demo-video");
      return video && !video.paused && video.readyState >= 2;
    })()`,
    "product-film playback",
    15_000,
  );
  assert.equal(hasSuccessfulResponse(responses, "/axonllm-demo.mp4"), true);
  await evaluate(cdp, "document.getElementById('demo-video').pause()");
  await click(cdp, "[data-demo-close]");
  await captureScreenshot(cdp, "axonllm-landing.png");

  await navigate(cdp, `${origin}${publicBase}architecture.html`);
  await waitFor(
    cdp,
    `document.querySelector('[data-axon-flow]')?.dataset.ready === "true"`,
    "the architecture-page request flow",
  );
  await waitFor(
    cdp,
    `document.getElementById("panel-infrastructure")?.dataset.loaded === "done"`,
    "the infrastructure SVG",
  );
  await waitFor(
    cdp,
    `document.getElementById("narration")?.hidden === false`,
    "the architecture narration manifest",
  );
  assert.equal(await evaluate(cdp, "document.title"), "AxonLLM — Architecture");
  assert.equal(
    await evaluate(cdp, "document.querySelectorAll('[data-scenario]').length"),
    4,
  );
  assert.equal(
    await evaluate(
      cdp,
      "Boolean(document.querySelector('#panel-infrastructure svg[role=\"img\"]'))",
    ),
    true,
  );
  assert.equal(
    hasSuccessfulResponse(responses, "/architecture-infrastructure.svg"),
    true,
  );
  assert.equal(
    hasSuccessfulResponse(responses, "/narration/architecture-narration.json"),
    true,
  );

  await click(cdp, "#narr-play");
  await waitFor(
    cdp,
    `document.getElementById("narration")?.dataset.state === "playing"`,
    "architecture narration playback",
  );
  await waitFor(
    cdp,
    `document.getElementById("narr-time")?.textContent !== "0:00 / 1:21"`,
    "architecture narration progress",
  );
  assert.equal(hasSuccessfulResponse(responses, "/narration/infrastructure.mp3"), true);
  await evaluate(cdp, "document.getElementById('narr-track').focus()");
  await cdp.send("Input.dispatchKeyEvent", {
    type: "keyDown",
    key: "ArrowRight",
    code: "ArrowRight",
  });
  await cdp.send("Input.dispatchKeyEvent", {
    type: "keyUp",
    key: "ArrowRight",
    code: "ArrowRight",
  });
  await waitFor(
    cdp,
    `Boolean(document.querySelector("#panel-infrastructure.is-dimmed .arch-cell.is-lit"))`,
    "narration-driven diagram highlighting",
  );
  await click(cdp, "#narr-play");
  await waitFor(
    cdp,
    `document.getElementById("narration")?.dataset.state === "paused"`,
    "architecture narration to pause",
  );
  assert.equal(
    await evaluate(cdp, "document.querySelector('#panel-infrastructure.is-dimmed') === null"),
    true,
    "Pausing narration left the architecture diagram dimmed.",
  );

  const zoomBefore = await evaluate(cdp, "document.getElementById('zoom-level').textContent");
  await click(cdp, "#zoom-in");
  const zoomAfter = await evaluate(cdp, "document.getElementById('zoom-level').textContent");
  assert.notEqual(zoomAfter, zoomBefore);
  await click(cdp, "#narr-transcript-btn");
  assert.equal(
    await evaluate(cdp, "document.getElementById('narr-transcript').hidden"),
    false,
  );
  assert.match(
    await evaluate(cdp, "document.getElementById('narr-transcript-text').textContent"),
    /production AWS topology/,
  );

  await click(cdp, "#tab-pipeline");
  await waitFor(
    cdp,
    `document.getElementById("panel-pipeline")?.dataset.loaded === "done"`,
    "the request-pipeline SVG",
  );
  assert.match(
    await evaluate(cdp, "document.getElementById('narr-title').textContent"),
    /Request Pipeline/,
  );
  await click(cdp, "#narr-play");
  await waitFor(
    cdp,
    `document.getElementById("narration")?.dataset.state === "playing"`,
    "request-pipeline narration playback",
  );
  await waitFor(
    cdp,
    `document.getElementById("narr-time")?.textContent !== "0:00 / 1:48"`,
    "request-pipeline narration progress",
  );
  assert.equal(hasSuccessfulResponse(responses, "/narration/pipeline.mp3"), true);
  await click(cdp, "#narr-play");

  await click(cdp, "#tab-components");
  await waitFor(
    cdp,
    `document.getElementById("panel-components")?.dataset.loaded === "done"`,
    "the component SVG",
  );
  assert.equal(hasSuccessfulResponse(responses, "/architecture-components.svg"), true);
  await captureScreenshot(cdp, "axonllm-architecture.png");

  await cdp.send("Emulation.setDeviceMetricsOverride", {
    width: 390,
    height: 844,
    deviceScaleFactor: 1,
    mobile: true,
  });
  await delay(150);
  assert.equal(
    await evaluate(
      cdp,
      "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1",
    ),
    true,
    "The public architecture page overflows the mobile viewport.",
  );

  const failedLocalRequests = serverRequests.filter(({ status }) => status >= 400);
  assert.deepEqual(failedLocalRequests, []);
  assert.deepEqual(browserExceptions, []);
  assert.deepEqual(consoleErrors, []);
  console.log(
    "public site e2e OK: canonical landing, interactive animation, MP3 narration, "
      + "product film, three SVG architecture views, highlighting, zoom, and mobile layout",
  );
} catch (error) {
  if (chromeOutput) {
    console.error("Chrome output (tail):\n", chromeOutput);
  }
  throw error;
} finally {
  cdp?.close();
  await new Promise((resolveClose) => server.close(resolveClose));
  if (chrome.exitCode === null) chrome.kill("SIGTERM");
  await Promise.race([
    new Promise((resolveExit) => chrome.once("exit", resolveExit)),
    delay(2_000),
  ]);
  if (chrome.exitCode === null) {
    chrome.kill("SIGKILL");
    await Promise.race([
      new Promise((resolveExit) => chrome.once("exit", resolveExit)),
      delay(2_000),
    ]);
  }
  await rm(profileDirectory, {
    recursive: true,
    force: true,
    maxRetries: 5,
    retryDelay: 100,
  });
}
