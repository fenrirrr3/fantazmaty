document.querySelector('[data-select-chapters]')?.addEventListener('change', event => {
    document.querySelectorAll('#chapter-assign input[name="chapters"]').forEach(input => { input.checked = event.target.checked; });
});
document.querySelectorAll('select[data-filter-options]').forEach(select => {
    const input = document.createElement('input');
    input.type = 'search'; input.placeholder = 'Filtruj autorów'; input.setAttribute('aria-label', 'Filtruj autorów');
    select.before(input);
    input.addEventListener('input', () => {
        const query = input.value.trim().toLocaleLowerCase('pl');
        Array.from(select.options).forEach(option => { option.hidden = !option.selected && !option.text.toLocaleLowerCase('pl').includes(query); });
    });
});
