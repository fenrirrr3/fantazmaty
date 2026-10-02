(() => {
  "use strict";
  document.querySelectorAll('.restart-workflow-form').forEach(form => {
    const update = () => {
      const selected = new Set(Array.from(form.querySelectorAll('input[name="stages"]:checked'), input => input.value));
      form.querySelectorAll('[data-repeat-performer]').forEach(row => {
        const enabled = row.dataset.repeatStages.split(',').some(kind => selected.has(kind));
        row.hidden = !enabled;
        const select = row.querySelector('select');
        const search = document.getElementById(`${select.id}_search`);
        select.disabled = !enabled;
        select.required = enabled && !search;
        if (search) { search.disabled = !enabled; search.required = enabled; }
      });
    };
    form.addEventListener('change', event => { if (event.target.name === 'stages') update(); });
    update();
    document.addEventListener('DOMContentLoaded', update);
  });
})();
