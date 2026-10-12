// Formularz filtrów bez przycisku wysyłania dostaje „Filtruj”; przycisk bez atrybutu type (domyślnie submit)
// też się liczy – wcześniej Audiodeskrypcje i Rekrutacja miały dwa przyciski „Filtruj”.
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../static/core/ui.js'), 'utf8');

function control(tag, type) {
    return {tagName: tag, getAttribute: name => (name === 'type' ? type : null)};
}
function run(controls) {
    const appended = [], callbacks = [];
    const form = {
        elements: {namedItem: () => null}, append: node => appended.push(node), addEventListener() {},
        querySelectorAll: selector => (selector === 'button, input[type="submit"]' ? controls : []),
        querySelector: () => null,
    };
    const document = {
        readyState: 'loading', addEventListener: (event, fn) => callbacks.push(fn), querySelector: () => null,
        querySelectorAll: selector => (selector.includes('.filters-form') ? [form] : []),
        createElement: tag => ({tagName: tag.toUpperCase()}),
    };
    vm.runInNewContext(script, {document, window: {__fantazmatyUIInitialized: true}, location: {href: 'https://cms.test/x'},
        URL, URLSearchParams, setTimeout, clearTimeout});
    callbacks[1]();
    return appended.filter(node => node.textContent === 'Filtruj').length;
}
const cases = {
    'no buttons': [[], 1],
    'button without type attribute': [[control('BUTTON', null)], 0],
    'explicit submit button': [[control('BUTTON', 'submit')], 0],
    'submit input': [[control('INPUT', 'submit')], 0],
    'only a type="button" helper': [[control('BUTTON', 'button')], 1],
};
for (const [name, [controls, expected]] of Object.entries(cases)) assert.equal(run(controls), expected, name);
console.log('OK: filter forms get exactly one submit button.');
