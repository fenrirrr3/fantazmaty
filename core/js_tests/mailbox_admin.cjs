// mailbox-admin.js: w adminie skrzynek widać sekcję IMAP albo SMTP – zależnie od wybranego przeznaczenia.
const assert = require('node:assert/strict');
const {Document, runScript, ready} = require('./support/dom.js');

function page(value) {
    const doc = new Document();
    const purpose = doc.el('select', {name: 'purpose'},
        ...['submissions', 'recruitment', 'split_mailing'].map(option => doc.option(option, option)));
    purpose.value = value;
    const imap = doc.el('fieldset', {className: 'module mailbox-imap'});
    const smtp = doc.el('fieldset', {className: 'module mailbox-smtp'});
    doc.body.append(purpose, imap, smtp);
    runScript('mailbox-admin.js', doc); ready(doc);
    return {purpose, imap, smtp};
}
const visible = ({imap, smtp}) => [!imap.hidden, !smtp.hidden];
const cases = [['submissions', [true, false]], ['recruitment', [true, false]], ['split_mailing', [false, true]]];
for (const [value, expected] of cases) assert.deepEqual(visible(page(value)), expected, `na starcie: ${value}`);
const form = page('submissions');
for (const [value, expected] of [['split_mailing', [false, true]], ['recruitment', [true, false]]]) {
    form.purpose.value = value; form.purpose.fire('change');
    assert.deepEqual(visible(form), expected, `po zmianie na ${value}`);
}
// Strona bez sekcji (np. lista skrzynek) – skrypt nic nie robi i nie zgłasza błędu.
const empty = new Document(); runScript('mailbox-admin.js', empty); ready(empty);
console.log('Mailbox admin: IMAP/SMTP sections follow purpose OK');
