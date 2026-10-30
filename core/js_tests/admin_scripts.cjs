// Skrypty panelu admina: admin-navigation.js (pamiętanie rozwinięcia grup i przewinięcia menu per użytkownik,
// filtr menu) oraz admin-inlines.js (czytelne etykiety „Dodaj kolejny…” w formularzach wierszowych).
const assert = require('node:assert/strict');
const {Document, runScript, ready} = require('./support/dom.js');

function storage(initial = {}, broken = false) {
    const data = {...initial};
    return {data, getItem: key => { if (broken) throw new Error('blocked'); return key in data ? data[key] : null; },
        setItem: (key, value) => { if (broken) throw new Error('blocked'); data[key] = String(value); }};
}
function navigation({scope = '7', saved = {}, broken = false} = {}) {
    const doc = new Document();
    const group = (name, open = false) => doc.el('details', {open, dataset: {adminGroup: name}}, doc.el('summary', {}, name), doc.el('a', {href: '/admin/x/'}, 'Link'));
    const groups = [group('teksty'), group('ludzie', true), group('teksty')];
    const filter = doc.el('input', {id: 'nav-filter'});
    const sidebar = doc.el('nav', {id: 'nav-sidebar', scrollTop: 0}, filter, ...groups);
    doc.body.append(doc.el('div', scope ? {dataset: {cmsAdminUser: scope}} : {}), sidebar);
    const localStorage = storage(saved, broken);
    runScript('admin-navigation.js', doc, {localStorage}); ready(doc);
    const toggle = (g, open) => { g.open = open; g.fire('toggle'); };
    return {doc, groups, filter, sidebar, localStorage, toggle};
}
const P = 'fantazmaty:admin:7:';

// Zapisany stan grup i przewinięcia jest przywracany (osobno dla każdego użytkownika).
let n = navigation({saved: {[P + 'group:teksty']: '1', [P + 'group:ludzie']: '0', [P + 'sidebar-scroll']: '240', 'fantazmaty:admin:8:group:teksty': '0'}});
assert.deepEqual(n.groups.map(g => g.open), [true, false, true]); assert.equal(n.sidebar.scrollTop, 240);
// Zwinięcie grupy zapisuje stan i synchronizuje jej duplikat (ta sama grupa w dwóch miejscach menu).
n.toggle(n.groups[0], false);
assert.equal(n.localStorage.data[P + 'group:teksty'], '0'); assert.equal(n.groups[2].open, false);
assert.equal(n.localStorage.data['fantazmaty:admin:8:group:teksty'], '0', 'stan innego użytkownika nietknięty');
// Przewinięcie i kliknięcie linku zapamiętują pozycję menu.
n.sidebar.scrollTop = 99; n.sidebar.fire('scroll'); assert.equal(n.localStorage.data[P + 'sidebar-scroll'], '99');
n.sidebar.scrollTop = 120; n.groups[1].children[1].click(); assert.equal(n.localStorage.data[P + 'sidebar-scroll'], '120');
n.sidebar.scrollTop = 130; n.groups[1].children[0].click(); assert.equal(n.localStorage.data[P + 'sidebar-scroll'], '120', 'klik poza linkiem nie zapisuje');
// Filtr menu rozwija wszystkie grupy bez zapisywania tego stanu; wyczyszczenie przywraca zapisany stan.
n.filter.value = 'aut'; n.filter.fire('input');
assert.deepEqual(n.groups.map(g => g.open), [true, true, true]);
n.toggle(n.groups[1], true); assert.equal(n.localStorage.data[P + 'group:ludzie'], '0', 'rozwinięcie przez filtr nie jest zapisywane');
n.filter.value = ' '; n.filter.fire('input');
assert.deepEqual(n.groups.map(g => g.open), [false, false, false]);

// Brak zapisanych wartości: domyślne rozwinięcie z szablonu i przewinięcie 0.
n = navigation(); assert.deepEqual(n.groups.map(g => g.open), [false, true, false]); assert.equal(n.sidebar.scrollTop, 0);
// Zablokowany localStorage: nawigacja działa bez wyjątków.
n = navigation({broken: true}); n.toggle(n.groups[0], true); assert.equal(n.groups[2].open, true);
n.sidebar.fire('scroll'); n.filter.value = 'x'; n.filter.fire('input'); n.filter.value = ''; n.filter.fire('input');
// Bez znacznika użytkownika (np. strona logowania) skrypt nic nie robi.
n = navigation({scope: '', saved: {[P + 'group:teksty']: '1'}}); assert.equal(n.groups[0].open, false);
n.toggle(n.groups[0], true); assert.deepEqual(n.localStorage.data, {[P + 'group:teksty']: '1'});

// admin-inlines.js
const doc = new Document();
const inline = (id, heading, text = 'Dodaj kolejny Workflow stage') => {
    const link = doc.el('a', {}, text);
    return doc.el('div', {id, className: 'inline-group'}, doc.el('h2', {}, heading), doc.el('div', {className: 'add-row'}, link));
};
const groups = [inline('workflow_stages-group', 'Etapy'), inline('workflow_role_assignments-group', 'Role'),
    inline('notes-group', 'Notatki o autorach', 'Dodaj kolejne(go)(-ną)(-ny) Notatka o autorze'), inline('texts-group', 'Teksty'),
    inline('templates-group', 'Treści wiadomości', 'Dodaj kolejne(go)(-ną)(-ny) Treść wiadomości')];
doc.body.append(...groups);
const observers = [], frames = [];
class MutationObserver {
    constructor(callback) { this.callback = callback; observers.push(this); }
    observe(target, options) { this.target = target; this.options = options; }
}
runScript('admin-inlines.js', doc, {MutationObserver, requestAnimationFrame: fn => frames.push(fn)}); ready(doc);
const label = group => group.querySelector('.add-row a').textContent;
// Notatki o autorach (nagłówek w liczbie mnogiej) i dowolny inny formularz: bez form „kolejne(go)(-ną)(-ny)”.
assert.deepEqual(groups.map(label), ['Dodaj kolejny etap pracy', 'Dodaj kolejne przypisanie roli', 'Dodaj kolejną notatkę o autorze',
    'Dodaj kolejny Workflow stage', 'Dodaj: Treść wiadomości']);
assert.equal(observers.length, 5); assert.equal(JSON.stringify(observers[0].options), '{"childList":true,"subtree":true}'); assert.equal(observers[0].target, groups[0]);
// Django dodaje nowy wiersz i link – po zmianach w DOM etykieta jest poprawiana raz na klatkę.
groups[0].querySelector('.add-row').replaceChildren(doc.el('a', {}, 'Dodaj kolejny Workflow stage'));
observers[0].callback(); observers[0].callback();
assert.equal(frames.length, 1, 'wiele mutacji – jedna aktualizacja');
frames.shift()(); assert.equal(label(groups[0]), 'Dodaj kolejny etap pracy');
observers[0].callback(); assert.equal(frames.length, 1, 'po aktualizacji kolejne zmiany znów są obsługiwane');
console.log('Admin scripts: per-user group/scroll memory, duplicate groups, filter, blocked storage; inline add-row labels OK');
