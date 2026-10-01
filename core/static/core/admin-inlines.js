document.addEventListener('DOMContentLoaded', () => {
    const updateGroup = group => {
        const stage = group.id.includes('workflow_stages');
        const role = group.id.includes('workflow_role_assignments');
        const note = /notatk(?:a|ę|i) o autorze/i.test(group.querySelector('h2')?.textContent || '');
        if (stage || role || note) group.querySelectorAll('.add-row a').forEach(link => {
            const label = stage ? 'Dodaj kolejny etap pracy' : role ? 'Dodaj kolejne przypisanie roli' : 'Dodaj kolejną notatkę o autorze';
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
