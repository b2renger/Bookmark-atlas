// Print the names, sizes and SHA-256 of everything the build and the app
// download, so they can be pinned (desktop/pins.json, src/atlas/engine.py).
// Run by .github/workflows/pins.yml, by hand, when a pin is to be set or moved.
// It trusts what it receives over HTTPS at that moment: a pin proves the file
// did not change afterwards, not that it was right on that day.

import crypto from 'node:crypto';
import fs from 'node:fs';

const headers = { 'user-agent': 'bookmark-atlas-pins', accept: 'application/vnd.github+json' };
if (process.env.GH_TOKEN) headers.authorization = `Bearer ${process.env.GH_TOKEN}`;

async function hashOf(url) {
    const res = await fetch(url, { headers: { 'user-agent': headers['user-agent'] } });
    if (!res.ok) throw new Error(`${res.status} for ${url}`);
    const hash = crypto.createHash('sha256');
    let bytes = 0;
    for await (const chunk of res.body) { hash.update(chunk); bytes += chunk.length; }
    return { sha256: hash.digest('hex'), bytes };
}

const out = { made: new Date().toISOString(), llama: {}, python: {}, models: {} };

// llama.cpp, the build named in prepare-engine.mjs.
const BUILD = /const BUILD = '([^']+)'/.exec(fs.readFileSync(new URL('./prepare-engine.mjs', import.meta.url), 'utf8'))[1];
const LLAMA = { 'win32-x64': /-bin-win-cpu-x64\.zip$/, 'linux-x64': /-bin-ubuntu-x64\.tar\.gz$/, 'darwin-arm64': /-bin-macos-arm64\.tar\.gz$/ };
let release = await (await fetch(`https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/${BUILD}`, { headers })).json();
out.llama.build = BUILD;
for (const [platform, pattern] of Object.entries(LLAMA)) {
    const asset = release.assets.find((a) => pattern.test(a.name));
    out.llama[platform] = { name: asset.name, github_digest: asset.digest || null, ...(await hashOf(asset.browser_download_url)) };
}

// Standalone Python: the latest release today becomes the pinned one.
const TRIPLES = { 'win32-x64': 'x86_64-pc-windows-msvc-install_only', 'linux-x64': 'x86_64-unknown-linux-gnu-install_only', 'darwin-arm64': 'aarch64-apple-darwin-install_only' };
release = await (await fetch('https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest', { headers })).json();
out.python.tag = release.tag_name;
for (const [platform, triple] of Object.entries(TRIPLES)) {
    const pattern = new RegExp(`^cpython-3\\.12\\.\\d+\\+.*-${triple}\\.tar\\.gz$`);
    const asset = release.assets.find((a) => pattern.test(a.name));
    out.python[platform] = { name: asset.name, github_digest: asset.digest || null, ...(await hashOf(asset.browser_download_url)) };
}

// The three writing models: which commit "main" is today, and the file's hash.
const engine = fs.readFileSync(new URL('../../src/atlas/engine.py', import.meta.url), 'utf8');
for (const url of engine.match(/https:\/\/huggingface\.co\/[^"\s]+\/resolve\/main\/[^"\s]+\.gguf/g) || []) {
    const head = await fetch(url, { method: 'HEAD', redirect: 'manual', headers: { 'user-agent': headers['user-agent'] } });
    out.models[url] = {
        commit: head.headers.get('x-repo-commit'),
        linked_etag: head.headers.get('x-linked-etag'),
        linked_size: head.headers.get('x-linked-size'),
        ...(await hashOf(url)),
    };
    console.error(`hashed ${url}`);
}

console.log('PINS-BEGIN');
console.log(JSON.stringify(out, null, 2));
console.log('PINS-END');
