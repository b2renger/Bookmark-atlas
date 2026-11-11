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
const ITEMS = [DATABASE, 'secrets.json', 'scripts', 'backups'];
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

function writePointer(settingsDir, dataFolder) {
    fs.mkdirSync(settingsDir, { recursive: true });
    const file = path.join(settingsDir, POINTER);
    if (dataFolder === null) {
        fs.rmSync(file, { force: true });
        return;
    }
    if (!path.isAbsolute(dataFolder)) throw new Error('The data folder must be an absolute path.');
    fs.writeFileSync(file, JSON.stringify({ dataFolder }, null, 2));
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

module.exports = { DATABASE, ITEMS, readPointer, writePointer, resolve, describe, copyData, samePlace, isInside };
