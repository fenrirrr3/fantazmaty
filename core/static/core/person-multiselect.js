(() => {
  "use strict";
  const initialize = () => document.querySelectorAll('select[data-person-multiple]').forEach(select => {
    if (select.dataset.lookupReady) return;
    select.dataset.lookupReady = 'true';
    const options = [...select.options].filter(o => o.value && !o.disabled);
    const fold = value => value.toLocaleLowerCase('pl').normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/ł/g, 'l');
    const counts = new Map();
    options.forEach(o => counts.set(o.text, (counts.get(o.text) || 0) + 1));
    const label = o => counts.get(o.text) > 1 ? `${o.text} (#${o.value})` : o.text;
    const wrapper = document.createElement('div'); wrapper.className = 'person-multiple';
    const chosen = document.createElement('div'); chosen.className = 'person-multiple-chosen';
    const input = document.createElement('input'); input.type = 'search'; input.autocomplete = 'off';
    input.id = `${select.id}_search`; input.placeholder = 'Wpisz nazwisko i wybierz osobę';
    input.setAttribute('role', 'combobox'); input.setAttribute('aria-autocomplete', 'list');
    const list = document.createElement('div'); list.className = 'person-multiple-results'; list.id = `${select.id}_results`;
    list.setAttribute('role', 'listbox'); input.setAttribute('aria-controls', list.id);
    const status = document.createElement('span'); status.className = 'person-multiple-status'; status.setAttribute('role', 'status');
    [...select.labels].forEach(l => { l.htmlFor = input.id; });
    select.hidden = true; select.before(wrapper); wrapper.append(chosen, input, list, status);
    let active = -1;
    const close = () => { list.replaceChildren(); active = -1; input.setAttribute('aria-expanded', 'false'); input.removeAttribute('aria-activedescendant'); };
    const signal = () => select.dispatchEvent(new Event('change', {bubbles: true}));
    const renderChosen = () => {
      chosen.replaceChildren();
      options.filter(o => o.selected).forEach(option => {
        const chip = document.createElement('span'); chip.className = 'person-multiple-chip';
        const name = document.createElement('span'); name.textContent = label(option);
        const remove = document.createElement('button'); remove.type = 'button'; remove.textContent = '×';
        remove.setAttribute('aria-label', `Usuń z konsultacji: ${label(option)}`);
        remove.addEventListener('click', () => { option.selected = false; signal(); input.focus(); });
        chip.append(name, remove); chosen.append(chip);
      });
    };
    const search = () => {
      close(); const query = fold(input.value.trim());
      if (!query) { status.textContent = ''; return; }
      const matches = options.filter(o => !o.selected && fold(label(o)).includes(query));
      matches.slice(0, 20).forEach((option, index) => {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = label(option);
        button.id = `${list.id}_${index}`; button.setAttribute('role', 'option'); button.setAttribute('aria-selected', 'false');
        button.addEventListener('click', () => { option.selected = true; input.value = ''; input.setCustomValidity(''); signal(); input.focus(); status.textContent = 'Dodano osobę. Możesz wyszukać kolejną.'; });
        list.append(button);
      });
      input.setAttribute('aria-expanded', String(!!list.children.length));
      status.textContent = matches.length > 20 ? 'Zawęź wyszukiwanie – pokazano pierwszych 20 osób.' : matches.length ? 'Wybierz osobę z podpowiedzi.' : 'Brak pasujących osób.';
    };
    select.addEventListener('change', () => { renderChosen(); close(); });
    input.addEventListener('input', () => { input.setCustomValidity(input.value.trim() ? 'Wybierz osobę z podpowiedzi albo wyczyść wyszukiwanie.' : ''); search(); });
    input.addEventListener('focus', search);
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') { close(); return; }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); if (!list.children.length) { search(); return; }
        active = (active + (event.key === 'ArrowDown' ? 1 : -1) + list.children.length) % list.children.length;
        [...list.children].forEach((b, i) => b.setAttribute('aria-selected', String(i === active)));
        input.setAttribute('aria-activedescendant', list.children[active].id);
      }
      if (event.key === 'Enter' && input.value.trim()) { event.preventDefault(); if (active >= 0) list.children[active].click(); }
    });
    document.addEventListener('click', event => { if (!wrapper.contains(event.target)) close(); });
    select.form?.addEventListener('reset', () => setTimeout(() => { input.value = ''; input.setCustomValidity(''); renderChosen(); close(); status.textContent = ''; }, 0));
    renderChosen(); close();
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {once: true});
  else initialize();
})();
