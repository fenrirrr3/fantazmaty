(() => {
    const key = "fantazmaty-theme";
    try {
        const saved = localStorage.getItem(key) || localStorage.getItem("theme");
        document.documentElement.dataset.theme = saved === "dark" ? "dark" : "light";
    }
    catch (_) { document.documentElement.dataset.theme = "light"; }
    document.addEventListener("DOMContentLoaded", () => {
        const button = document.querySelector("[data-theme-toggle]");
        if (button) {
            const update = () => {
                const dark = document.documentElement.dataset.theme === "dark";
                button.textContent = dark ? "Włącz jasny motyw" : "Włącz ciemny motyw";
                button.setAttribute("aria-pressed", String(dark));
            };
            button.addEventListener("click", () => {
                const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
                document.documentElement.dataset.theme = next;
                try { localStorage.setItem(key, next); } catch (_) {}
                update();
            });
            update();
        }
        const palette = value => {
            value = value.trim().toLocaleLowerCase("pl");
            if (/koordynator|^k\. redakcji$|^k\. weryfikacji$/.test(value)) return "coordinator";
            if (/recenz/.test(value)) return "reviewer";
            if (/gotow/.test(value)) return "ready";
            if (/do redakcji/.test(value)) return "ready-for-editing";
            if (/styl/.test(value)) return "styling";
            if (/weryfik|kontr\. wer/.test(value)) return "verification";
            if (/korekt/.test(value)) return "proofreading";
            if (/redak|redaktor|plik u autora/.test(value)) return "editing";
            return "";
        };
        const rolePalettes = {
            editor: 'editing', editing_reviewer: 'editing', editing: 'editing', author_editing: 'editing',
            editing_coordinator: 'coordinator', verification_coordinator: 'coordinator',
            coordinator_control: 'coordinator', coordinator_verification_control: 'coordinator',
            reviewer: 'reviewer', styling: 'styling', ready: 'ready', ready_for_editing: 'ready-for-editing',
            proofreader_1: 'proofreading', proofreader_2: 'proofreading', proofreader_3: 'proofreading', proofreader_4: 'proofreading',
            verifier_1: 'verification', verifier_2: 'verification', verifier_3: 'verification', verifier_4: 'verification'
        };
        const stagePalettes = {
            editing: 'editing', author_editing: 'editing', editing_review: 'editing',
            editing_control: 'coordinator', coordinator_control: 'coordinator', editor_control: 'editing',
            ready_for_editing: 'ready-for-editing', ready: 'ready', styling: 'styling',
            first_verification: 'verification', second_verification: 'verification', third_verification: 'verification', fourth_verification: 'verification',
            first_proofreading: 'proofreading', second_proofreading: 'proofreading', third_proofreading: 'proofreading', fourth_proofreading: 'proofreading'
        };
        document.querySelectorAll(".role-badge, .workflow-stage-badge").forEach(el => {
            const color = el.dataset.palette || rolePalettes[el.dataset.role] || stagePalettes[el.dataset.stage] || palette(el.textContent);
            if (color) el.dataset.palette = color;
        });
        document.querySelectorAll(".workflow-stage-row").forEach(row => {
            const value = row.dataset.stage || "";
            const color = stagePalettes[value] || '';
            if (color) row.dataset.palette = color;
        });
    });
})();
