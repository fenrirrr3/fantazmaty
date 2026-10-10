document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('post-layout-bulk');
    if (!form) return;
    const action = form.querySelector('[name="operation"]');
    const update = () => {
        form.querySelectorAll('[data-bulk-action]').forEach(panel => {
            const active = panel.dataset.bulkAction === action.value;
            panel.hidden = !active;
            panel.querySelectorAll('input, select').forEach(field => {
                field.disabled = !active;
                field.required = active;
                if (!active && field.type === 'checkbox') field.checked = false;
            });
        });
    };
    action.addEventListener('change', update);
    window.addEventListener('pageshow', update);
    update();
});
