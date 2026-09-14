document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.dashboard-list').forEach((list, index) => {
        const rest = [...list.children].slice(6);
        if (!rest.length) return;
        list.id ||= `dashboard-list-${index}`;
        rest.forEach(item => { item.hidden = true; });
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'secondary-button dashboard-expand';
        button.setAttribute('aria-controls', list.id); button.setAttribute('aria-expanded', 'false');
        button.textContent = `Pokaż pozostałe (${rest.length})`;
        button.addEventListener('click', () => {
            const open = button.getAttribute('aria-expanded') !== 'true';
            rest.forEach(item => { item.hidden = !open; });
            button.setAttribute('aria-expanded', String(open));
            button.textContent = open ? 'Zwiń listę' : `Pokaż pozostałe (${rest.length})`;
        });
        list.after(button);
    });
    document.querySelectorAll('form[data-confirm-delete]').forEach(form => {
        form.addEventListener('submit', event => {
            if (!window.confirm(form.dataset.confirmDelete)) event.preventDefault();
        });
    });
    const sidebar = document.getElementById('site-sidebar');
    if (sidebar) {
        const key = `fantazmaty:sidebar:${document.body.dataset.userId || ''}`;
        try { sidebar.scrollTop = Number(sessionStorage.getItem(key)) || 0; } catch (_) {}
        const save = () => { try { sessionStorage.setItem(key, String(sidebar.scrollTop)); } catch (_) {} };
        sidebar.addEventListener('scroll', save, {passive:true});
        window.addEventListener('pagehide', save);
    }
    document.querySelectorAll('form[method="get"] input').forEach(field => { field.autocomplete = 'off'; });
    // A manual reload clears text searches in both the URL and displayed results.
    if (performance.getEntriesByType('navigation')[0]?.type === 'reload') {
        const url = new URL(location.href);
        let changed = false;
        document.querySelectorAll('form[method="get"] input[name="q"], form[method="get"] input[name="query"], form[method="get"] input[type="search"], form[method="get"] input[type="text"]').forEach(field => {
            field.autocomplete = 'off';
            if (url.searchParams.has(field.name)) { url.searchParams.delete(field.name); changed = true; }
            field.value = '';
        });
        if (changed) { url.searchParams.delete('page'); location.replace(url.href); }
    }
});
