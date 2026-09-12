(() => {
    const key = "fantazmaty-theme";
    try { document.documentElement.dataset.theme = localStorage.getItem(key) === "dark" ? "dark" : "light"; }
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
            if (/koordynator/.test(value)) return "coordinator";
            if (/recenz/.test(value)) return "reviewer";
            if (/gotow/.test(value)) return "ready";
            if (/do redakcji/.test(value)) return "ready-for-editing";
            if (/styl/.test(value)) return "styling";
            if (/weryfik|kontr\. wer/.test(value)) return "verification";
            if (/korekt/.test(value)) return "proofreading";
            if (/redak|redaktor|plik u autora/.test(value)) return "editing";
            return "";
        };
        document.querySelectorAll(".role-badge, .workflow-stage-badge").forEach(el => {
            const color = palette(el.textContent);
            if (color) el.dataset.palette = color;
        });
        document.querySelectorAll(".workflow-stage-row").forEach(row => {
            const value = row.dataset.stage || "";
            const color = value === "ready_for_editing" ? "ready-for-editing" : value === "ready" ? "ready" : value.includes("styling") ? "styling" :
                /verification|coordinator_control|editor_control/.test(value) ? "verification" :
                value.includes("proofreading") ? "proofreading" : value.includes("editing") ? "editing" : "";
            if (color) row.dataset.palette = color;
        });
    });
})();
