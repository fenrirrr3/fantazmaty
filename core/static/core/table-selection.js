// Shift selects a range of visible rows in the CURRENT order, within one column.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('main table').forEach(table => {
        // Django admin has its own range-selection handler.
        if (table.id === 'result_list') return;
        const anchors = new Map();
        function visible(box) {
            return !box.disabled && !box.closest('[hidden], .cms-page-hidden') && box.getClientRects().length > 0;
        }
        table.addEventListener('click', event => {
            const box = event.target;
            if (!box.matches('tbody input[type="checkbox"]') || !visible(box)) return;
            const column = box.closest('td, th').cellIndex;
            const key = `${column}:${box.name}`;
            const boxes = [...table.querySelectorAll('tbody input[type="checkbox"]')]
                .filter(item => item.name === box.name && item.closest('td, th').cellIndex === column && visible(item));
            const anchor = anchors.get(key);
            if (event.shiftKey && boxes.includes(anchor)) {
                const left = boxes.indexOf(anchor), right = boxes.indexOf(box);
                boxes.slice(Math.min(left, right), Math.max(left, right) + 1).forEach(item => {
                    if (item !== box && item.checked !== box.checked) {
                        item.checked = box.checked;
                        item.dispatchEvent(new Event('change', {bubbles: true}));
                    }
                });
                // Keep the original anchor for repeated Shift clicks.
            } else anchors.set(key, box);
        });
        table.addEventListener('change', event => {
            const all = table.querySelector('[data-select-table]');
            if (!all) return;
            const boxes = [...table.querySelectorAll('tbody input[name="selected"]')].filter(visible);
            if (event.target === all) {
                const checked = all.checked;
                boxes.forEach(box => { box.checked = checked; });
                anchors.clear();
            }
            all.checked = boxes.length > 0 && boxes.every(box => box.checked);
            all.indeterminate = boxes.some(box => box.checked) && !all.checked;
        });
    });
});
