// Where the user's data lives, and how to move it.
//
// Everything a user owns is in ONE folder: the database, their grooming
// scripts, the backups and the Notion token. Copying that folder to another
// machine and pointing the app at it brings everything back.
//
// Which folder that is, is remembered per machine in a small pointer file in
// the app's own settings folder. Nothing here depends on Electron, so it is
// tested with plain Node (datafolder.test.js).

const fs = require('node:fs');
const path = require('node:path');

const DATABASE = 'atlas.sqlite';
// What makes up the user's data. Anything else in the folder is left alone.
const ITEMS = [DATABASE, 'secrets.json', 'scripts', 'backups', 'Workspaces'];
const POINTER = 'location.json';

/** The folder chosen on this machine, or null if none was chosen. */
function readPointer(settingsDir) {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(settingsDir, POINTER), 'utf8'));
        return typeof data.dataFolder === 'string' && path.isAbsolute(data.dataFolder) ? data.dataFolder : null;
    } catch {
        return null;
    }
}

function writePointer(settingsDir, dataFolder, home) {
    fs.mkdirSync(settingsDir, { recursive: true });
    const file = path.join(settingsDir, POINTER);
    if (dataFolder === null) {
        fs.rmSync(file, { force: true });
        return;
    }
    if (!path.isAbsolute(dataFolder)) throw new Error('The data folder must be an absolute path.');
    const data = { dataFolder };
    // "home" is set only while a snapshot is open: where to go back to.
    if (home !== undefined) data.home = home;
    fs.writeFileSync(file, JSON.stringify(data, null, 2));
}

/**
 * While a snapshot is open: the collection to return to, as an absolute path
 * or the word "default". Null when no snapshot is open.
 */
function readHome(settingsDir) {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(settingsDir, POINTER), 'utf8'));
        if (data.home === 'default') return 'default';
        return typeof data.home === 'string' && path.isAbsolute(data.home) ? data.home : null;
    } catch {
        return null;
    }
}

const SQLITE_HEADER = Buffer.from('SQLite format 3\u0000', 'latin1');

/** True if the file starts like an SQLite database. */
function looksLikeDatabase(file) {
    try {
        const handle = fs.openSync(file, 'r');
        try {
            const head = Buffer.alloc(16);
            return fs.readSync(handle, head, 0, 16, 0) === 16 && head.equals(SQLITE_HEADER);
        } finally {
            fs.closeSync(handle);
        }
    } catch {
        return false;
    }
}

/**
 * Open a snapshot file: copy it into a folder of its own under
 * ``snapshotsRoot`` and point the app at that folder. The user's collection
 * is not touched, and the snapshot file itself is not modified; what the user
 * changes while looking at the snapshot stays in the copy.
 *
 * ``current`` says where the app is now ({ folder, source }), so that it can
 * come back. Returns the folder created.
 */
function openSnapshot(settingsDir, file, snapshotsRoot, current) {
    if (!looksLikeDatabase(file)) throw new Error('This file is not a Bookmark Atlas snapshot.');
    const base = path.basename(file).replace(/\.atlas-snapshot\.sqlite$|\.sqlite$|\.db$/i, '')
        .replace(/[^A-Za-z0-9 _.\-]/g, '_').slice(0, 60) || 'snapshot';
    fs.mkdirSync(snapshotsRoot, { recursive: true });
    let folder = path.join(snapshotsRoot, base);
    for (let n = 2; fs.existsSync(folder); n += 1) folder = path.join(snapshotsRoot, `${base} (${n})`);
    fs.mkdirSync(folder);
    fs.copyFileSync(file, path.join(folder, DATABASE));
    // Opening a snapshot from inside another one still returns to the real home.
    const home = readHome(settingsDir) ?? (current.source === 'chosen' ? current.folder : 'default');
    writePointer(settingsDir, folder, home);
    return folder;
}

/** Leave the snapshot: point the app back at the user's collection. Returns where, or null. */
function returnHome(settingsDir) {
    const home = readHome(settingsDir);
    if (home === null) return null;
    writePointer(settingsDir, home === 'default' ? null : home);
    return home;
}

/**
 * The folder to use: an override (for tests and troubleshooting), then the
 * one chosen on this machine, then the default.
 */
function resolve({ override, settingsDir, defaultDir }) {
    if (override) return { folder: path.resolve(override), source: 'override' };
    const chosen = readPointer(settingsDir);
    if (chosen) return { folder: chosen, source: 'chosen' };
    return { folder: defaultDir, source: 'default' };
}

/** What is in a folder, as far as Atlas is concerned. */
function describe(folder) {
    let entries;
    try {
        entries = fs.readdirSync(folder);
    } catch {
        return { exists: false, hasCollection: false, empty: true, writable: false };
    }
    let writable = true;
    try {
        fs.accessSync(folder, fs.constants.W_OK);
    } catch {
        writable = false;
    }
    return {
        exists: true,
        hasCollection: entries.includes(DATABASE),
        empty: entries.length === 0,
        writable,
    };
}

function samePlace(a, b) {
    const norm = (p) => {
        const resolved = path.resolve(p);
        return process.platform === 'win32' ? resolved.toLowerCase() : resolved;
    };
    return norm(a) === norm(b);
}

function isInside(child, parent) {
    const relative = path.relative(path.resolve(parent), path.resolve(child));
    return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative);
}

/**
 * Copy the user's data from one folder to another. The source is left
 * untouched, so nothing is lost if the copy is interrupted. Refuses to write
 * over an existing collection. Returns the names copied.
 *
 * Call it only while the backend is stopped: the database must not be open.
 */
function copyData(from, to) {
    if (samePlace(from, to)) throw new Error('That is already the data folder.');
    if (isInside(to, from)) throw new Error('The new folder cannot be inside the current data folder.');
    fs.mkdirSync(to, { recursive: true });
    if (describe(to).hasCollection) {
        throw new Error('That folder already holds a collection. Nothing was copied.');
    }
    // A journal beside the database means it was not closed cleanly.
    for (const suffix of ['-journal', '-wal']) {
        if (fs.existsSync(path.join(from, DATABASE + suffix))) {
            throw new Error('The database is still in use. Close Bookmark Atlas completely and try again.');
        }
    }
    const copied = [];
    for (const name of ITEMS) {
        const source = path.join(from, name);
        if (!fs.existsSync(source)) continue;
        fs.cpSync(source, path.join(to, name), { recursive: true, errorOnExist: true, force: false });
        copied.push(name);
    }
    // The database is what matters: make sure it arrived whole.
    if (copied.includes(DATABASE)) {
        const a = fs.statSync(path.join(from, DATABASE)).size;
        const b = fs.statSync(path.join(to, DATABASE)).size;
        if (a !== b) throw new Error('The database copy is incomplete. The original is untouched.');
    }
    return copied;
}

module.exports = {
    DATABASE, ITEMS, readPointer, writePointer, readHome, looksLikeDatabase, openSnapshot, returnHome,
    resolve, describe, copyData, samePlace, isInside,
};
