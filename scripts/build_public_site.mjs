#!/usr/bin/env node

import assert from "node:assert/strict";
import {
  copyFile,
  mkdir,
  readFile,
  readdir,
  rm,
  writeFile,
} from "node:fs/promises";
import { dirname, extname, join, parse, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const sourceRoot = join(repositoryRoot, "site");
const packagedRoot = join(
  repositoryRoot,
  "src",
  "gateway",
  "resources",
  "runtime",
  "site",
);
const outputRoot = resolve(
  process.argv[2] || join(repositoryRoot, "build", "public-site"),
);

const topLevelExtensions = new Set([
  ".css",
  ".drawio",
  ".html",
  ".js",
  ".mp4",
  ".png",
  ".svg",
  ".vtt",
]);
const narrationExtensions = new Set([".json", ".mp3"]);

function assertSafeOutputPath() {
  const filesystemRoot = parse(outputRoot).root;
  const protectedPaths = new Set([filesystemRoot, repositoryRoot, sourceRoot, packagedRoot]);
  if (protectedPaths.has(outputRoot)) {
    throw new Error(`Refusing to replace protected path: ${outputRoot}`);
  }
}

function validatePublicPath(relativePath) {
  const parts = relativePath.split(sep);
  const extension = extname(relativePath).toLowerCase();
  if (parts.length === 1 && topLevelExtensions.has(extension)) return;
  if (
    parts.length === 2
    && parts[0] === "narration"
    && narrationExtensions.has(extension)
  ) {
    return;
  }
  throw new Error(`Unexpected file in the public site source: ${relativePath}`);
}

async function listPublicFiles(root, current = "") {
  const directory = join(root, current);
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];

  for (const entry of entries.sort((left, right) => left.name.localeCompare(right.name))) {
    const relativePath = current ? join(current, entry.name) : entry.name;
    if (!current && (entry.name === "deploy.sh" || entry.name === "infra")) continue;
    if (entry.isSymbolicLink()) {
      throw new Error(`Public site source must not contain symlinks: ${relativePath}`);
    }
    if (entry.isDirectory()) {
      if (relativePath !== "narration") {
        throw new Error(`Unexpected directory in the public site source: ${relativePath}`);
      }
      files.push(...await listPublicFiles(root, relativePath));
      continue;
    }
    if (!entry.isFile()) {
      throw new Error(`Unsupported public site entry: ${relativePath}`);
    }
    validatePublicPath(relativePath);
    files.push(relativePath);
  }

  return files;
}

async function assertPackagedSiteMatches(sourceFiles) {
  const packagedFiles = await listPublicFiles(packagedRoot);
  assert.deepEqual(
    packagedFiles,
    sourceFiles,
    "site/ and the packaged runtime site have different public file lists",
  );

  for (const relativePath of sourceFiles) {
    const [source, packaged] = await Promise.all([
      readFile(join(sourceRoot, relativePath)),
      readFile(join(packagedRoot, relativePath)),
    ]);
    assert.deepEqual(
      packaged,
      source,
      `${relativePath} differs between site/ and the packaged runtime site`,
    );
  }
}

function publicHtml(source, relativePath) {
  let html = source;

  // The canonical files also run at the root of a live AxonLLM gateway.
  // GitHub Pages hosts them below /axonllm/, so only URL targets change here;
  // markup, copy, styling, animation, narration, and media remain byte-for-byte
  // from the website source.
  html = html.replaceAll(
    'href="/admin/dashboard?tour=1"',
    'href="https://github.com/hk-775/axonllm#quick-start"',
  );
  html = html.replaceAll(
    'href="/admin/dashboard"',
    'href="https://github.com/hk-775/axonllm#quick-start"',
  );
  html = html.replaceAll(
    'href="/admin/architecture"',
    'href="architecture.html"',
  );
  html = html.replaceAll('href="/#', 'href="./#');
  html = html.replaceAll('href="/"', 'href="./"');

  const rootRelativeAsset = /\b(?:href|src)=["']\//.exec(html);
  if (rootRelativeAsset) {
    throw new Error(
      `${relativePath} still contains a root-relative public URL: ${rootRelativeAsset[0]}`,
    );
  }
  return html;
}

async function build() {
  assertSafeOutputPath();
  const sourceFiles = await listPublicFiles(sourceRoot);
  await assertPackagedSiteMatches(sourceFiles);

  await rm(outputRoot, { recursive: true, force: true });
  await mkdir(outputRoot, { recursive: true });

  for (const relativePath of sourceFiles) {
    const sourcePath = join(sourceRoot, relativePath);
    const outputPath = join(outputRoot, relativePath);
    await mkdir(dirname(outputPath), { recursive: true });
    if (extname(relativePath).toLowerCase() === ".html") {
      const html = await readFile(sourcePath, "utf8");
      await writeFile(outputPath, publicHtml(html, relativePath), "utf8");
    } else {
      await copyFile(sourcePath, outputPath);
    }
  }

  await writeFile(join(outputRoot, ".nojekyll"), "", "utf8");
  console.log(
    `public site built: ${sourceFiles.length} canonical assets -> ${relative(
      repositoryRoot,
      outputRoot,
    ) || outputRoot}`,
  );
}

await build();
