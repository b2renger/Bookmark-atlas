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
//   ATLAS_USER_DATA=<dir>   use another settings folder for the app itself
//   ATLAS_DATA_DIR=<dir>    use this data folder, whatever was chosen
//
// The user's data (database, scripts, backups, token) is in one folder, the
// "data folder", which the user can place anywhere: see datafolder.js.

const { app, BrowserWindow, Menu, dialog, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const datafolder = require('./datafolder.js');

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

// Set once at start by settleDataFolder(); changing it restarts the app.
let dataFolder = null;
let dataFolderSource = 'default';

/** Decide which data folder to use. Returns false if the app should quit. */
function settleDataFolder() {
    const found = datafolder.resolve({
        override: process.env.ATLAS_DATA_DIR,
        settingsDir: userData(),
        defaultDir: userData(),
    });
    dataFolder = found.folder;
    dataFolderSource = found.source;

    if (found.source === 'chosen' && !datafolder.describe(dataFolder).exists) {
        if (SMOKE) return false;
        const choice = dialog.showMessageBoxSync({
            type: 'warning',
            buttons: ['Choose another folder…', 'Use the default folder', 'Quit'],
            defaultId: 0,
            cancelId: 2,
            message: 'Your data folder is not available',
            detail: `${dataFolder}\n\nIf it is on an external or network drive, connect it and start Bookmark Atlas again.`,
        });
        if (choice === 2) return false;
        if (choice === 1) {
            datafolder.writePointer(userData(), null);
            dataFolder = userData();
            dataFolderSource = 'default';
        } else {
            const picked = dialog.showOpenDialogSync({
                title: 'Choose the data folder',
                properties: ['openDirectory', 'createDirectory'],
            });
            if (!picked || !picked[0]) return false;
            datafolder.writePointer(userData(), picked[0]);
            dataFolder = picked[0];
            dataFolderSource = 'chosen';
        }
    }
    fs.mkdirSync(dataFolder, { recursive: true });
    return true;
}

// ---- backend ---------------------------------------------------------------

function backendCommand() {
    const packagedBase = path.join(process.resourcesPath, 'backend');
    const devBase = path.join(__dirname, 'backend');
    const base = app.isPackaged ? packagedBase : devBase;
    const bundled = process.platform === 'win32'
        ? path.join(base, 'python', 'python.exe')
        : path.join(base, 'python', 'bin', 'python3');
    const launcher = path.join(base, 'run_atlas.py');
    const tail = ['--db', path.join(dataFolder, datafolder.DATABASE), 'ui', '--port', '0', '--announce'];

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
                // The Notion token lives beside the database, so the data
                // folder is complete on its own.
                ATLAS_CONFIG_DIR: dataFolder,
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

/** Stop the backend and wait until its process is gone, so its files are closed. */
function stopBackendAndWait(timeoutMs = 8000) {
    return new Promise((resolve) => {
        const child = backend;
        if (!child) { resolve(); return; }
        const timer = setTimeout(resolve, timeoutMs);
        child.once('exit', () => { clearTimeout(timer); resolve(); });
        stopBackend();
    });
}

// ---- snapshots -------------------------------------------------------------
// A snapshot is a collection file someone exported. Opening one copies it into
// a folder of its own and restarts on it; the user's collection is untouched.

async function restartWith(action, failureTitle) {
    quitting = true;                      // the backend stopping is expected
    await stopBackendAndWait();
    try {
        action();
    } catch (error) {
        dialog.showErrorBox(failureTitle, `${error.message}\n\nBookmark Atlas will restart where it was.`);
    }
    app.relaunch();
    app.exit(0);
}

async function openSnapshot() {
    if (dataFolderSource === 'override') {
        dialog.showMessageBox(win, { type: 'info', message: 'The data folder is set by ATLAS_DATA_DIR; snapshots cannot be opened here.' });
        return;
    }
    const pick = await dialog.showOpenDialog(win, {
        title: 'Open a snapshot',
        buttonLabel: 'Open',
        properties: ['openFile'],
        filters: [{ name: 'Bookmark Atlas snapshot', extensions: ['sqlite'] }, { name: 'All files', extensions: ['*'] }],
    });
    if (pick.canceled || !pick.filePaths[0]) return;
    const file = pick.filePaths[0];
    if (!datafolder.looksLikeDatabase(file)) {
        dialog.showMessageBox(win, { type: 'warning', message: 'This file is not a Bookmark Atlas snapshot.', detail: file });
        return;
    }
    const { response } = await dialog.showMessageBox(win, {
        type: 'question',
        buttons: ['Open the snapshot', 'Cancel'],
        defaultId: 0,
        cancelId: 1,
        message: 'Open this snapshot?',
        detail: `${file}\n\nBookmark Atlas will restart and show the snapshot. Your own collection is not touched: come back to it with Atlas > Return to my collection.`,
    });
    if (response !== 0) return;
    await restartWith(
        () => datafolder.openSnapshot(userData(), file, path.join(userData(), 'Snapshots'), { folder: dataFolder, source: dataFolderSource }),
        'The snapshot was not opened',
    );
}

async function returnToCollection() {
    if (datafolder.readHome(userData()) === null) return;
    await restartWith(() => datafolder.returnHome(userData()), 'Could not return to your collection');
}

// ---- data folder -----------------------------------------------------------

async function chooseDataFolder() {
    if (dataFolderSource === 'override') {
        dialog.showMessageBox(win, { type: 'info', message: 'The data folder is set by ATLAS_DATA_DIR and cannot be changed here.' });
        return;
    }
    const pick = await dialog.showOpenDialog(win, {
        title: 'Choose the data folder',
        defaultPath: dataFolder,
        buttonLabel: 'Choose',
        properties: ['openDirectory', 'createDirectory'],
    });
    if (pick.canceled || !pick.filePaths[0]) return;
    const target = pick.filePaths[0];
    if (datafolder.samePlace(target, dataFolder)) {
        dialog.showMessageBox(win, { type: 'info', message: 'That is already your data folder.' });
        return;
    }
    const found = datafolder.describe(target);
    if (!found.writable) {
        dialog.showMessageBox(win, { type: 'warning', message: 'Bookmark Atlas cannot write in that folder.', detail: target });
        return;
    }

    let copy = false;
    if (found.hasCollection) {
        const { response } = await dialog.showMessageBox(win, {
            type: 'question',
            buttons: ['Use that collection', 'Cancel'],
            defaultId: 0,
            cancelId: 1,
            message: 'That folder already holds a collection',
            detail: `Bookmark Atlas will restart and open the collection in:\n${target}\n\nYour current data is not touched. It stays in:\n${dataFolder}`,
        });
        if (response !== 0) return;
    } else {
        const { response } = await dialog.showMessageBox(win, {
            type: 'question',
            buttons: ['Copy my data there and use it', 'Start an empty collection there', 'Cancel'],
            defaultId: 0,
            cancelId: 2,
            message: 'Use this folder for your data?',
            detail: `${target}\n\nCopying keeps the original in place as a safety copy:\n${dataFolder}\nYou can delete it yourself once you have checked the new folder.\n\nBookmark Atlas will restart.`,
        });
        if (response === 2) return;
        copy = response === 0;
    }

    quitting = true;                      // the backend stopping is expected
    await stopBackendAndWait();
    try {
        if (copy) datafolder.copyData(dataFolder, target);
        datafolder.writePointer(userData(), target);
    } catch (error) {
        // Nothing was switched: the pointer is written last, and a failed
        // copy never touches the original.
        dialog.showErrorBox('The data folder was not changed', `${error.message}\n\nBookmark Atlas will restart with the current folder.`);
    }
    app.relaunch();
    app.exit(0);
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
                { label: 'Open data folder', click: () => shell.openPath(dataFolder) },
                { label: 'Choose data folder…', click: () => chooseDataFolder() },
                { type: 'separator' },
                { label: 'Open snapshot…', click: () => openSnapshot() },
                { label: 'Return to my collection', enabled: datafolder.readHome(userData()) !== null, click: () => returnToCollection() },
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
    // macOS only installs updates into an app signed by a paid developer
    // certificate, which this project does not have: point to the download.
    // On Linux only the AppImage can replace itself.
    if (process.platform === 'darwin' || (process.platform === 'linux' && !process.env.APPIMAGE)) {
        if (manual) {
            dialog.showMessageBox(win, {
                type: 'info',
                buttons: ['Open the download page', 'Close'],
                defaultId: 0,
                cancelId: 1,
                message: `You have Bookmark Atlas ${app.getVersion()}`,
                detail: 'This build cannot update itself. The latest version is on the download page; install it over this one, your bookmarks are kept.',
            }).then(({ response }) => { if (response === 0) shell.openExternal(RELEASES_URL); });
        }
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
    const output = { ...result, version: app.getVersion(), packaged: app.isPackaged, url: backendUrl, dataFolder };
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
                // What the backend itself says its folder is, read from the page.
                const shown = await win.webContents.executeJavaScript(
                    '(document.getElementById("data-folder") || {}).textContent || ""');
                const banner = await win.webContents.executeJavaScript(
                    '(document.getElementById("snapbanner") || {}).textContent || ""');
                finishSmoke({ ok: true, summary: last, title: win.getTitle(), dataFolderShown: shown, snapshotBanner: banner });
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
        if (!settleDataFolder()) {
            if (SMOKE) finishSmoke({ ok: false, error: 'The data folder is not available.' });
            quitting = true;
            app.quit();
            return;
        }
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
