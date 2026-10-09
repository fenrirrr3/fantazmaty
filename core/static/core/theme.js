// Motyw strony: odczyt zapisanego wyboru przed renderowaniem i przyciski w stopce.
// Kolory odznak ról i etapów ustala serwer (core/palettes.py).
(() => {
    const key = "fantazmaty-theme";
    try {
        const saved = localStorage.getItem(key) || localStorage.getItem("theme");
        document.documentElement.dataset.theme = ["dark", "autumn"].includes(saved) ? saved : "light";
    }
    catch (_) { document.documentElement.dataset.theme = "light"; }
    document.addEventListener("DOMContentLoaded", () => {
        const button = document.querySelector("[data-theme-toggle]");
        const autumn = document.querySelector("[data-theme-autumn]");
        const update = () => {
            const current = document.documentElement.dataset.theme;
            if (button) {
                button.textContent = current === "dark" ? "Włącz jasny motyw" : "Włącz ciemny motyw";
                button.setAttribute("aria-pressed", String(current === "dark"));
            }
            if (autumn) autumn.setAttribute("aria-pressed", String(current === "autumn"));
            const meta = document.querySelector('meta[name="theme-color"]');
            if (meta) meta.content = current === "autumn" ? "#503322" : "#172434";
        };
        const setTheme = theme => {
            document.documentElement.dataset.theme = theme;
            try { localStorage.setItem(key, theme); } catch (_) {}
            update();
        };
        button?.addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
        autumn?.addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "autumn" ? "light" : "autumn"));
        update();
    });
})();
