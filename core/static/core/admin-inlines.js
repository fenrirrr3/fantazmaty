document.addEventListener('DOMContentLoaded', () => {
    const updateGroup = group => {
        const stage = group.id.includes('workflow_stages');
        const role = group.id.includes('workflow_role_assignments');
        const note = /notatk[aęi] o autor/i.test(group.querySelector('h2')?.textContent || '');
        group.querySelectorAll('.add-row a').forEach(link => {
            // Django po polsku: „Dodaj kolejne(go)(-ną)(-ny) Treść wiadomości” – bez form w nawiasach.
            const generic = link.textContent.trim().replace(/^Dodaj kolejne\(go\)\(-ną\)\(-ny\)\s+(.+)$/, (_, name) => 'Dodaj: ' + name);
            const label = stage ? 'Dodaj kolejny etap pracy' : role ? 'Dodaj kolejne przypisanie roli'
                : note ? 'Dodaj kolejną notatkę o autorze' : generic;
            if (link.textContent !== label) link.textContent = label;
        });
    };
    document.querySelectorAll('.inline-group').forEach(group => {
        let pending = false;
        updateGroup(group);
        new MutationObserver(() => {
            if (pending) return;
            pending = true;
            requestAnimationFrame(() => { pending = false; updateGroup(group); });
        }).observe(group, {childList: true, subtree: true});
    });
});
