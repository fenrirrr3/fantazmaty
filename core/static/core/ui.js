(() => {
    "use strict";

    if (window.__fantazmatyUIInitialized) return;
    window.__fantazmatyUIInitialized = true;

    const LINK_DELAY = 500;
    const EDGE_TOLERANCE = 4;
    const feedbackTimers = new WeakMap();
    const pendingLinks = new Map();
    const trackedForms = new Map();
    const initialized = new WeakSet();
    const collator = new Intl.Collator("pl", {
        numeric: true,
        sensitivity: "base",
    });

    const storage = {
        get(key, persistent = false) {
            try {
                return (persistent ? localStorage : sessionStorage).getItem(key);
            } catch {
                return null;
            }
        },
        set(key, value, persistent = false) {
            try {
                (persistent ? localStorage : sessionStorage).setItem(key, value);
            } catch {
                // Interfejs działa również przy zablokowanej pamięci przeglądarki.
            }
        },
        remove(key, persistent = false) {
            try {
                (persistent ? localStorage : sessionStorage).removeItem(key);
            } catch {
                // Brak dostępu do pamięci nie blokuje obsługi strony.
            }
        },
    };

    const elementFrom = (event) =>
        event.target instanceof Element ? event.target : null;

    const find = (selector, root = document) => {
        try {
            return root.querySelector(selector);
        } catch {
            return null;
        }
    };

    const userScope = () => document.body.dataset.userId || "";

    const scopedKey = (kind, name) => {
        const userId = userScope();
        return userId
            ? `fantazmaty:v2:${userId}:${location.pathname}:${kind}:${name}`
            : null;
    };

    let toast;
    let toastTimer;

    function announce(message, isError = false) {
        if (!toast) {
            toast = document.createElement("div");
            toast.className = "copy-toast";
            toast.setAttribute("role", "status");
            toast.setAttribute("aria-live", "polite");
            toast.setAttribute("aria-atomic", "true");
            document.body.append(toast);
        }

        clearTimeout(toastTimer);
        toast.hidden = false;
        toast.classList.toggle("is-error", isError);
        toast.textContent = message;

        toastTimer = setTimeout(() => {
            toast.hidden = true;
            toast.textContent = "";
        }, isError ? 5000 : 2200);
    }

    function flash(element, className) {
        if (!element) return;

        clearTimeout(feedbackTimers.get(element));
        element.classList.remove("copied-cell", "copy-error", "is-copied");
        element.classList.add(className);

        feedbackTimers.set(element, setTimeout(() => {
            element.classList.remove(className);
            feedbackTimers.delete(element);
        }, 1600));
    }

    function legacyCopy(value) {
        const active = document.activeElement;
        const selection = window.getSelection();
        const ranges = [];

        if (selection) {
            for (let index = 0; index < selection.rangeCount; index += 1) {
                ranges.push(selection.getRangeAt(index).cloneRange());
            }
        }

        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.readOnly = true;
        textarea.tabIndex = -1;
        textarea.style.cssText =
            "position:fixed;left:-10000px;top:0;width:1px;height:1px;";

        const host = active?.closest?.("dialog[open]") || document.body;
        host.append(textarea);

        try {
            textarea.focus({ preventScroll: true });
            textarea.select();
            return document.execCommand("copy");
        } catch {
            return false;
        } finally {
            textarea.remove();

            if (active instanceof HTMLElement && active.isConnected) {
                active.focus({ preventScroll: true });
            }

            if (selection) {
                selection.removeAllRanges();
                ranges.forEach((range) => {
                    try {
                        selection.addRange(range);
                    } catch {
                        // Zaznaczony element mógł zniknąć z dokumentu.
                    }
                });
            }
        }
    }

    async function copy(value, element, isButton = false) {
        if (typeof value !== "string" || !value.trim()) {
            announce("Ta komórka nie zawiera tekstu do skopiowania.");
            return;
        }

        let succeeded = false;

        if (window.isSecureContext && navigator.clipboard?.writeText) {
            try {
                await navigator.clipboard.writeText(value);
                succeeded = true;
            } catch {
                // Awaryjna obsługa przeglądarek odmawiających Clipboard API.
            }
        }

        if (!succeeded) succeeded = legacyCopy(value);

        if (succeeded) {
            flash(element, isButton ? "is-copied" : "copied-cell");
            announce("Skopiowano do schowka.");
        } else {
            flash(element, "copy-error");
            announce(
                "Nie udało się skopiować. Zaznacz tekst i użyj Ctrl+C lub ⌘C.",
                true,
            );
        }
    }

    function visibleText(root) {
        const parts = [];

        function visit(node) {
            if (node.nodeType === Node.TEXT_NODE) {
                parts.push(node.nodeValue || "");
                return;
            }
            if (!(node instanceof Element)) return;

            if (node.matches(
                "[hidden], [aria-hidden='true'], .hidden-column, " +
                ".sr-only, .visually-hidden, script, style, template, " +
                "button, input, select, textarea, .copy-value-button, " +
                "[data-copy-ignore], [data-copy='false']"
            )) return;

            const style = getComputedStyle(node);
            if (style.display === "none" || style.visibility === "hidden") return;

            if (node.tagName === "BR") {
                parts.push("\n");
                return;
            }

            if (node !== root && node.hasAttribute("data-copy-value")) {
                parts.push(node.dataset.copyValue);
                return;
            }

            if (node instanceof HTMLDetailsElement && !node.open) {
                const summary = node.querySelector(":scope > summary");
                if (summary) visit(summary);
                return;
            }

            const block = ["block", "flex", "grid", "list-item"].includes(
                style.display,
            );
            if (block) parts.push("\n");
            node.childNodes.forEach(visit);
            if (block) parts.push("\n");
        }

        visit(root);

        return parts.join("")
            .replace(/\u00a0/g, " ")
            .replace(/[ \t]+/g, " ")
            .replace(/ *\n */g, "\n")
            .replace(/\n{3,}/g, "\n\n")
            .trim();
    }

    function cellValue(cell) {
        if (cell.hasAttribute("data-copy-value")) {
            return cell.dataset.copyValue;
        }
        return visibleText(cell);
    }

    function copyCellFrom(target) {
        if (!target || target.closest(
            "[data-copy='false'], [data-copy-ignore], " +
            "input, textarea, select, button, summary, " +
            ".add-row, .inline-deletelink, .related-widget-wrapper-link, " +
            "a[role='button'], a[href^='#'], " +
            "[contenteditable]:not([contenteditable='false'])"
        )) return null;

        const cell = target.closest("td, th");
        if (!cell || !cell.closest("table")) return null;
        return cell;
    }

    function cancelLink(link) {
        const timer = pendingLinks.get(link);
        if (timer !== undefined) clearTimeout(timer);
        pendingLinks.delete(link);
    }

    function initializeClipboard() {
        const selected = new Set();
        let anchor = null;
        const clearCells = () => {
            selected.forEach(cell => cell.classList.remove("copy-selected"));
            selected.clear();
        };
        const addCell = cell => { selected.add(cell); cell.classList.add("copy-selected"); };
        const selectionText = () => {
            const cells = [...selected].filter(cell => cell.isConnected);
            if (!cells.length) return "";
            const table = cells[0].closest("table");
            const columns = [...new Set(cells.map(cell => cell.cellIndex))].sort((a,b) => a-b);
            return [...table.rows].filter(row => [...row.cells].some(cell => selected.has(cell)))
                .map(row => columns.map(column => {
                    const cell = row.cells[column];
                    return cell && selected.has(cell) ? cellValue(cell).replace(/\t/g, " ").replace(/\r?\n/g, " ") : "";
                }).join("\t")).join("\n");
        };
        document.addEventListener("click", event => {
            const target = elementFrom(event);
            const cell = copyCellFrom(target);
            if (!(event.ctrlKey || event.metaKey || event.shiftKey)) {
                if (!target?.closest("input,textarea,select,button")) { clearCells(); anchor = cell; }
                return;
            }
            if (!cell || event.button !== 0) return;
            event.preventDefault();
            event.stopImmediatePropagation();
            const link = target.closest("a[href]");
            if (link) cancelLink(link);
            if (anchor && anchor.closest("table") !== cell.closest("table")) { clearCells(); anchor = null; }
            if (event.shiftKey && anchor) {
                const table = cell.closest("table");
                const start = Math.min(anchor.parentElement.rowIndex, cell.parentElement.rowIndex);
                const end = Math.max(anchor.parentElement.rowIndex, cell.parentElement.rowIndex);
                const left = Math.min(anchor.cellIndex, cell.cellIndex);
                const right = Math.max(anchor.cellIndex, cell.cellIndex);
                if (!(event.ctrlKey || event.metaKey)) clearCells();
                for (let r = start; r <= end; r++) for (let c = left; c <= right; c++) {
                    const candidate = table.rows[r].cells[c];
                    if (candidate && copyCellFrom(candidate) && !candidate.querySelector("input,select,textarea,button")) addCell(candidate);
                }
            } else if (selected.has(cell)) {
                selected.delete(cell); cell.classList.remove("copy-selected");
            } else { addCell(cell); anchor = cell; }
            window.getSelection()?.removeAllRanges();
            announce(`Zaznaczono komórek: ${selected.size}. Ctrl+C / ⌘C kopiuje zaznaczenie.`);
        }, true);
        document.addEventListener("copy", event => {
            if (elementFrom(event)?.closest("input,textarea,[contenteditable='true']")) return;
            const value = selectionText();
            if (!value || !event.clipboardData) return;
            event.preventDefault();
            event.clipboardData.setData("text/plain", value);
            announce("Skopiowano zaznaczone komórki.");
        });
        document.addEventListener("keydown", event => {
            if (event.key === "Escape") { clearCells(); anchor = null; }
        });
        document.addEventListener("dblclick", (event) => {
            if (event.button !== 0) return;

            const target = elementFrom(event);
            const cell = copyCellFrom(target);
            if (!cell) return;

            const link = target.closest("a[href]");
            if (link) cancelLink(link);

            event.preventDefault();
            event.stopImmediatePropagation();
            if (selected.size && (event.ctrlKey || event.metaKey || event.shiftKey)) return;
            void copy(cellValue(cell), cell);
        }, true);

        document.addEventListener("click", (event) => {
            const target = elementFrom(event);
            if (!target) return;

            const button = target.closest(
                "button[data-copy-value], button[data-copy-target], " +
                ".copy-value-button[data-copy-value], " +
                ".copy-value-button[data-copy-target]"
            );

            if (button) {
                event.preventDefault();
                event.stopImmediatePropagation();

                const source = button.dataset.copyTarget
                    ? find(button.dataset.copyTarget)
                    : null;
                const value = button.hasAttribute("data-copy-value")
                    ? button.dataset.copyValue
                    : source instanceof HTMLInputElement ||
                      source instanceof HTMLTextAreaElement
                        ? source.value
                        : source ? visibleText(source) : "";

                void copy(value, button, true);
                return;
            }

            const link = target.closest("a[href]");
            if (
                !link ||
                !copyCellFrom(target) ||
                event.button !== 0 ||
                event.detail === 0 ||
                event.ctrlKey || event.metaKey || event.shiftKey || event.altKey ||
                link.hasAttribute("download") ||
                (link.target && link.target !== "_self")
            ) return;

            const url = new URL(link.href, location.href);
            if (!["http:", "https:", "mailto:", "tel:"].includes(url.protocol)) {
                return;
            }

            // Krótkie opóźnienie zapobiega nawigacji po pierwszym kliknięciu,
            // zanim użytkownik wykona drugie kliknięcie w komórce z linkiem.
            event.preventDefault();
            event.stopImmediatePropagation();
            cancelLink(link);

            if (event.detail >= 2) return;

            pendingLinks.set(link, setTimeout(() => {
                pendingLinks.delete(link);
                if (link.isConnected) location.assign(url.href);
            }, LINK_DELAY));
        }, true);

        document.addEventListener("keydown", (event) => {
            if (
                !(event.ctrlKey || event.metaKey) ||
                event.altKey || event.shiftKey ||
                event.key.toLowerCase() !== "c"
            ) return;

            // Nie przechwytujemy zwykłego kopiowania zaznaczonego tekstu.
            if (window.getSelection()?.toString()) return;

            const cell = copyCellFrom(elementFrom(event));
            if (!cell) return;

            event.preventDefault();
            if (selected.size && (event.ctrlKey || event.metaKey || event.shiftKey)) return;
            void copy(cellValue(cell), cell);
        });
    }

    function initializeNavigation() {
        const button = find("#menu-button, [data-menu-toggle]");
        const controlledId = button?.getAttribute("aria-controls");
        const panel = controlledId
            ? document.getElementById(controlledId)
            : find("#navigation-links, .site-sidebar");
        const dropdowns = [...document.querySelectorAll(".navigation-dropdown")];

        const closeDropdowns = () => {
            dropdowns.forEach((dropdown) => { dropdown.open = false; });
        };

        const closePanel = () => {
            panel?.classList.remove("is-open");
            button?.setAttribute("aria-expanded", "false");
        };

        button?.addEventListener("click", () => {
            if (!panel) return;
            const open = panel.classList.toggle("is-open");
            button.setAttribute("aria-expanded", String(open));
            if (!open) closeDropdowns();
        });

        dropdowns.forEach((dropdown) => {
            dropdown.addEventListener("toggle", () => {
                if (!dropdown.open) return;
                dropdowns.forEach((other) => {
                    if (other !== dropdown) other.open = false;
                });
            });
        });

        document.addEventListener("pointerdown", (event) => {
            const target = elementFrom(event);
            if (!target || panel?.contains(target) || button?.contains(target)) {
                return;
            }
            if (!target.closest(".navigation-dropdown")) closeDropdowns();
            closePanel();
        });

        document.addEventListener("keydown", (event) => {
            if (event.key !== "Escape") return;

            const openDropdown = dropdowns.find((item) =>
                item.open && item.contains(document.activeElement)
            );
            if (openDropdown) {
                openDropdown.open = false;
                openDropdown.querySelector("summary")?.focus();
                return;
            }

            if (panel?.classList.contains("is-open")) {
                closePanel();
                button?.focus();
            }
        });
    }

    function initializeScrollableTable(container) {
        if (initialized.has(container)) return;
        const table = container.querySelector("table");
        if (!table) return;
        initialized.add(container);

        const shell = document.createElement("div");
        shell.className = "table-scroll-shell";
        container.before(shell);
        shell.append(container);

        const top = document.createElement("div");
        top.className = "table-scroll-top";
        top.tabIndex = 0;
        top.setAttribute("role", "region");
        top.setAttribute("aria-label", "Górny pasek przewijania tabeli");

        const spacer = document.createElement("div");
        spacer.className = "table-scroll-top-content";
        top.append(spacer);
        container.before(top);

        const createArrow = (direction, label, symbol) => {
            const button = document.createElement("button");
            button.type = "button";
            button.className = `table-scroll-button table-scroll-button-${direction}`;
            button.setAttribute("aria-label", label);
            button.textContent = symbol;
            shell.append(button);
            return button;
        };

        const left = createArrow("left", "Przewiń tabelę w lewo", "‹");
        const right = createArrow("right", "Przewiń tabelę w prawo", "›");
        let frame = null;

        const update = () => {
            frame = null;
            const maximum = Math.max(0, container.scrollWidth - container.clientWidth);
            const overflow = maximum > EDGE_TOLERANCE;
            spacer.style.width = `${container.scrollWidth}px`;
            top.hidden = !overflow;

            if (Math.abs(top.scrollLeft - container.scrollLeft) > 1) {
                top.scrollLeft = container.scrollLeft;
            }

            left.disabled = container.scrollLeft <= EDGE_TOLERANCE;
            right.disabled = container.scrollLeft >= maximum - EDGE_TOLERANCE;
            left.classList.toggle("is-visible", overflow && !left.disabled);
            right.classList.toggle("is-visible", overflow && !right.disabled);
        };

        const schedule = () => {
            if (frame === null) frame = requestAnimationFrame(update);
        };

        container.addEventListener("scroll", () => {
            top.scrollLeft = container.scrollLeft;
            schedule();
        }, { passive: true });

        top.addEventListener("scroll", () => {
            if (Math.abs(container.scrollLeft - top.scrollLeft) > 1) {
                container.scrollLeft = top.scrollLeft;
            }
        }, { passive: true });

        const scroll = (direction) => {
            container.scrollBy({
                left: direction * Math.max(200, container.clientWidth * 0.7),
                behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
                    ? "auto" : "smooth",
            });
        };

        left.addEventListener("click", () => scroll(-1));
        right.addEventListener("click", () => scroll(1));

        top.addEventListener("keydown", (event) => {
            if (!["Home", "End"].includes(event.key)) return;
            event.preventDefault();
            container.scrollLeft = event.key === "Home" ? 0 : container.scrollWidth;
        });

        if ("ResizeObserver" in window) {
            const observer = new ResizeObserver(schedule);
            observer.observe(table);
            observer.observe(container);
        } else {
            window.addEventListener("resize", schedule);
        }

        new MutationObserver(schedule).observe(table, {
            childList: true,
            subtree: true,
            characterData: true,
            attributes: true,
            attributeFilter: ["class", "hidden", "style", "colspan"],
        });

        schedule();
    }

    function sortValue(cell, type) {
        const raw = (cell?.dataset.sortValue ?? cell?.innerText ?? "").trim();
        if (!raw) return null;

        if (type === "number") {
            const value = Number(raw.replace(/[\s\u00a0]/g, "").replace(",", "."));
            return Number.isFinite(value) ? value : null;
        }

        if (type === "date") {
            const polish = raw.match(/^(\d{2})\.(\d{2})\.(\d{4})$/);
            const normalized = polish
                ? `${polish[3]}-${polish[2]}-${polish[1]}`
                : raw;
            const value = Date.parse(normalized);
            return Number.isFinite(value) ? value : null;
        }

        return raw;
    }

    function initializeSorting(table) {
        if (table.dataset.sortReady === "true") return;
        const body = table.tBodies[0];
        if (!body || table.dataset.serverSort === "true") return;
        table.dataset.sortReady = "true";

        const originalOrder = new WeakMap();
        let nextOrder = 0;
        let activeColumn = null;
        let direction = "none";

        const rememberRows = (rows) => rows.forEach((row) => {
            if (!originalOrder.has(row)) originalOrder.set(row, nextOrder++);
        });
        rememberRows([...body.rows]);

        table.addEventListener("click", (event) => {
            const button = elementFrom(event)?.closest("[data-sort-column]");
            if (!button || button.closest("table") !== table) return;
            const column = Number(button.dataset.sortColumn);
            if (!Number.isInteger(column) || column < 0) return;

            const rows = [...body.rows];
            if (rows.some((row) =>
                [...row.cells].some((cell) => cell.colSpan > 1 || cell.rowSpan > 1)
            )) return;

            event.preventDefault();
            rememberRows(rows);
            direction = activeColumn !== column ? "ascending"
                : direction === "ascending" ? "descending"
                    : direction === "descending" ? "none" : "ascending";
            activeColumn = direction === "none" ? null : column;

            table.querySelectorAll("thead th").forEach((header) => {
                header.removeAttribute("aria-sort");
                const indicator = header.querySelector(".sort-indicator");
                if (indicator) indicator.textContent = "";
            });

            if (direction === "none") {
                rows.sort((a, b) => originalOrder.get(a) - originalOrder.get(b));
            } else {
                button.closest("th")?.setAttribute("aria-sort", direction);
                const indicator = button.querySelector(".sort-indicator");
                if (indicator) indicator.textContent =
                    direction === "ascending" ? "▲" : "▼";

                const type = button.dataset.sortType || "text";
                rows.sort((a, b) => {
                    const first = sortValue(a.cells[column], type);
                    const second = sortValue(b.cells[column], type);
                    if (first === null && second !== null) return 1;
                    if (second === null && first !== null) return -1;

                    const comparison = first === null ? 0
                        : typeof first === "number" ? first - second
                            : collator.compare(first, second);
                    return (direction === "ascending" ? comparison : -comparison)
                        || originalOrder.get(a) - originalOrder.get(b);
                });
            }

            const fragment = document.createDocumentFragment();
            rows.forEach((row) => fragment.append(row));
            body.append(fragment);
        });
    }

    function formSnapshot(form) {
        return JSON.stringify([...new FormData(form)].filter(
            ([name]) => name !== "csrfmiddlewaretoken"
        ).map(([name, value]) => [
            name,
            value instanceof File
                ? [value.name, value.size, value.lastModified]
                : value,
        ]));
    }

    function draftFields(form) {
        return [...form.elements].filter((field) =>
            field.name &&
            !field.disabled &&
            field.matches("input, textarea, select") &&
            !["hidden", "password", "file", "submit", "button", "reset"]
                .includes(field.type) &&
            !field.closest("[data-no-autosave]")
        );
    }

    function initializeFormSafety(form) {
        if (initialized.has(form)) return;
        initialized.add(form);

        const baseline = formSnapshot(form);
        const key = form.dataset.autosave
            ? scopedKey("draft", form.dataset.autosave)
            : null;

        if (key && !form.querySelector(".errorlist, .field-error, .form-error")) {
            try {
                const draft = JSON.parse(storage.get(key) || "null");
                if (draft?.baseline === baseline && Array.isArray(draft.fields)) {
                    const fields = draftFields(form);
                    draft.fields.forEach((saved, index) => {
                        const field = fields[index];
                        if (!field || field.name !== saved.name ||
                            field.type !== saved.type) return;

                        if (["checkbox", "radio"].includes(field.type)) {
                            field.checked = saved.checked === true;
                        } else if (field instanceof HTMLSelectElement && field.multiple) {
                            const values = new Set(saved.values || []);
                            [...field.options].forEach((option) => {
                                option.selected = values.has(option.value);
                            });
                        } else if (typeof saved.value === "string") {
                            field.value = saved.value;
                        }
                    });
                }
            } catch {
                storage.remove(key);
            }
        }

        trackedForms.set(form, baseline);
        let timer;

        const saveDraft = () => {
            if (!key) return;
            const fields = draftFields(form).map((field) => ({
                name: field.name,
                type: field.type,
                value: field.value,
                checked: field.checked,
                values: field instanceof HTMLSelectElement
                    ? [...field.selectedOptions].map((option) => option.value)
                    : undefined,
            }));
            storage.set(key, JSON.stringify({ baseline, fields }));
        };

        form.addEventListener("input", () => {
            clearTimeout(timer);
            timer = setTimeout(saveDraft, 250);
        });
        form.addEventListener("change", saveDraft);

        form.addEventListener("submit", (event) => {
            queueMicrotask(() => {
                if (event.defaultPrevented) return;
                clearTimeout(timer);
                saveDraft();

                // Nie usuwamy szkicu przed odpowiedzią serwera.
                // Zmienione dane serwera unieważnią jego baseline.
                trackedForms.set(form, formSnapshot(form));
            });
        });
    }

    function initializeRememberedFilters(form) {
        if (initialized.has(form)) return;
        initialized.add(form);
        const key = scopedKey("filters", form.dataset.rememberFilters);
        if (!key) return;

        const names = new Set([...form.elements].map((field) => field.name));
        names.delete("csrfmiddlewaretoken");
        names.delete("page");
        names.delete("");

        const sanitize = (parameters) => {
            const result = new URLSearchParams();
            for (const [name, value] of parameters) {
                if (names.has(name)) result.append(name, value);
            }
            return result;
        };

        const saved = storage.get(key, true);
        if (location.search) storage.set(key, sanitize(new URLSearchParams(location.search)).toString(), true);
        if (!location.search && saved) {
            const restored = sanitize(new URLSearchParams(saved));
            if (restored.toString()) {
                const url = new URL(location.href);
                url.search = restored.toString();
                location.replace(url.href);
                return;
            }
        }

        form.addEventListener("submit", (event) => {
            queueMicrotask(() => {
                if (event.defaultPrevented) return;
                const params = new URLSearchParams();
                for (const [name, value] of new FormData(form)) {
                    if (typeof value === "string") params.append(name, value);
                }
                storage.set(key, sanitize(params).toString(), true);
            });
        });
    }

    function initializeBulkActions(form) {
        if (initialized.has(form)) return;
        initialized.add(form);

        const items = () => [...form.elements].filter((field) =>
            field.matches?.("[data-select-item]") && !field.disabled
        );
        const masters = () => [...form.elements].filter((field) =>
            field.matches?.("[data-select-all]")
        );

        const update = () => {
            const available = items();
            const checked = available.filter((field) => field.checked).length;
            masters().forEach((master) => {
                master.checked = available.length > 0 && checked === available.length;
                master.indeterminate = checked > 0 && checked < available.length;
            });
            available.forEach((field) => {
                field.closest("tr")?.classList.toggle("is-selected", field.checked);
            });
            form.querySelectorAll("[data-selected-count]").forEach((node) => {
                node.textContent = String(checked);
            });
        };

        document.addEventListener("change", (event) => {
            const field = elementFrom(event);
            if (field?.form !== form) return;

            if (field.matches("[data-select-all]")) {
                items().forEach((item) => { item.checked = field.checked; });
            }
            if (field.matches("[data-select-all], [data-select-item]")) update();
        });

        form.addEventListener("submit", (event) => {
            if (!items().some((field) => field.checked)) {
                event.preventDefault();
                announce("Zaznacz przynajmniej jeden rekord.", true);
                items()[0]?.focus();
            }
        });

        update();
    }

    function initializeCheckboxDropdown(dropdown) {
        if (initialized.has(dropdown)) return;
        initialized.add(dropdown);
        const inputs = [...dropdown.querySelectorAll('input[type="checkbox"]')];
        const summary = dropdown.querySelector('[data-checkbox-summary]');
        const update = () => {
            const count = inputs.filter(input => input.checked).length;
            summary.textContent = count ? `Wybrano: ${count}` : (dropdown.dataset.emptyLabel || 'Wszystkie etapy');
        };
        dropdown.addEventListener('change', update);
        dropdown.querySelector('[data-checkbox-clear]')?.addEventListener('click', () => {
            inputs.forEach(input => { input.checked = false; });
            update();
        });
        dropdown.addEventListener('keydown', event => {
            if (event.key === 'Escape') {
                dropdown.open = false;
                dropdown.querySelector('summary').focus();
            }
        });
        document.addEventListener('click', event => {
            if (!dropdown.contains(event.target)) dropdown.open = false;
        });
        update();
    }

    function initializeElements(root = document) {
        const each = (selector, callback) => {
            if (root instanceof Element && root.matches(selector)) callback(root);
            root.querySelectorAll?.(selector).forEach(callback);
        };

        each(".table-container", initializeScrollableTable);
        each("table[data-sortable-table]", initializeSorting);
        each("form[data-bulk-form]", initializeBulkActions);
        each("form[data-remember-filters]", initializeRememberedFilters);
        each("[data-checkbox-dropdown]", initializeCheckboxDropdown);
        each("form[data-autosave], form[data-warn-unsaved]", initializeFormSafety);
    }

    function initialize() {
        document.addEventListener('cms:form-saved', event => {
            const form = event.detail?.form;
            if (form && trackedForms.has(form)) trackedForms.set(form, formSnapshot(form));
        });
        initializeNavigation();
        initializeClipboard();

        initializeElements();

        document.addEventListener("change", (event) => {
            const field = elementFrom(event);
            if (!field?.matches("[data-auto-submit]") || !field.form) return;

            const page = field.form.elements.namedItem("page");
            if (page instanceof HTMLInputElement) page.value = "1";
            field.form.requestSubmit();
        });

        document.addEventListener("click", (event) => {
            const target = elementFrom(event);
            const clear = target?.closest("[data-clear-saved-filters]");
            if (clear) {
                const key = scopedKey("filters", clear.dataset.clearSavedFilters);
                if (key) storage.remove(key, true);
            }

            const reset = target?.closest("[data-discard-draft]");
            if (reset) {
                const form = reset.closest("form");
                if (!form) return;
                event.preventDefault();
                const key = scopedKey("draft", form.dataset.autosave);
                if (key) storage.remove(key, true);
                form.reset();
                trackedForms.set(form, formSnapshot(form));
            }
        });

        window.addEventListener("beforeunload", (event) => {
            const changed = [...trackedForms].some(([form, baseline]) =>
                form.isConnected && formSnapshot(form) !== baseline
            );
            if (!changed) return;
            event.preventDefault();
            event.returnValue = "";
        });

        // Delegacja kopiowania obejmuje również tabele dodane dynamicznie.
        // Pozostałe ulepszenia inicjalizujemy tylko dla nowych elementów.
        new MutationObserver((mutations) => {
            for (const mutation of mutations) {
                for (const node of mutation.addedNodes) {
                    if (node instanceof Element) initializeElements(node);
                }
            }
            for (const form of trackedForms.keys()) {
                if (!form.isConnected) trackedForms.delete(form);
            }
        }).observe(document.body, { childList: true, subtree: true });
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", initialize, { once: true });
    } else {
        initialize();
    }
})();

(() => {
    const initialize = () => {
        const fold = value => value.normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/ł/g, "l").replace(/Ł/g, "L").toLowerCase();
        document.querySelectorAll("select[data-searchable-select]").forEach(select => {
            const search = document.createElement("input");
            search.type = "search";
            search.className = "searchable-select-input";
            search.placeholder = select.dataset.searchableSelect;
            search.setAttribute("aria-label", select.dataset.searchableSelect);
            select.before(search);
            const options = [...select.options];
            let chosen = select.value;
            select.addEventListener("change", () => { chosen = select.value; });
            search.addEventListener("input", () => {
                const query = fold(search.value);
                const matches = options.filter(option => !option.value || option.value === chosen || fold(option.textContent).includes(query));
                select.replaceChildren(...matches);
                select.value = chosen;
            });
        });
        document.querySelectorAll("[data-confirm-import]").forEach(button => {
            const form = button.form;
            ["records", "anthology"].forEach(name => {
                const field = form.elements.namedItem(name);
                ["input", "change"].forEach(type => field?.addEventListener(type, () => {
                    button.disabled = true;
                    const token = form.elements.namedItem("preview_token");
                    if (token) token.value = "";
                }));
            });
        });
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize, {once:true});
    else initialize();
})();
