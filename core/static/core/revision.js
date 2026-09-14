document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('[data-dashboard-more]').forEach((button, index) => {
        const list = button.closest('section').querySelector('.dashboard-list');
        if (!list) return;
        list.id ||= `dashboard-list-${index}`;
        button.setAttribute('aria-controls', list.id);
        let busy = false, collapsed = false;
        button.addEventListener('click', async event => {
            event.preventDefault();
            if (busy) return;
            const extra = [...list.children].slice(6);
            if (collapsed || !button.dataset.url) {
                collapsed = !collapsed;
                extra.forEach(item => { item.hidden = collapsed; });
                button.setAttribute('aria-expanded', String(!collapsed));
                button.textContent = collapsed ? 'Pokaż pozostałe' : 'Zwiń listę';
                return;
            }
            busy = true;
            button.setAttribute('aria-busy', 'true');
            button.textContent = 'Wczytywanie…';
            try {
                const response = await fetch(button.dataset.url, {headers: {'Accept': 'application/json'}, credentials: 'same-origin'});
                if (!response.ok) throw new Error('load');
                const data = await response.json();
                if (typeof data.html !== 'string') throw new Error('format');
                list.insertAdjacentHTML('beforeend', data.html);
                button.dataset.url = data.next_url || '';
                button.setAttribute('aria-expanded', 'true');
                button.textContent = data.next_url ? 'Pokaż kolejne' : 'Zwiń listę';
            } catch (_) {
                button.textContent = 'Nie udało się wczytać — spróbuj ponownie';
            } finally {
                busy = false;
                button.removeAttribute('aria-busy');
            }
        });
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
