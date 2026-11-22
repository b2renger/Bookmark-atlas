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
import crypto from 'node:crypto';
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

// The release, the file and its SHA-256 are pinned in ../pins.json: every build
// of the app carries the same Python, and a file that changed is refused.
const PINS = JSON.parse(fs.readFileSync(path.join(desktop, 'pins.json'), 'utf8')).python;

async function findPythonAsset(key) {
    const pin = PINS[key];
    if (!pin) die(`no pinned Python for ${key} in pins.json`);
    const headers = { 'user-agent': 'bookmark-atlas-prepare-backend', accept: 'application/vnd.github+json' };
    if (process.env.GH_TOKEN) headers.authorization = `Bearer ${process.env.GH_TOKEN}`;
    const res = await fetch(`https://api.github.com/repos/astral-sh/python-build-standalone/releases/tags/${PINS.tag}`, { headers });
    if (!res.ok) die(`GitHub API answered ${res.status}. Set GH_TOKEN if rate-limited.`);
    const release = await res.json();
    const asset = (release.assets || []).find((a) => a.name === pin.name);
    if (!asset) die(`release ${PINS.tag} has no file named ${pin.name}`);
    return { ...asset, sha256: pin.sha256 };
}

async function download(url, dest, sha256) {
    const res = await fetch(url, { headers: { 'user-agent': 'bookmark-atlas-prepare-backend' } });
    if (!res.ok) die(`download answered ${res.status} for ${url}`);
    const buffer = Buffer.from(await res.arrayBuffer());
    const digest = crypto.createHash('sha256').update(buffer).digest('hex');
    if (digest !== sha256) die(`${path.basename(url)} is not the pinned file: sha256 ${digest}, expected ${sha256}`);
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
    const asset = await findPythonAsset(key);
    const tarball = path.join(backend, 'python.tar.gz');
    await download(asset.browser_download_url, tarball, asset.sha256);
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

    // 3. Packages. tzdata: Windows has no time zone database Python can read,
    //    and Notion dates may name a zone. numpy: vectors and clustering.
    //    keyring: tokens and keys go to the system keychain, not to a file.
    const pythonExe = process.platform === 'win32'
        ? path.join(pythonDir, 'python.exe')
        : path.join(pythonDir, 'bin', 'python3');
    execFileSync(pythonExe, ['-I', '-m', 'pip', 'install', '--no-warn-script-location', '--disable-pip-version-check', '-c', path.join(desktop, 'constraints.txt'), 'tzdata', 'numpy', 'keyring'], { stdio: 'inherit' });
    execFileSync(pythonExe, ['-I', '-c', 'from zoneinfo import ZoneInfo; ZoneInfo("Europe/Paris"); import numpy; print("numpy", numpy.__version__); import keyring; print("keyring", type(keyring.get_keyring()).__module__)'], { stdio: 'inherit' });

    // 4. The atlas package.
    const appDir = path.join(backend, 'app');
    removeIfPresent(appDir);
    fs.cpSync(source, path.join(appDir, 'atlas'), {
        recursive: true,
        filter: (src) => !src.includes('__pycache__'),
    });

    // 5. Prove the result runs before anything gets packaged.
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
