"use strict";
// Dymki „?” (details.help-tip) działają bez JavaScriptu: summary otwiera i zamyka
// opis z klawiatury i dotykiem. Skrypt dodaje: zamykanie klawiszem Escape (fokus
// wraca na „?”), zamykanie kliknięciem poza dymkiem, jeden otwarty dymek naraz
// i dosunięcie dymka do lewej, gdy wychodziłby poza prawą krawędź okna.
(() => {
    const tips = [...document.querySelectorAll("details.help-tip")];
    const EDGE = 16;
    function place(tip) {
        const body = tip.querySelector(".help-tip-body");
        if (!body) return;
        body.classList.remove("is-end");
        const rect = body.getBoundingClientRect();
        if (rect.right > window.innerWidth - EDGE) body.classList.add("is-end");
    }
    tips.forEach((tip) => {
        tip.addEventListener("toggle", () => {
            if (!tip.open) return;
            tips.forEach((other) => { if (other !== tip) other.open = false; });
            place(tip);
        });
    });
    document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape") return;
        tips.filter((tip) => tip.open).forEach((tip) => {
            tip.open = false;
            const summary = tip.querySelector("summary");
            if (summary) summary.focus();
        });
    });
    document.addEventListener("click", (event) => {
        tips.forEach((tip) => {
            if (tip.open && !tip.contains(event.target)) tip.open = false;
        });
    });
})();
