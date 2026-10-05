(() => {
    const input = document.getElementById('illustrator-search');
    const select = document.getElementById('id_illustrator');
    const counter = document.getElementById('illustrator-search-count');
    if (!input || !select) return;
    const options = Array.from(select.options);
    const normalize = value => value.normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ł/g, 'l').replace(/Ł/g, 'L').toLowerCase();
    input.addEventListener('input', () => {
        const tokens = normalize(input.value).trim().split(/\s+/).filter(Boolean);
        const selected = select.value;
        const matching = options.filter(option => !option.value || option.value === selected || tokens.every(token => normalize(option.textContent).includes(token)));
        select.replaceChildren(...matching);
        select.value = selected;
        counter.textContent = `Widoczne osoby: ${matching.filter(option => option.value).length}.`;
    });
})();
