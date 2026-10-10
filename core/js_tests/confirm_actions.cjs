const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const assert = require('node:assert/strict');
const code = fs.readFileSync(path.resolve(__dirname, '../static/core/ui.js'), 'utf8');
const start = code.indexOf('    // Nieodwracalne decyzje');
const handler = code.slice(start, code.indexOf('    const sidebar', start));

function run({formData = {}, submitter = null, fields = {}, answer = true}) {
    let listener = null;
    const asked = [];
    class HTMLFormElement {}
    const form = Object.assign(new HTMLFormElement(), {
        dataset: formData,
        matches: () => false,
        elements: {namedItem: name => (name in fields ? {value: fields[name]} : null)},
    });
    const context = {
        HTMLFormElement,
        document: {addEventListener(type, fn) { if (type === 'submit') listener = fn; }},
        window: {confirm(question) { asked.push(question); return answer; }},
    };
    vm.createContext(context);
    vm.runInContext(handler, context);
    const event = {target: form, submitter, prevented: false, stopped: false,
        preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; }};
    listener(event);
    return {asked, event};
}

let result = run({submitter: {dataset: {confirm: 'Zakończyć etap?'}}, answer: false});
assert.deepEqual(result.asked, ['Zakończyć etap?']);
assert.equal(result.event.prevented, true, 'Odmowa blokuje wysłanie');
assert.equal(result.event.stopped, true, 'Odmowa nie oznacza formularza jako zapisanego');

result = run({submitter: {dataset: {confirm: 'Zakończyć etap?'}}});
assert.equal(result.event.prevented, false);

result = run({submitter: {dataset: {}}});
assert.deepEqual(result.asked, [], 'Zwykłe formularze nie pytają');

const ad = {confirm: 'Zakończyć AD?', confirmWhen: 'stage=completed'};
assert.deepEqual(run({formData: ad, submitter: {dataset: {}}, fields: {stage: 'writing'}}).asked, []);
assert.deepEqual(run({formData: ad, submitter: {dataset: {}}, fields: {stage: 'completed'}}).asked, ['Zakończyć AD?']);
console.log('confirm_actions ok');
