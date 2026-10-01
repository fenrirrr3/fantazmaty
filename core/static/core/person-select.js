(() => {
  "use strict";
  document.querySelectorAll('select[data-searchable-person]').forEach(select => {
    const options = Array.from(select.options).filter(option => option.value && !option.disabled);
    const counts = new Map();
    options.forEach(option => counts.set(option.text, (counts.get(option.text) || 0) + 1));
    const labels = new Map(options.map(option => [option.value, counts.get(option.text) > 1 ? `${option.text} (#${option.value})` : option.text]));
    const values = new Map(Array.from(labels, ([value, label]) => [label, value]));
    const normalize = value => value.toLocaleLowerCase('pl').normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ł/g, 'l');
    const input = document.createElement('input');
    input.type = 'search'; input.id = `${select.id}_search`;
    input.placeholder = 'Wpisz nazwisko i wybierz osobę'; input.autocomplete = 'off';
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list'); input.setAttribute('aria-expanded', 'false');
    const wrapper = document.createElement('span'); wrapper.className = 'lookup-field';
    const list = document.createElement('div'); list.className = 'author-suggestions';
    list.id = `${select.id}_suggestions`; list.setAttribute('role', 'listbox'); input.setAttribute('aria-controls', list.id);
    const status = document.createElement('span'); status.className = 'lookup-status'; status.setAttribute('role', 'status');
    input.value = labels.get(select.value) || '';
    Array.from(select.labels).forEach(label => { label.htmlFor = input.id; });
    input.required = select.required; select.required = false; select.hidden = true;
    select.before(wrapper, status); wrapper.append(input, list);
    let active = -1;
    const close = () => { list.replaceChildren(); active = -1; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); };
    const choose = value => {
      select.value = value; input.value = labels.get(value) || ''; input.setCustomValidity('');
      input.focus(); close(); status.textContent = 'Wybrano osobę.'; select.dispatchEvent(new Event('change', {bubbles: true}));
    };
    const search = () => {
      close(); const query = normalize(input.value.trim());
      if (!query) { status.textContent = ''; return; }
      const matches = Array.from(labels).filter(([, label]) => normalize(label).includes(query));
      matches.slice(0, 30).forEach(([value, label], index) => {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
        button.id = `${list.id}_${index}`; button.setAttribute('role', 'option'); button.setAttribute('aria-selected', 'false');
        button.addEventListener('click', () => choose(value)); list.append(button);
      });
      input.setAttribute('aria-expanded', String(!!list.children.length));
      status.textContent = matches.length > 30 ? 'Zawęź zapytanie – pokazano pierwszych 30 osób.' : matches.length ? 'Wybierz osobę z listy.' : 'Brak pasujących osób.';
    };
    input.addEventListener('input', () => {
      const value = input.value.trim(); const chosen = values.get(value);
      input.setCustomValidity(value && !chosen ? 'Wybierz osobę z podpowiedzi albo wyczyść pole.' : '');
      select.value = chosen || ''; select.dispatchEvent(new Event('change', {bubbles: true})); search();
    });
    input.addEventListener('focus', search);
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') { close(); return; }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); const choices = Array.from(list.children); if (!choices.length) { search(); return; }
        active = (active + (event.key === 'ArrowDown' ? 1 : -1) + choices.length) % choices.length;
        choices.forEach((button, index) => button.setAttribute('aria-selected', String(index === active)));
        input.setAttribute('aria-activedescendant', choices[active].id); choices[active].scrollIntoView({block: 'nearest'});
      }
      if (event.key === 'Enter' && list.children.length) { event.preventDefault(); if (active >= 0) list.children[active].click(); }
    });
    document.addEventListener('click', event => { if (!wrapper.contains(event.target)) close(); });
    select.form?.addEventListener('reset', () => setTimeout(() => { input.value = labels.get(select.value) || ''; input.setCustomValidity(''); status.textContent = ''; close(); }, 0));
  });
})();
