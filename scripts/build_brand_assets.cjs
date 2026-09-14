#!/usr/bin/env node
/**
 * Rebuild the Harness identity from its native vector geometry.
 *
 * Requires Node.js >= 18 and sharp >= 0.33 (no browser or network requests).
 * Run: node scripts/build_brand_assets.cjs
 * If sharp is supplied by a shared runtime, set NODE_PATH to its node_modules.
 *
 * The 32-unit robot geometry is shared with web/companion.js. Raster exports
 * are derived from SVG at their target sizes; existing bitmaps are never edited.
 * PWA artwork fits the central maskable safe circle (radius 40% of the canvas).
 */
'use strict';

const fs = require('node:fs/promises');
const path = require('node:path');
const sharp = require('sharp');

const ROOT = path.resolve(__dirname, '..');
const BRAND_DIR = path.join(ROOT, 'web', 'brand');
const BODY = 'M11 8H21C25.418 8 29 11.582 29 16V21C29 25.418 25.418 29 21 29H11C6.582 29 3 25.418 3 21V16C3 11.582 6.582 8 11 8Z';
const FACE = `<path d="${BODY}"/><rect x="15" y="3" width="2" height="5" rx="1"/><circle cx="16" cy="3" r="2"/>`;
const EYES = '<rect x="9" y="15" width="4" height="6" rx="2"/><rect x="19" y="15" width="4" height="6" rx="2"/>';
const THEMES = {
  light: {face: '#1c2b30', eyes: '#ffffff', muted: '#4f646a'},
  dark: {face: '#edf3f3', eyes: '#11181b', muted: '#b2c3c6'},
};

/** @param {{face: string, eyes: string}} theme @returns {string} */
function robot(theme) {
  return `<g fill="${theme.face}">${FACE}</g><g fill="${theme.eyes}">${EYES}</g>`;
}

/** @param {string} content @param {string} [viewBox] @returns {string} */
function svg(content, viewBox = '0 0 32 32') {
  return `<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}" role="img" aria-labelledby="brand-title"><title id="brand-title">Local Agent Harness</title>${content}</svg>\n`;
}

/** @param {'light'|'dark'} themeName @returns {string} */
function wordmark(themeName) {
  const theme = THEMES[themeName];
  return svg(`<g transform="translate(0 0) scale(2)">${robot(theme)}</g><g fill="${theme.face}" font-family="Segoe UI, Inter, Helvetica, Arial, sans-serif"><text x="78" y="36" font-size="32" font-weight="600" letter-spacing="-1">Harness</text><text x="79" y="54" fill="${theme.muted}" font-size="10.5" font-weight="500" letter-spacing="1">Local Agent Harness</text></g>`, '0 0 230 64');
}

/** Write a deterministic PNG and verify its dimensions and corner alpha.
 * @param {string} filename
 * @param {string} source
 * @param {number} size
 * @param {boolean} transparent
 * @returns {Promise<void>}
 */
async function raster(filename, source, size, transparent) {
  const destination = path.join(ROOT, filename);
  await fs.mkdir(path.dirname(destination), {recursive: true});
  await sharp(Buffer.from(source)).resize(size, size).png({compressionLevel: 9}).toFile(destination);
  const metadata = await sharp(destination).metadata();
  if (metadata.width !== size || metadata.height !== size || metadata.format !== 'png') {
    throw new Error(`Invalid raster dimensions or format: ${filename}`);
  }
  const {data, info} = await sharp(destination).ensureAlpha().raw().toBuffer({resolveWithObject: true});
  const cornerAlpha = data[info.channels - 1];
  if (cornerAlpha !== (transparent ? 0 : 255)) {
    throw new Error(`Unexpected corner alpha for ${filename}: ${cornerAlpha}`);
  }
  process.stdout.write(`${filename}: ${size} × ${size}, ${transparent ? 'transparent' : 'opaque'}\n`);
}

/** @returns {Promise<void>} */
async function main() {
  // Fail visibly if the animated and static identities drift apart.
  const companion = await fs.readFile(path.join(ROOT, 'web', 'companion.js'), 'utf8');
  if (!companion.includes(BODY)) throw new Error('Companion geometry changed; update the brand source before exporting.');
  await fs.mkdir(BRAND_DIR, {recursive: true});
  const marks = {
    light: svg(robot(THEMES.light)),
    dark: svg(robot(THEMES.dark)),
  };
  const monochrome = svg(`<defs><mask id="eye-cutouts"><rect width="32" height="32" fill="white"/><g fill="black">${EYES}</g></mask></defs><g fill="currentColor" mask="url(#eye-cutouts)">${FACE}</g>`);
  const adaptive = svg(`<style>.face{fill:#1c2b30}.eyes{fill:#ffffff}@media(prefers-color-scheme:dark){.face{fill:#edf3f3}.eyes{fill:#11181b}}</style><g class="face">${FACE}</g><g class="eyes">${EYES}</g>`);
  const pwa = svg(`<rect width="32" height="32" fill="#11181b"/><g transform="translate(4 4) scale(.75)">${robot({face: '#68d5c4', eyes: '#11181b'})}</g>`);
  const vectors = {
    'mark-light.svg': marks.light,
    'mark-dark.svg': marks.dark,
    'mark-mono.svg': monochrome,
    'favicon.svg': adaptive,
    'wordmark-light.svg': wordmark('light'),
    'wordmark-dark.svg': wordmark('dark'),
    'app-icon.svg': pwa,
  };
  for (const [name, source] of Object.entries(vectors)) {
    await fs.writeFile(path.join(BRAND_DIR, name), source, 'utf8');
  }
  for (const size of [16, 32, 64]) {
    await raster(`web/favicon-${size}.png`, marks.light, size, true);
  }
  for (const themeName of ['light', 'dark']) {
    await raster(`web/logo-${themeName}-64.png`, marks[themeName], 64, true);
    // Keep any historical unversioned raster aliases only when already present.
    const legacy = `web/logo-${themeName}.png`;
    try {
      await fs.access(path.join(ROOT, legacy));
      await raster(legacy, marks[themeName], 64, true);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
  for (const [filename, size] of [['icon-192.png', 192], ['icon-512.png', 512], ['apple-touch-icon.png', 180]]) {
    await raster(`web_mobile/icons/${filename}`, pwa, size, false);
  }
  process.stdout.write(`Exported ${Object.keys(vectors).length} native SVG assets.\n`);
}

main().catch((error) => {
  process.stderr.write(`Brand build failed: ${error.message}\n`);
  process.exitCode = 1;
});
