(() => {
    const input = document.getElementById('illustrator-search');
    const choices = document.getElementById('id_illustrators');
    const counter = document.getElementById('illustrator-search-count');
    if (!input || !choices) return;
    const rows = Array.from(choices.querySelectorAll('input[type="checkbox"]')).map(box => ({box, row: box.closest('label').parentElement}));
    const normalize = value => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ł/g, 'l').replace(/Ł/g, 'L').toLowerCase();
    const refresh = () => {
        const tokens = normalize(input.value).trim().split(/\s+/).filter(Boolean);
        let visible = 0;
        rows.forEach(({box, row}) => {
            row.hidden = !box.checked && !tokens.every(token => normalize(row.textContent).includes(token));
            if (!row.hidden) visible += 1;
        });
        if (counter) counter.textContent = `Widoczne osoby: ${visible}. Zaznaczone: ${rows.filter(({box}) => box.checked).length}.`;
    };
    input.addEventListener('input', refresh);
    choices.addEventListener('change', refresh);
    refresh();
})();
