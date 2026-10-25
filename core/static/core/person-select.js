// Wyszukiwalny select jednej osoby (przypisania, korekta, scalanie haseł): podpowiedzi z opcji selecta, opcjonalny
// filtr według roli wybranej w innym polu. Wymaga lookup.js. W panelu admina skrypt z Media ładuje się w <head>,
// zanim powstanie formularz, więc widżety powstają dopiero po wczytaniu dokumentu.
(() => {
  "use strict";
  const initialize = () => document.querySelectorAll('select[data-searchable-person]').forEach(select => {
    if (select.dataset.lookupReady) return;
    select.dataset.lookupReady = 'true';
    const options = Array.from(select.options).filter(option => option.value && !option.disabled);
    const roleSelect = document.getElementById(select.dataset.assignmentRoleSelect || '');
    const allowed = value => !roleSelect || options.some(option => option.value === value &&
      (option.dataset.assignmentRoles || '').split(' ').includes(roleSelect.value) && roleSelect.value);
    const counts = new Map();
    options.forEach(option => counts.set(option.text, (counts.get(option.text) || 0) + 1));
    const labels = new Map(options.map(option => [option.value, counts.get(option.text) > 1 ? `${option.text} (#${option.value})` : option.text]));
    const values = new Map(Array.from(labels, ([value, label]) => [label, value]));
    const itemLabel = select.dataset.searchItem || 'osobę';
    const itemsLabel = select.dataset.searchItems || 'osób';
    const field = Lookup.combobox(select, {placeholder: select.dataset.searchPlaceholder || 'Wpisz nazwisko i wybierz osobę'});
    const {input, list, status, wrapper, close} = field;
    input.value = labels.get(select.value) || '';
    input.required = select.required; select.required = false; select.hidden = true;
    select.before(wrapper, status);
    const choose = value => {
      if (!allowed(value)) return;
      select.value = value; input.value = labels.get(value) || ''; input.setCustomValidity('');
      input.focus(); close(); status.textContent = `Wybrano ${itemLabel}.`; select.dispatchEvent(new Event('change', {bubbles: true}));
    };
    const search = () => {
      close(); const query = Lookup.fold(input.value.trim());
      if (!query) { status.textContent = ''; return; }
      const matches = Array.from(labels).filter(([value, label]) => allowed(value) && Lookup.fold(label).includes(query));
      field.show(matches.slice(0, 30), ([, label]) => label, ([value]) => choose(value));
      status.textContent = matches.length > 30 ? `Zawęź zapytanie – pokazano pierwszych 30 ${itemsLabel}.` : matches.length ? `Wybierz ${itemLabel} z listy.` : `Brak pasujących ${itemsLabel}.`;
    };
    input.addEventListener('input', () => {
      const value = input.value.trim(); const candidate = values.get(value);
      const chosen = candidate && allowed(candidate) ? candidate : null;
      input.setCustomValidity(value && !chosen ? `Wybierz ${itemLabel} z podpowiedzi albo wyczyść pole.` : '');
      select.value = chosen || ''; select.dispatchEvent(new Event('change', {bubbles: true})); search();
    });
    input.addEventListener('focus', search);
    let syncRole = null;
    if (roleSelect) {
      syncRole = () => {
        options.forEach(option => { option.disabled = !allowed(option.value); });
        if (!allowed(select.value)) { select.value = ''; input.value = ''; }
        input.disabled = !roleSelect.value;
        input.setCustomValidity(''); close();
        status.textContent = roleSelect.value ? 'Wybierz osobę uprawnioną do tej roli.' : 'Najpierw wybierz rolę.';
      };
      roleSelect.addEventListener('change', syncRole);
      syncRole();
    }
    input.addEventListener('keydown', event => {
      if (event.key === 'Escape') { close(); return; }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); if (!field.move(event.key)) search(); }
      if (event.key === 'Enter' && list.children.length) { event.preventDefault(); field.pick(); }
    });
    field.onOutside(close);
    // Defaults are restored after the reset event; the role hint is recomputed last so it is not wiped.
    select.form?.addEventListener('reset', () => setTimeout(() => { input.value = labels.get(select.value) || ''; input.setCustomValidity(''); status.textContent = ''; close(); syncRole?.(); }, 0));
  });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initialize, {once: true});
  else initialize();
})();
