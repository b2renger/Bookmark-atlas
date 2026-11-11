// Prepare desktop/backend for packaging:
//
//   backend/python/   a relocatable standalone CPython (astral-sh/python-build-standalone)
//   backend/app/atlas the atlas package, copied from ../src/atlas
//   backend/run_atlas.py   the entry point (already in the repository)
//
// Run by CI before electron-builder. Needs `tar` (built into Windows 10+,
// macOS and Linux) and network access. Same approach as LlmOnLan's
// sidecar/build-sidecar.mjs.

import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const PY_PREFIX = '3.12';
const TRIPLES = {
    'win32-x64': 'x86_64-pc-windows-msvc-install_only',
    'darwin-arm64': 'aarch64-apple-darwin-install_only',
    'darwin-x64': 'x86_64-apple-darwin-install_only',
    'linux-x64': 'x86_64-unknown-linux-gnu-install_only',
    'linux-arm64': 'aarch64-unknown-linux-gnu-install_only',
};

const here = path.dirname(fileURLToPath(import.meta.url));
const desktop = path.resolve(here, '..');
const backend = path.join(desktop, 'backend');
const source = path.resolve(desktop, '..', 'src', 'atlas');

const log = (m) => console.log(`[prepare-backend] ${m}`);
const die = (m) => { console.error(`[prepare-backend] ERROR: ${m}`); process.exit(1); };

async function findPythonAsset(triple) {
    // The latest release, not a fixed tag: tags rotate often.
    const headers = { 'user-agent': 'bookmark-atlas-prepare-backend', accept: 'application/vnd.github+json' };
    if (process.env.GH_TOKEN) headers.authorization = `Bearer ${process.env.GH_TOKEN}`;
    const res = await fetch('https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest', { headers });
    if (!res.ok) die(`GitHub API answered ${res.status}. Set GH_TOKEN if rate-limited.`);
    const release = await res.json();
    const pattern = new RegExp(`^cpython-${PY_PREFIX.replace('.', '\\.')}\\.\\d+\\+.*-${triple}\\.tar\\.gz$`);
    const asset = (release.assets || []).find((a) => pattern.test(a.name));
    if (!asset) die(`no Python ${PY_PREFIX} build for ${triple} in release ${release.tag_name}`);
    return asset;
}

async function download(url, dest) {
    const res = await fetch(url, { headers: { 'user-agent': 'bookmark-atlas-prepare-backend' } });
    if (!res.ok) die(`download answered ${res.status} for ${url}`);
    const buffer = Buffer.from(await res.arrayBuffer());
    fs.writeFileSync(dest, buffer);
    log(`downloaded ${path.basename(url)} (${(buffer.length / 1e6).toFixed(0)} MB)`);
}

function removeIfPresent(target) {
    fs.rmSync(target, { recursive: true, force: true });
}

function folderSizeMb(dir) {
    let total = 0;
    for (const entry of fs.readdirSync(dir, { withFileTypes: true, recursive: true })) {
        if (entry.isFile()) total += fs.statSync(path.join(entry.parentPath ?? entry.path, entry.name)).size;
    }
    return (total / 1e6).toFixed(0);
}

async function main() {
    const key = `${process.platform}-${process.arch}`;
    const triple = TRIPLES[key];
    if (!triple) die(`unsupported platform ${key}`);
    if (!fs.existsSync(path.join(source, '__init__.py'))) die(`atlas package not found at ${source}`);

    // 1. Standalone Python.
    const pythonDir = path.join(backend, 'python');
    removeIfPresent(pythonDir);
    const asset = await findPythonAsset(triple);
    const tarball = path.join(backend, 'python.tar.gz');
    await download(asset.browser_download_url, tarball);
    // Relative paths and cwd, so tar never sees a Windows drive colon (which
    // GNU tar would read as a remote host).
    execFileSync('tar', ['-xzf', 'python.tar.gz'], { cwd: backend, stdio: 'inherit' });
    fs.rmSync(tarball);
    if (!fs.existsSync(pythonDir)) die('the archive did not contain a python/ folder');

    // 2. Drop what the app never uses: the test suite, Tk, IDLE, C headers.
    const lib = process.platform === 'win32'
        ? path.join(pythonDir, 'Lib')
        : path.join(pythonDir, 'lib', `python${PY_PREFIX}`);
    for (const name of ['test', 'idlelib', 'tkinter', 'turtledemo', 'lib2to3']) removeIfPresent(path.join(lib, name));
    for (const name of ['tcl', 'include', 'libs', 'share']) removeIfPresent(path.join(pythonDir, name));

    // 3. The atlas package.
    const appDir = path.join(backend, 'app');
    removeIfPresent(appDir);
    fs.cpSync(source, path.join(appDir, 'atlas'), {
        recursive: true,
        filter: (src) => !src.includes('__pycache__'),
    });

    // 4. Prove the result runs before anything gets packaged.
    const python = process.platform === 'win32'
        ? path.join(pythonDir, 'python.exe')
        : path.join(pythonDir, 'bin', 'python3');
    const version = execFileSync(python, ['-I', '-u', path.join(backend, 'run_atlas.py'), '--version'], { encoding: 'utf8' }).trim();
    const runtime = execFileSync(python, ['-I', '-c', 'import sys, sqlite3, ssl; print(sys.version.split()[0], "sqlite", sqlite3.sqlite_version, ssl.OPENSSL_VERSION)'], { encoding: 'utf8' }).trim();

    fs.writeFileSync(path.join(backend, 'BUILD_INFO.json'), JSON.stringify({
        python: asset.name,
        atlas: version,
        platform: key,
    }, null, 2) + '\n');

    log(`${version} on Python ${runtime}`);
    log(`backend ready: ${folderSizeMb(backend)} MB in ${backend}`);
}

main().catch((error) => die(error.stack || String(error)));
