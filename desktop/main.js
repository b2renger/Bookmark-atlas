// Bookmark Atlas: Electron main process.
//
// The app is a thin shell. It starts the Python backend (bundled next to the
// app), waits for it to announce the address of its local web interface, and
// shows that page in a window. It also keeps itself up to date from GitHub
// Releases with electron-updater, the same way ComfyQ and LlmOnLan do.
//
// Flags and environment, used by CI and for troubleshooting:
//   --smoke-test            start, load the page, write a result file, exit
//   ATLAS_SMOKE_OUT=<file>  where the smoke test writes its JSON result
//   ATLAS_USER_DATA=<dir>   use another data folder than the default

const { app, BrowserWindow, Menu, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');

const SMOKE = process.argv.includes('--smoke-test');
const RELEASES_URL = 'https://github.com/b2renger/Bookmark-atlas/releases/latest';
const BACKEND_START_TIMEOUT_MS = 45_000;

if (process.env.ATLAS_USER_DATA) app.setPath('userData', process.env.ATLAS_USER_DATA);
if (SMOKE) app.disableHardwareAcceleration();

let win = null;
let backend = null;
let backendUrl = null;
let quitting = false;
let autoUpdater = null;
let updaterWired = false;
let manualCheck = false;

const userData = () => app.getPath('userData');
const logPath = () => path.join(userData(), 'backend.log');

// ---- backend ---------------------------------------------------------------

function backendCommand() {
    const packagedBase = path.join(process.resourcesPath, 'backend');
    const devBase = path.join(__dirname, 'backend');
    const base = app.isPackaged ? packagedBase : devBase;
    const bundled = process.platform === 'win32'
        ? path.join(base, 'python', 'python.exe')
        : path.join(base, 'python', 'bin', 'python3');
    const launcher = path.join(base, 'run_atlas.py');
    const tail = ['--db', path.join(userData(), 'atlas.sqlite'), 'ui', '--port', '0', '--announce'];

    if (fs.existsSync(bundled) && fs.existsSync(path.join(base, 'app', 'atlas'))) {
        // Isolated mode: the user's own Python settings cannot interfere.
        return { command: bundled, args: ['-I', '-u', launcher, ...tail], env: {} };
    }
    if (app.isPackaged) throw new Error(`The bundled backend is missing from ${base}.`);
    // Development without a prepared backend: system Python on the source tree.
    return {
        command: process.platform === 'win32' ? 'python' : 'python3',
        args: ['-u', '-m', 'atlas', ...tail],
        env: { PYTHONPATH: path.resolve(__dirname, '..', 'src') },
    };
}

function startBackend() {
    return new Promise((resolve, reject) => {
        fs.mkdirSync(userData(), { recursive: true });
        const log = fs.createWriteStream(logPath(), { flags: 'w' });
        let spec;
        try { spec = backendCommand(); } catch (error) { reject(error); return; }
        log.write(`[shell] ${new Date().toISOString()} starting ${spec.command} ${spec.args.join(' ')}\n`);

        backend = spawn(spec.command, spec.args, {
            env: {
                ...process.env,
                ...spec.env,
                // Settings and the Notion token live beside the database,
                // in this app's own data folder.
                ATLAS_CONFIG_DIR: userData(),
                ATLAS_APP_VERSION: app.getVersion(),
            },
            windowsHide: true,
            stdio: ['ignore', 'pipe', 'pipe'],
        });

        let settled = false;
        let pending = '';
        const timer = setTimeout(() => {
            if (settled) return;
            settled = true;
            reject(new Error('The backend did not start in time.'));
        }, BACKEND_START_TIMEOUT_MS);

        backend.stdout.setEncoding('utf8');
        backend.stdout.on('data', (chunk) => {
            log.write(chunk);
            if (settled) return;
            pending += chunk;
            let newline;
            while ((newline = pending.indexOf('\n')) >= 0) {
                const line = pending.slice(0, newline).trim();
                pending = pending.slice(newline + 1);
                if (!line.startsWith('ATLAS_READY ')) continue;
                try {
                    const { url } = JSON.parse(line.slice('ATLAS_READY '.length));
                    const parsed = new URL(url);
                    if (parsed.hostname !== '127.0.0.1') throw new Error(`unexpected address ${url}`);
                    settled = true;
                    clearTimeout(timer);
                    resolve(parsed.href);
                } catch (error) {
                    settled = true;
                    clearTimeout(timer);
                    reject(error);
                }
                return;
            }
        });
        backend.stderr.on('data', (chunk) => log.write(chunk));
        backend.on('error', (error) => {
            log.write(`[shell] could not start: ${error.message}\n`);
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            reject(error);
        });
        backend.on('exit', (code, signal) => {
            log.write(`[shell] backend exited (code ${code}, signal ${signal})\n`);
            backend = null;
            if (!settled) {
                settled = true;
                clearTimeout(timer);
                reject(new Error(`The backend stopped while starting (code ${code}).`));
            } else if (!quitting) {
                fail('Bookmark Atlas stopped unexpectedly', `The backend exited (code ${code}).`);
            }
        });
    });
}

function stopBackend() {
    if (backend && !backend.killed) {
        try { backend.kill(); } catch { /* already gone */ }
    }
}

// ---- window ----------------------------------------------------------------

function isExternalWebLink(url) {
    try {
        const parsed = new URL(url);
        return (parsed.protocol === 'http:' || parsed.protocol === 'https:')
            && parsed.origin !== new URL(backendUrl).origin;
    } catch { return false; }
}

function createWindow() {
    win = new BrowserWindow({
        width: 1240,
        height: 880,
        minWidth: 420,
        minHeight: 480,
        show: !SMOKE,
        title: 'Bookmark Atlas',
        backgroundColor: '#f6f5f1',
        webPreferences: {
            // The page is plain web content: no Node, no preload, sandboxed.
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
        },
    });

    // Bookmarks open in the user's browser, never inside the app window.
    win.webContents.setWindowOpenHandler(({ url }) => {
        if (isExternalWebLink(url)) shell.openExternal(url);
        return { action: 'deny' };
    });
    win.webContents.on('will-navigate', (event, url) => {
        if (new URL(url).origin === new URL(backendUrl).origin) return;
        event.preventDefault();
        if (isExternalWebLink(url)) shell.openExternal(url);
    });
    win.on('closed', () => { win = null; });
    win.loadURL(backendUrl);
    return win;
}

function buildMenu() {
    const template = [
        {
            label: 'Atlas',
            submenu: [
                { label: `Version ${app.getVersion()}`, enabled: false },
                { label: 'Check for updates…', click: () => checkForUpdates(true) },
                { type: 'separator' },
                { label: 'Open data folder', click: () => shell.openPath(userData()) },
                { type: 'separator' },
                { role: 'quit' },
            ],
        },
        { role: 'editMenu' },
        {
            label: 'View',
            submenu: [
                { role: 'reload' },
                { role: 'resetZoom' },
                { role: 'zoomIn' },
                { role: 'zoomOut' },
                { type: 'separator' },
                { role: 'togglefullscreen' },
                { role: 'toggleDevTools' },
            ],
        },
    ];
    Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function fail(title, detail) {
    if (SMOKE) { finishSmoke({ ok: false, error: `${title}: ${detail}` }); return; }
    dialog.showErrorBox(title, `${detail}\n\nDetails are in:\n${logPath()}`);
    quitting = true;
    app.quit();
}

// ---- self-update -----------------------------------------------------------
// Unsigned builds, updated from GitHub Releases. Downloads in the background,
// installs when the app quits, and offers to restart once ready.

function getUpdater() {
    if (autoUpdater) return autoUpdater;
    try { ({ autoUpdater } = require('electron-updater')); } catch (error) {
        console.warn('[updater] unavailable:', error.message);
        return null;
    }
    return autoUpdater;
}

function wireUpdater(updater) {
    if (updaterWired) return;
    updaterWired = true;
    updater.autoDownload = true;
    updater.autoInstallOnAppQuit = true;
    updater.on('error', (error) => {
        console.warn('[updater] error:', error?.message || error);
        if (manualCheck) {
            manualCheck = false;
            dialog.showMessageBox(win, {
                type: 'warning',
                message: 'Could not check for updates',
                detail: `${error?.message || error}\n\nThe latest version is always at:\n${RELEASES_URL}`,
            });
        }
    });
    updater.on('update-not-available', () => {
        if (!manualCheck) return;
        manualCheck = false;
        dialog.showMessageBox(win, { type: 'info', message: `Bookmark Atlas ${app.getVersion()} is the latest version.` });
    });
    updater.on('update-available', (info) => {
        if (!manualCheck) return;
        manualCheck = false;
        dialog.showMessageBox(win, {
            type: 'info',
            message: `Version ${info.version} is available`,
            detail: 'It is downloading in the background. You will be asked to restart when it is ready.',
        });
    });
    updater.on('update-downloaded', async (info) => {
        const { response } = await dialog.showMessageBox(win, {
            type: 'info',
            buttons: ['Restart now', 'Later'],
            defaultId: 0,
            cancelId: 1,
            message: `Bookmark Atlas ${info.version} is ready`,
            detail: 'Restart to use it now, or it will be installed the next time you quit.',
        });
        if (response === 0) {
            quitting = true;
            stopBackend();
            updater.quitAndInstall();
        }
    });
}

function checkForUpdates(manual) {
    if (!app.isPackaged) {
        if (manual) dialog.showMessageBox(win, { type: 'info', message: 'Updates only apply to an installed build.' });
        return;
    }
    const updater = getUpdater();
    if (!updater) return;
    wireUpdater(updater);
    manualCheck = manual;
    updater.checkForUpdates().catch((error) => console.warn('[updater] check failed:', error?.message || error));
}

// ---- smoke test ------------------------------------------------------------
// Proves the packaged app works end to end: backend starts, page loads, the
// page reaches the API. CI runs this on the built app before publishing.

let smokeDone = false;
function finishSmoke(result) {
    if (smokeDone) return;
    smokeDone = true;
    const output = { ...result, version: app.getVersion(), packaged: app.isPackaged, url: backendUrl };
    try {
        if (process.env.ATLAS_SMOKE_OUT) fs.writeFileSync(process.env.ATLAS_SMOKE_OUT, JSON.stringify(output, null, 2));
    } catch (error) { console.error('[smoke] could not write result:', error.message); }
    console.log('[smoke]', JSON.stringify(output));
    quitting = true;
    stopBackend();
    app.exit(result.ok ? 0 : 1);
}

async function runSmoke() {
    const deadline = Date.now() + 30_000;
    let last = '';
    while (Date.now() < deadline) {
        try {
            last = await win.webContents.executeJavaScript(
                '(document.getElementById("summary") || {}).textContent || ""');
            // The header is filled only after the page has called the API.
            if (last.includes('bookmarks') && last.includes(`v${app.getVersion()}`)) {
                finishSmoke({ ok: true, summary: last, title: win.getTitle() });
                return;
            }
        } catch (error) { last = `error: ${error.message}`; }
        await new Promise((resolve) => setTimeout(resolve, 500));
    }
    finishSmoke({ ok: false, error: 'The page did not show its summary in time.', summary: last });
}

// ---- lifecycle -------------------------------------------------------------

if (!SMOKE && !app.requestSingleInstanceLock()) {
    app.quit();
} else {
    app.on('second-instance', () => {
        if (!win) return;
        if (win.isMinimized()) win.restore();
        win.focus();
    });

    app.whenReady().then(async () => {
        if (SMOKE) setTimeout(() => finishSmoke({ ok: false, error: 'Smoke test timed out.' }), 90_000);
        try {
            backendUrl = await startBackend();
        } catch (error) {
            fail('Bookmark Atlas could not start', error.message);
            return;
        }
        buildMenu();
        createWindow();
        if (SMOKE) {
            win.webContents.once('did-finish-load', runSmoke);
            win.webContents.once('did-fail-load', (_event, code, description) =>
                finishSmoke({ ok: false, error: `Page failed to load: ${description} (${code})` }));
        } else {
            checkForUpdates(false);
        }
    });

    app.on('before-quit', () => { quitting = true; stopBackend(); });
    app.on('window-all-closed', () => { quitting = true; app.quit(); });
    process.on('exit', stopBackend);
}
