#!/usr/bin/env node

const { spawnSync } = require("node:child_process");
const fs = require("node:fs/promises");
const path = require("node:path");
const sharp = require("sharp");

const repoRoot = path.resolve(__dirname, "..");
const outputDir = path.join(
  repoRoot,
  "twocomms/twocomms_django_theme/static/img/configurator/studio",
);

const python = [
  "import importlib.util, json, pathlib",
  "p = pathlib.Path('twocomms/storefront/custom_print_stage_art.py')",
  "spec = importlib.util.spec_from_file_location('custom_print_stage_art', p)",
  "m = importlib.util.module_from_spec(spec)",
  "spec.loader.exec_module(m)",
  "print(json.dumps(m.build_stage_art()))",
].join("; ");

const result = spawnSync("python3", ["-c", python], {
  cwd: repoRoot,
  encoding: "utf8",
});
if (result.status !== 0) {
  throw new Error(result.stderr || "Could not load custom print stage art");
}

const art = JSON.parse(result.stdout);
const profiles = [
  ["hoodie-regular", art.hoodie.regular],
  ["hoodie-oversize", art.hoodie.oversize],
  ["tshirt-regular", art.tshirt.regular],
  ["tshirt-oversize", art.tshirt.oversize],
  ["longsleeve", art.longsleeve.default],
];

function wrapSvg(markup, extraCss = "") {
  return `
    <svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1400" viewBox="0 0 420 520" preserveAspectRatio="xMidYMid meet">
      <style>
        .cp-stage-svg__cords--lacing { display: none; }
        ${extraCss}
      </style>
      ${markup}
    </svg>
  `;
}

async function render(name, markup, extraCss = "") {
  const target = path.join(outputDir, name);
  await sharp(Buffer.from(wrapSvg(markup, extraCss)))
    .png({ compressionLevel: 9, adaptiveFiltering: true })
    .toFile(target);
}

async function main() {
  await fs.mkdir(outputDir, { recursive: true });
  for (const [name, profile] of profiles) {
    await render(`${name}-front.png`, profile.front.svg);
    await render(`${name}-back.png`, profile.back.svg);
  }

  const lacing = `
    <defs>
      <linearGradient id="metal" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#f1f1f3"/>
        <stop offset="1" stop-color="#696b72"/>
      </linearGradient>
    </defs>
    <g fill="none" stroke="#d7d2c8" stroke-width="4.2" stroke-linecap="round">
      <path d="M193 190 C186 217 197 239 190 263"/>
      <path d="M227 190 C234 217 223 239 230 263"/>
    </g>
    <g fill="url(#metal)" stroke="#222329" stroke-width="1.2">
      <circle cx="193" cy="185" r="6.2"/><circle cx="227" cy="185" r="6.2"/>
      <rect x="187" y="258" width="7" height="13" rx="2" transform="rotate(-8 190 264)"/>
      <rect x="226" y="258" width="7" height="13" rx="2" transform="rotate(8 229 264)"/>
    </g>
  `;
  await render("hoodie-lacing.png", lacing);
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
