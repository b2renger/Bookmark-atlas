// Prepare desktop/engine for packaging: llama.cpp's llama-server and the
// libraries beside it, from ggml-org's own release for this platform. It is the
// engine of the language model that runs inside the app; the model files are
// not shipped, they are downloaded by the app on request.
//
// Run by CI before electron-builder. Needs `tar` and network access.
//
// The build is pinned: a known one, the same LlmOnLan's farm uses, new enough
// for Gemma 4 and EmbeddingGemma 2 (which need b11454 or later).

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const BUILD = 'b11836';
// The CPU builds: they run on every machine. (Apple's includes Metal.)
const ASSETS = {
    'win32-x64': /-bin-win-cpu-x64\.zip$/,
    'linux-x64': /-bin-ubuntu-x64\.tar\.gz$/,
    'darwin-arm64': /-bin-macos-arm64\.tar\.gz$/,
};

const here = path.dirname(fileURLToPath(import.meta.url));
const desktop = path.resolve(here, '..');
const engine = path.join(desktop, 'engine');
const log = (m) => console.log(`[prepare-engine] ${m}`);
const die = (m) => { console.error(`[prepare-engine] ERROR: ${m}`); process.exit(1); };

function find(dir, name) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) { const found = find(full, name); if (found) return found; }
        else if (entry.name === name) return full;
    }
    return null;
}

const key = `${process.platform}-${process.arch}`;
const wanted = ASSETS[key];
if (!wanted) die(`no llama.cpp build is listed for ${key}`);

const headers = { 'user-agent': 'bookmark-atlas-prepare-engine', accept: 'application/vnd.github+json' };
if (process.env.GH_TOKEN) headers.authorization = `Bearer ${process.env.GH_TOKEN}`;
const res = await fetch(`https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/${BUILD}`, { headers });
if (!res.ok) die(`GitHub API answered ${res.status} for llama.cpp ${BUILD}. Set GH_TOKEN if rate-limited.`);
const release = await res.json();
const asset = release.assets.find((a) => wanted.test(a.name));
if (!asset) die(`llama.cpp ${BUILD} has no file matching ${wanted}. It has:\n  ${release.assets.map((a) => a.name).join('\n  ')}`);

fs.rmSync(engine, { recursive: true, force: true });
const scratch = path.join(desktop, 'engine-download');
fs.rmSync(scratch, { recursive: true, force: true });
fs.mkdirSync(scratch, { recursive: true });
const archive = path.join(scratch, asset.name);
log(`downloading ${asset.name} (${(asset.size / 1e6).toFixed(0)} MB)`);
const download = await fetch(asset.browser_download_url, { headers: { 'user-agent': headers['user-agent'] } });
if (!download.ok) die(`download answered ${download.status}`);
fs.writeFileSync(archive, Buffer.from(await download.arrayBuffer()));
// bsdtar (Windows, macOS) and GNU tar read .tar.gz; bsdtar also reads .zip.
if (asset.name.endsWith('.zip') && process.platform !== 'win32') execFileSync('unzip', ['-q', archive, '-d', scratch], { stdio: 'inherit' });
else execFileSync('tar', ['-xf', archive, '-C', scratch], { stdio: 'inherit' });
fs.rmSync(archive);

const name = process.platform === 'win32' ? 'llama-server.exe' : 'llama-server';
const server = find(scratch, name);
if (!server) die(`${asset.name} holds no ${name}`);
// Everything beside the server (its libraries) comes along, flat; the other
// programs of the archive (llama-cli, llama-bench, …) are left out.
fs.mkdirSync(engine, { recursive: true });
let kept = 0;
for (const entry of fs.readdirSync(path.dirname(server), { withFileTypes: true })) {
    if (entry.isDirectory()) continue;
    const isProgram = /^(llama|rpc|test|export|convert)-/i.test(entry.name) && !/\.(dll|so|dylib)(\.|$)/i.test(entry.name);
    if (isProgram && entry.name !== name) continue;
    // cp, not copyFile: the Unix archives hold symbolic links between library versions.
    fs.cpSync(path.join(path.dirname(server), entry.name), path.join(engine, entry.name), { verbatimSymlinks: true });
    kept += 1;
}
if (process.platform !== 'win32') fs.chmodSync(path.join(engine, name), 0o755);
fs.rmSync(scratch, { recursive: true, force: true });
fs.writeFileSync(path.join(engine, 'ENGINE_INFO.json'), JSON.stringify({ build: BUILD, asset: asset.name, platform: key }, null, 2));
const size = fs.readdirSync(engine).reduce((sum, file) => sum + fs.statSync(path.join(engine, file)).size, 0);
log(`engine ready: ${kept} files, ${(size / 1e6).toFixed(0)} MB, from ${asset.name}`);
