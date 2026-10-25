// Minimalny DOM do testów skryptów interfejsu (Node bez jsdom). Plik pomocniczy – nie jest testem
// (test_javascript.py uruchamia tylko core/js_tests/*.cjs).
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

class Event {
    constructor(type, init = {}) { this.type = type; this.bubbles = !!init.bubbles; this.detail = init.detail; this.defaultPrevented = false; }
    preventDefault() { this.defaultPrevented = true; }
}
class CustomEvent extends Event {}

const camel = name => name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
const FORM_CONTROLS = new Set(['input', 'select', 'textarea', 'button']);

// Selektory: lista po przecinku, potomek (spacja), dziecko (>), znacznik, #id, .klasa, [atrybut], [atrybut="wartość"].
function parseCompound(text) {
    const parts = {tag: null, id: null, classes: [], attrs: []};
    const re = /^([a-zA-Z][\w-]*)|#([\w-]+)|\.([\w-]+)|\[([\w-]+)(?:([*^$]?=)["']?([^"'\]]*)["']?)?\]/g;
    let match, consumed = 0;
    while ((match = re.exec(text)) && match[0]) {
        consumed += match[0].length;
        if (match[1]) parts.tag = match[1].toLowerCase();
        else if (match[2]) parts.id = match[2];
        else if (match[3]) parts.classes.push(match[3]);
        else parts.attrs.push({name: match[4], op: match[5], value: match[6]});
    }
    if (consumed !== text.length) throw new Error(`Nieobsługiwany selektor w atrapie DOM: ${text}`);
    return parts;
}
function matchesCompound(el, parts) {
    if (!(el instanceof Element)) return false;
    if (parts.tag && el.tagName.toLowerCase() !== parts.tag) return false;
    if (parts.id && el.id !== parts.id) return false;
    if (parts.classes.some(c => !el.classList.contains(c))) return false;
    return parts.attrs.every(({name, op, value}) => {
        const actual = el.getAttribute(name);
        if (actual === null || actual === undefined) return false;
        if (!op) return true;
        if (op === '=') return String(actual) === value;
        if (op === '*=') return String(actual).includes(value);
        if (op === '^=') return String(actual).startsWith(value);
        return String(actual).endsWith(value);
    });
}
function matches(el, selector) {
    return selector.split(',').some(single => {
        const tokens = single.trim().replace(/\s*>\s*/g, ' > ').split(/\s+/);
        const check = (node, index) => {
            if (!matchesCompound(node, parseCompound(tokens[index]))) return false;
            if (index === 0) return true;
            const combinator = tokens[index - 1] === '>';
            const next = combinator ? index - 2 : index - 1;
            if (combinator) return !!node.parentNode && check(node.parentNode, next);
            for (let p = node.parentNode; p; p = p.parentNode) if (check(p, next)) return true;
            return false;
        };
        return check(el, tokens.length - 1);
    });
}

class Element {
    constructor(tag = 'div', ownerDocument = null) {
        this.tagName = tag.toUpperCase(); this.ownerDocument = ownerDocument;
        this.children = []; this.parentNode = null; this.attrs = {}; this.dataset = {}; this.listeners = {};
        this._text = ''; this.id = ''; this.className = ''; this.hidden = false; this.disabled = false;
        this.required = false; this.value = ''; this.checked = false; this.validationMessage = '';
        this.style = {}; this.colSpan = 1; this.rowSpan = 1;
    }
    get classList() {
        const el = this;
        const list = () => el.className.split(/\s+/).filter(Boolean);
        return {contains: c => list().includes(c), add: (...c) => { el.className = [...new Set([...list(), ...c])].join(' '); },
            remove: (...c) => { el.className = list().filter(x => !c.includes(x)).join(' '); }};
    }
    get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
    set textContent(value) { this._detachAll(); this._text = String(value); }
    set innerHTML(_) { throw new Error('Skrypt nie może używać innerHTML (ryzyko XSS).'); }
    get firstElementChild() { return this.children[0] || null; }
    get parentElement() { return this.parentNode instanceof Element ? this.parentNode : null; }
    _adopt(items) {
        return items.map(item => {
            const node = typeof item === 'string' ? Object.assign(new Element('#text', this.ownerDocument), {_text: item}) : item;
            node.parentNode?._remove(node); node.parentNode = this; return node;
        });
    }
    _remove(node) { this.children = this.children.filter(child => child !== node); node.parentNode = null; }
    _detachAll() { this.children.forEach(child => { child.parentNode = null; }); this.children = []; }
    append(...items) { this.children.push(...this._adopt(items)); }
    prepend(...items) { this.children.unshift(...this._adopt(items)); }
    replaceChildren(...items) { this._detachAll(); this._text = ''; this.append(...items); }
    _siblingInsert(items, offset) {
        const parent = this.parentNode;
        if (!parent) throw new Error(`Element ${this.tagName} nie ma rodzica – nie można wstawić obok niego.`);
        const nodes = parent._adopt(items);
        parent.children.splice(parent.children.indexOf(this) + offset, 0, ...nodes);
    }
    before(...items) { this._siblingInsert(items, 0); }
    after(...items) { this._siblingInsert(items, 1); }
    remove() { this.parentNode?._remove(this); }
    contains(node) { for (let n = node; n; n = n.parentNode) if (n === this) return true; return false; }
    setAttribute(name, value) {
        value = String(value);
        if (name === 'id') this.id = value; else if (name === 'class') this.className = value;
        else if (name.startsWith('data-')) this.dataset[camel(name.slice(5))] = value;
        else if (['disabled', 'required', 'hidden', 'checked', 'multiple'].includes(name)) this[name] = true;
        this.attrs[name] = value;
    }
    getAttribute(name) {
        if (name === 'id') return this.id || null;
        if (name === 'class') return this.className || null;
        if (name.startsWith('data-')) return this.dataset[camel(name.slice(5))] ?? null;
        if (['disabled', 'required', 'hidden', 'checked', 'multiple'].includes(name)) return this[name] ? '' : null;
        if (name in this.attrs) return this.attrs[name];
        if (['type', 'name', 'value', 'href'].includes(name) && this[name] !== undefined && this[name] !== '') return String(this[name]);
        return null;
    }
    hasAttribute(name) { return this.getAttribute(name) !== null; }
    removeAttribute(name) {
        delete this.attrs[name];
        if (name.startsWith('data-')) delete this.dataset[camel(name.slice(5))];
        if (['disabled', 'required', 'hidden', 'checked', 'multiple'].includes(name)) this[name] = false;
    }
    descendants() { return this.children.flatMap(child => [child, ...child.descendants()]); }
    querySelectorAll(selector) { return this.descendants().filter(el => matches(el, selector)); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    matches(selector) { return matches(this, selector); }
    closest(selector) { for (let n = this; n instanceof Element; n = n.parentNode) if (matches(n, selector)) return n; return null; }
    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter(f => f !== fn); }
    dispatchEvent(event) {
        if (!event.target) event.target = this;
        for (let node = this; node; node = node.parentNode) {
            for (const fn of [...(node.listeners[event.type] || [])]) fn(event);
            if (!event.bubbles) break;
        }
        return !event.defaultPrevented;
    }
    // Pomocnik testów: zdarzenie z dowolnymi polami (np. key), zwraca obiekt zdarzenia.
    fire(type, init = {}) { const event = Object.assign(new Event(type, init), init); this.dispatchEvent(event); return event; }
    click() { if (!this.disabled) this.dispatchEvent(new Event('click', {bubbles: true})); }
    focus() { if (this.ownerDocument) this.ownerDocument.activeElement = this; this.dispatchEvent(new Event('focus')); }
    scrollIntoView() {}
    setCustomValidity(message) { this.validationMessage = message; }
    get form() { return this._form || this.closest('form'); }
    set form(value) { this._form = value; }
    // <form>
    get elements() {
        const controls = this.descendants().filter(el => FORM_CONTROLS.has(el.tagName.toLowerCase()));
        controls.namedItem = name => controls.find(el => el.name === name) || null;
        return controls;
    }
    // <table>, <thead>/<tbody>, <tr>
    get tHead() { return this.children.find(c => c.tagName === 'THEAD') || null; }
    get tBodies() { return this.children.filter(c => c.tagName === 'TBODY'); }
    get rows() { return this.children.filter(c => c.tagName === 'TR'); }
    get cells() { return this.children.filter(c => c.tagName === 'TH' || c.tagName === 'TD'); }
    // <select>
    get options() { return this.descendants().filter(el => el.tagName === 'OPTION'); }
    get selectedOptions() { return this.options.filter(option => option.selected); }
    add(option) { this.append(option); }
}

class OptionElement extends Element {
    constructor(text = '', value = undefined, ownerDocument = null) {
        super('option', ownerDocument); this._text = String(text); this._value = value === undefined ? undefined : String(value); this.selected = false;
    }
    get text() { return this.textContent; }
    get value() { return this._value === undefined ? this.textContent : this._value; }
    set value(value) { this._value = String(value); }
}

class SelectElement extends Element {
    constructor(ownerDocument = null) { super('select', ownerDocument); this.multiple = false; this.labels = []; this._cleared = false; }
    get value() {
        // Jak w przeglądarce: pojedynczy select bez zaznaczenia pokazuje pierwszą opcję, chyba że ustawiono nieistniejącą wartość.
        const chosen = this.selectedOptions[0] || (!this.multiple && !this._cleared ? this.options[0] : null);
        return chosen ? chosen.value : '';
    }
    set value(value) {
        let found = false;
        this.options.forEach(option => { option.selected = !found && option.value === String(value); found ||= option.selected; });
        this._cleared = !found;
    }
}

class Document extends Element {
    constructor() { super('#document'); this.ownerDocument = this; this.activeElement = null; this.body = this.createElement('body'); this.append(this.body); }
    createElement(tag) {
        if (tag === 'select') return new SelectElement(this);
        if (tag === 'option') return new OptionElement('', undefined, this);
        return new Element(tag, this);
    }
    getElementById(id) { return id ? this.descendants().find(el => el.id === id) || null : null; }
    // Krótki zapis: el('input', {id: 'x', dataset: {...}}, ...dzieci)
    el(tag, props = {}, ...children) {
        const node = this.createElement(tag);
        const {dataset, attrs, ...rest} = props;
        Object.assign(node, rest); Object.assign(node.dataset, dataset || {});
        Object.entries(attrs || {}).forEach(([k, v]) => node.setAttribute(k, v));
        node.append(...children); return node;
    }
    option(text, value, props = {}) { return Object.assign(new OptionElement(text, value, this), props); }
}

// Sterowane zegary: nic nie wykonuje się samo – test woła run()/runAll().
function fakeTimers() {
    const timers = new Map(); let id = 0;
    return {
        setTimeout: (fn, delay = 0) => { timers.set(++id, {fn, delay}); return id; },
        clearTimeout: handle => { timers.delete(handle); },
        pending: () => [...timers.values()],
        runAll() { const due = [...timers.entries()]; timers.clear(); due.forEach(([, t]) => t.fn()); return due.length; },
    };
}

// Atrapa fetch: każde wywołanie czeka na answer()/fail(); abort() odrzuca obietnicę jak w przeglądarce.
function fakeFetch() {
    const requests = [];
    const fetch = (url, options = {}) => new Promise((resolve, reject) => {
        const request = {url: new URL(String(url), 'https://cms.example'), options, resolve, reject, aborted: false};
        options.signal?.addEventListener?.('abort', () => {
            request.aborted = true; reject(Object.assign(new Error('aborted'), {name: 'AbortError'}));
        });
        requests.push(request);
    });
    const answer = (request, body, {ok = true, status = 200} = {}) => request.resolve({ok, status, json: async () => body});
    return {fetch, requests, answer, last: () => requests.at(-1)};
}

const flush = () => new Promise(resolve => setImmediate(resolve));

// Uruchamia skrypt (albo kilka po kolei, np. ['lookup.js', 'author-search.js']) z core/static/core w jednym
// izolowanym kontekście; window === kontekst globalny.
function runScript(names, document, extra = {}) {
    const context = vm.createContext({document, Event, CustomEvent, URL, URLSearchParams, AbortController, location: {origin: 'https://cms.example', pathname: '/'},
        Option: function Option(text, value) { return document.option(text, value); },
        setTimeout: () => { throw new Error('Test musi podać setTimeout.'); }, clearTimeout: () => {},
        console, ...extra});
    context.window = context;
    for (const name of [].concat(names)) {
        vm.runInContext(fs.readFileSync(path.resolve(__dirname, '../../static/core', name), 'utf8'), context);
    }
    return context;
}
const ready = document => document.dispatchEvent(new Event('DOMContentLoaded'));

module.exports = {Document, Element, Event, fakeTimers, fakeFetch, flush, runScript, ready};
