const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {addresses, uniqueEmails, tableEmails, collectServerEmails} = require('../static/core/email-copy.js');

function cell(text, hidden = false) {
    return {nodeType: 1, tagName: 'TD', style: {}, closest: () => hidden ? {} : null,
        childNodes: [{nodeType: 3, textContent: text}]};
}
function row(cells, hidden = false, localPagination = false) {
    return {cells, style: {}, closest: () => hidden ? {} : null, className: localPagination ? 'cms-page-hidden' : ''};
}
assert.deepEqual(addresses('Anna <anna+cms@example.org>, B: b@example.pl'), ['anna+cms@example.org', 'b@example.pl']);
assert.deepEqual(uniqueEmails(['A@example.org', 'a@example.org', 'b@example.pl']), ['A@example.org', 'b@example.pl']);
const table = {tBodies: [{rows: [row([cell('a@example.org')]), row([cell('b@example.org')], false, true),
    row([cell('filtered@example.org')], true), row([cell('hidden@example.org', true)])]}]};
assert.deepEqual(tableEmails(table), ['a@example.org', 'b@example.org']);
assert.deepEqual(tableEmails(table, new Set([0])), []); // Hidden columns stay excluded on fetched pages too.

function themeFixture(saved, blocked = false) {
    const callbacks = [], written = [];
    function button() { return {events: {}, attrs: {}, addEventListener(n, fn) { this.events[n] = fn; }, setAttribute(n,v) { this.attrs[n] = v; }}; }
    const dark = button(), autumn = button(), root = {dataset: {}}, meta = {};
    vm.runInNewContext(fs.readFileSync(path.resolve(__dirname, '../static/core/theme.js'), 'utf8'), {
        document: {documentElement: root, addEventListener: (_, fn) => callbacks.push(fn), querySelectorAll: () => [],
            querySelector: sel => sel === '[data-theme-toggle]' ? dark : sel === '[data-theme-autumn]' ? autumn : meta},
        localStorage: {getItem() { if (blocked) throw Error('blocked'); return saved; }, setItem(k,v) { if (blocked) throw Error('blocked'); written.push(v); }},
    });
    callbacks.forEach(fn => fn()); return {dark, autumn, root, written};
}
let f = themeFixture('autumn'); assert.equal(f.root.dataset.theme, 'autumn'); assert.equal(f.autumn.attrs['aria-pressed'], 'true');
f.dark.events.click(); assert.equal(f.root.dataset.theme, 'dark'); assert.equal(f.autumn.attrs['aria-pressed'], 'false');
f.autumn.events.click(); assert.equal(f.root.dataset.theme, 'autumn');
f.autumn.events.click(); assert.equal(f.root.dataset.theme, 'light'); assert.deepEqual(f.written, ['dark', 'autumn', 'light']);
f = themeFixture('invalid'); assert.equal(f.root.dataset.theme, 'light');
f = themeFixture('autumn', true); f.autumn.events.click(); assert.equal(f.root.dataset.theme, 'autumn');

(async () => {
    const calls = [];
    const result = await collectServerEmails({url: 'https://cms.invalid/list/?q=Anna&roles=1&roles=2&sort=-title&other_page=7',
        pageParam: 'archive_page', sizeParam: 'archive_size', progress() {}, loadPage: async value => {
            const url = new URL(value); calls.push(url);
            const page = Number(url.searchParams.get('archive_page'));
            return {page, pages: 3, count: 1001, emails: [`page${page}@example.org`, 'duplicate@example.org']};
        }});
    assert.deepEqual(result, ['page1@example.org', 'duplicate@example.org', 'page2@example.org', 'page3@example.org']);
    assert.equal(calls.length, 3);
    for (const url of calls) {
        assert.deepEqual(url.searchParams.getAll('roles'), ['1', '2']);
        assert.equal(url.searchParams.get('q'), 'Anna'); assert.equal(url.searchParams.get('other_page'), '7');
        assert.equal(url.searchParams.get('sort'), '-title'); assert.equal(url.searchParams.get('archive_size'), '500');
    }
    await assert.rejects(collectServerEmails({url: 'https://cms.invalid/', pageParam: 'page', sizeParam: 'size', progress() {},
        loadPage: async value => {
            const page = Number(new URL(value).searchParams.get('page'));
            if (page === 2) throw Error('network failed');
            return {page, pages: 2, count: 600, emails: ['partial@example.org']};
        }}), /network failed/); // Must never return a partial result as success.
    await assert.rejects(collectServerEmails({url: 'https://cms.invalid/', pageParam: 'page', sizeParam: 'size', progress() {},
        loadPage: async value => ({page: Number(new URL(value).searchParams.get('page')), pages: 2,
            count: new URL(value).searchParams.get('page') === '1' ? 600 : 599, emails: []})}), /Lista zmieniła/);
    console.log('Email extraction, all-page filtering, failures and theme persistence: OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
