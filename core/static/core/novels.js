const chapterForm = document.querySelector('#chapter-assign');
if (chapterForm) {
    const boxes = [...chapterForm.querySelectorAll('input[name="chapters"]')];
    const all = chapterForm.querySelector('[data-select-chapters]');
    const status = chapterForm.querySelector('[data-selected-chapters]');
    const update = () => {
        const count = boxes.filter(input => input.checked).length;
        if (status) status.textContent = `Zaznaczone rozdziały: ${count}`;
        if (all) { all.checked = count > 0 && count === boxes.length; all.indeterminate = count > 0 && count < boxes.length; }
    };
    all?.addEventListener('change', () => { boxes.forEach(input => { input.checked = all.checked; }); update(); });
    boxes.forEach(input => input.addEventListener('change', update));
    update();
}
document.querySelectorAll('select[data-filter-options]').forEach(select => {
    const input = document.createElement('input');
    input.type = 'search'; input.placeholder = 'Filtruj autorów'; input.setAttribute('aria-label', 'Filtruj autorów');
    select.before(input);
    input.addEventListener('input', () => {
        const query = input.value.trim().toLocaleLowerCase('pl');
        Array.from(select.options).forEach(option => { option.hidden = !option.selected && !option.text.toLocaleLowerCase('pl').includes(query); });
    });
});
