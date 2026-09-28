(() => {
  document.querySelectorAll('select[data-searchable-person]').forEach(select => {
    const options = Array.from(select.options).filter(option => option.value);
    const counts = new Map();
    options.forEach(option => counts.set(option.text, (counts.get(option.text) || 0) + 1));
    const labels = new Map(options.map(option => [option.value, counts.get(option.text) > 1 ? `${option.text} (#${option.value})` : option.text]));
    const values = new Map(Array.from(labels, ([value, label]) => [label, value]));
    const input = document.createElement('input');
    input.type = 'search'; input.id = `${select.id}_search`;
    input.placeholder = 'Wpisz nazwisko i wybierz osobę';
    input.autocomplete = 'off';
    const suggestions = document.createElement('datalist');
    suggestions.id = `${select.id}_suggestions`;
    for (const label of labels.values()) {
      const option = document.createElement('option'); option.value = label; suggestions.append(option);
    }
    input.setAttribute('list', suggestions.id);
    input.value = labels.get(select.value) || '';
    const label = document.querySelector(`label[for="${select.id}"]`);
    if (label) label.htmlFor = input.id;
    input.required = select.required; select.required = false;
    select.hidden = true; select.style.display = 'none';
    select.before(input, suggestions);
    input.addEventListener('input', () => {
      const value = input.value.trim();
      const chosen = values.get(value);
      input.setCustomValidity(value && !chosen ? 'Wybierz osobę z podpowiedzi albo wyczyść pole.' : '');
      select.value = chosen || '';
      select.dispatchEvent(new Event('change', {bubbles: true}));
    });
    select.form?.addEventListener('reset', () => setTimeout(() => {
      input.value = labels.get(select.value) || ''; input.setCustomValidity('');
    }, 0));
  });
})();
