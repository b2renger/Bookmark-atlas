// Run with: node --test desktop/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const df = require('./datafolder.js');

function temp() {
    return fs.mkdtempSync(path.join(os.tmpdir(), 'atlas-df-'));
}

function seed(folder) {
    fs.mkdirSync(path.join(folder, 'scripts'), { recursive: true });
    fs.mkdirSync(path.join(folder, 'backups'), { recursive: true });
    fs.writeFileSync(path.join(folder, 'atlas.sqlite'), 'database-bytes');
    fs.writeFileSync(path.join(folder, 'secrets.json'), '{"notion_token":"t"}');
    fs.writeFileSync(path.join(folder, 'scripts', 'mine.py'), 'NAME = "mine"');
    fs.writeFileSync(path.join(folder, 'backups', 'atlas-1.sqlite'), 'old');
    fs.writeFileSync(path.join(folder, 'backend.log'), 'machine-specific');
}

test('no pointer means the default folder', () => {
    const settings = temp();
    assert.equal(df.readPointer(settings), null);
    assert.deepEqual(df.resolve({ settingsDir: settings, defaultDir: '/default' }), { folder: '/default', source: 'default' });
});

test('a chosen folder is remembered, and can be forgotten', () => {
    const settings = temp();
    const chosen = temp();
    df.writePointer(settings, chosen);
    assert.equal(df.readPointer(settings), chosen);
    assert.deepEqual(df.resolve({ settingsDir: settings, defaultDir: '/default' }), { folder: chosen, source: 'chosen' });
    df.writePointer(settings, null);
    assert.equal(df.readPointer(settings), null);
});

test('an override wins over the chosen folder', () => {
    const settings = temp();
    df.writePointer(settings, temp());
    const override = temp();
    assert.deepEqual(df.resolve({ override, settingsDir: settings, defaultDir: '/default' }), { folder: override, source: 'override' });
});

test('a damaged or relative pointer is ignored', () => {
    const settings = temp();
    fs.writeFileSync(path.join(settings, 'location.json'), '{not json');
    assert.equal(df.readPointer(settings), null);
    fs.writeFileSync(path.join(settings, 'location.json'), '{"dataFolder":"relative/path"}');
    assert.equal(df.readPointer(settings), null);
    fs.writeFileSync(path.join(settings, 'location.json'), '{"dataFolder":42}');
    assert.equal(df.readPointer(settings), null);
    assert.throws(() => df.writePointer(settings, 'relative'), /absolute/);
});

test('describe tells an empty folder, a collection and a missing folder apart', () => {
    const folder = temp();
    assert.deepEqual(df.describe(folder), { exists: true, hasCollection: false, empty: true, writable: true });
    seed(folder);
    assert.equal(df.describe(folder).hasCollection, true);
    assert.equal(df.describe(folder).empty, false);
    assert.deepEqual(df.describe(path.join(folder, 'nope')), { exists: false, hasCollection: false, empty: true, writable: false });
});

test('copyData brings the whole collection and leaves the source untouched', () => {
    const from = temp();
    const to = path.join(temp(), 'new', 'place');
    seed(from);

    const copied = df.copyData(from, to);

    assert.deepEqual(copied, ['atlas.sqlite', 'secrets.json', 'scripts', 'backups']);
    assert.equal(fs.readFileSync(path.join(to, 'atlas.sqlite'), 'utf8'), 'database-bytes');
    assert.equal(fs.readFileSync(path.join(to, 'scripts', 'mine.py'), 'utf8'), 'NAME = "mine"');
    assert.equal(fs.readFileSync(path.join(to, 'backups', 'atlas-1.sqlite'), 'utf8'), 'old');
    assert.equal(fs.existsSync(path.join(to, 'backend.log')), false, 'machine-specific files stay behind');
    assert.equal(fs.readFileSync(path.join(from, 'atlas.sqlite'), 'utf8'), 'database-bytes');
});

test('copyData never writes over an existing collection', () => {
    const from = temp();
    const to = temp();
    seed(from);
    fs.writeFileSync(path.join(to, 'atlas.sqlite'), 'someone-elses-data');

    assert.throws(() => df.copyData(from, to), /already holds a collection/);
    assert.equal(fs.readFileSync(path.join(to, 'atlas.sqlite'), 'utf8'), 'someone-elses-data');
});

test('copyData refuses the same folder, a folder inside it, and an open database', () => {
    const from = temp();
    seed(from);
    assert.throws(() => df.copyData(from, from), /already the data folder/);
    assert.throws(() => df.copyData(from, path.join(from, 'inner')), /inside the current/);
    assert.equal(fs.existsSync(path.join(from, 'inner')), false);

    fs.writeFileSync(path.join(from, 'atlas.sqlite-journal'), 'x');
    assert.throws(() => df.copyData(from, temp()), /still in use/);
});

test('copyData copes with a source that has only a database', () => {
    const from = temp();
    fs.writeFileSync(path.join(from, 'atlas.sqlite'), 'x');
    assert.deepEqual(df.copyData(from, path.join(temp(), 'b')), ['atlas.sqlite']);
});
