(() => {
  const key = value => value.toLocaleLowerCase('pl').normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ł/g, 'l');
  document.querySelectorAll('select[data-searchable-person]').forEach(select => {
    const options = Array.from(select.options, option => option.cloneNode(true));
    const input = document.createElement('input');
    input.type = 'search'; input.placeholder = 'Szukaj osoby…';
    input.setAttribute('aria-label', 'Szukaj przypisanej osoby');
    input.setAttribute('aria-controls', select.id);
    input.style.marginBottom = '0.5rem';
    select.before(input);
    input.addEventListener('input', () => {
      const selected = select.value, query = key(input.value.trim());
      select.replaceChildren(...options.filter(option => !option.value || option.value === selected || key(option.text).includes(query)).map(option => option.cloneNode(true)));
      select.value = selected;
    });
  });
})();
