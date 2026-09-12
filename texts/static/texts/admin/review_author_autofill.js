(() => {
    "use strict";

    function initializeAuthorAutofill() {
        const authorField = document.getElementById("id_author");
        const firstNameField = document.getElementById("id_author_first_name");
        const lastNameField = document.getElementById("id_author_last_name");
        const emailField = document.getElementById("id_email");

        if (
            !authorField ||
            !firstNameField ||
            !lastNameField ||
            !emailField ||
            authorField.dataset.autofillInitialized === "true"
        ) {
            return;
        }

        const urlTemplate = authorField.dataset.authorDetailsUrl;
        const form = authorField.form;

        if (!urlTemplate || !form) {
            return;
        }

        authorField.dataset.autofillInitialized = "true";

        const identityFields = [
            firstNameField,
            lastNameField,
            emailField,
        ];

        const confirmationField = form.querySelector(
            '[name="confirm_submission_warnings"]'
        );
        const confirmationTokenField = form.querySelector(
            '[name="submission_warnings_token"]'
        );

        const originalReadOnly = new Map(
            identityFields.map((field) => [field, field.readOnly])
        );

        let statusElement = document.getElementById(
            "review-author-autofill-status"
        );

        if (!statusElement) {
            statusElement = document.createElement("div");
            statusElement.id = "review-author-autofill-status";
            statusElement.className = "help";
            statusElement.hidden = true;

            const row = authorField.closest(".form-row");
            const container = authorField.closest(
                ".related-widget-wrapper"
            );

            if (row) {
                row.append(statusElement);
            } else if (container) {
                container.after(statusElement);
            } else {
                authorField.after(statusElement);
            }
        }

        statusElement.setAttribute("role", "status");
        statusElement.setAttribute("aria-live", "polite");
        statusElement.setAttribute("aria-atomic", "true");

        const descriptionIds = new Set(
            (authorField.getAttribute("aria-describedby") || "")
                .split(/\s+/)
                .filter(Boolean)
        );

        descriptionIds.add(statusElement.id);
        authorField.setAttribute(
            "aria-describedby",
            [...descriptionIds].join(" ")
        );

        let selectedAuthorId = authorField.value.trim();
        let requestNumber = 0;
        let controller = null;

        function showStatus(message, kind = "info") {
            statusElement.className =
                kind === "error"
                    ? "errornote review-author-autofill-status"
                    : "help review-author-autofill-status";

            statusElement.dataset.kind = kind;
            statusElement.textContent = message;
            statusElement.hidden = !message;
        }

        function invalidateConfirmation() {
            if (confirmationField) {
                confirmationField.checked = false;
            }

            if (confirmationTokenField) {
                confirmationTokenField.value = "";
            }
        }

        function updateReadOnlyState(authorSelected) {
            for (const field of identityFields) {
                // Wybrany rekord Author jest źródłem tych danych.
                // Serwer również uzupełnia je niezależnie od JavaScript.
                field.readOnly =
                    authorSelected || originalReadOnly.get(field);
            }
        }

        function clearIdentityFields() {
            for (const field of identityFields) {
                field.value = "";
            }
        }

        function buildDetailsUrl(authorId) {
            if (!/^[1-9]\d*$/.test(authorId)) {
                throw new Error("Invalid author identifier");
            }

            const url = new URL(urlTemplate, window.location.href);

            if (
                url.origin !== window.location.origin ||
                !/^https?:$/.test(url.protocol) ||
                !/\/0(?=\/|$)/.test(url.pathname)
            ) {
                throw new Error("Invalid author details URL");
            }

            url.pathname = url.pathname.replace(
                /\/0(?=\/|$)/,
                `/${authorId}`
            );

            return url.href;
        }

        function isValidAuthorData(data) {
            return (
                data !== null &&
                typeof data === "object" &&
                !Array.isArray(data) &&
                typeof data.first_name === "string" &&
                typeof data.last_name === "string" &&
                typeof data.email === "string" &&
                typeof data.is_blacklisted === "boolean"
            );
        }

        async function loadAuthorDetails(authorId, clearExisting = false) {
            const currentRequest = ++requestNumber;

            if (controller) {
                controller.abort();
                controller = null;
            }

            updateReadOnlyState(Boolean(authorId));

            if (clearExisting) {
                clearIdentityFields();
            }

            if (!authorId) {
                showStatus("");
                return;
            }

            showStatus("Pobieranie danych autora…");

            const currentController = new AbortController();
            controller = currentController;

            try {
                const response = await fetch(buildDetailsUrl(authorId), {
                    method: "GET",
                    credentials: "same-origin",
                    cache: "no-store",
                    redirect: "error",
                    signal: currentController.signal,
                    headers: {
                        Accept: "application/json",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                });

                if (!response.ok) {
                    throw new Error("Author details request failed");
                }

                const data = await response.json();

                if (
                    currentRequest !== requestNumber ||
                    authorField.value.trim() !== authorId
                ) {
                    return;
                }

                if (!isValidAuthorData(data)) {
                    throw new Error("Invalid author details response");
                }

                firstNameField.value = data.first_name;
                lastNameField.value = data.last_name;
                emailField.value = data.email;

                // Programowe uzupełnianie nie unieważnia podpisu
                // zwróconego po wcześniejszej walidacji formularza.
                // Zgodność podpisu z aktualnymi danymi sprawdza serwer.
                if (data.is_blacklisted) {
                    showStatus(
                        "Uwaga: autor znajduje się na czarnej liście. " +
                            "Przed zapisem zgłoszenia wymagane będzie " +
                            "potwierdzenie ostrzeżenia.",
                        "warning"
                    );
                } else {
                    showStatus(
                        "Dane uzupełniono z rekordu autora. " +
                            "Możliwe duplikaty zostaną sprawdzone " +
                            "przed zapisem."
                    );
                }
            } catch (error) {
                if (
                    currentRequest !== requestNumber ||
                    currentController.signal.aborted
                ) {
                    return;
                }

                // Nie zapisujemy danych autora ani odpowiedzi w konsoli.
                showStatus(
                    "Nie udało się pobrać danych autora. " +
                        "Sprawdź połączenie i sesję logowania. " +
                        "Aby ponowić próbę, wyczyść wybór i wybierz autora " +
                        "ponownie. Ten komunikat nie potwierdza braku " +
                        "ostrzeżeń; serwer sprawdzi zgłoszenie przy zapisie.",
                    "error"
                );
            } finally {
                if (controller === currentController) {
                    controller = null;
                }
            }
        }

        function handleAuthorChange() {
            const authorId = authorField.value.trim();

            // Django Select2 i natywne zdarzenie mogą zgłosić tę samą zmianę.
            if (authorId === selectedAuthorId) {
                return;
            }

            selectedAuthorId = authorId;
            invalidateConfirmation();
            void loadAuthorDetails(authorId, true);
        }

        authorField.addEventListener("change", handleAuthorChange);

        if (window.django?.jQuery) {
            window.django
                .jQuery(authorField)
                .on("change.reviewAuthorAutofill", handleAuthorChange);
        }

        for (const field of [
            ...identityFields,
            form.querySelector('[name="title"]'),
        ]) {
            if (!field) {
                continue;
            }

            field.addEventListener("input", invalidateConfirmation);
            field.addEventListener("change", invalidateConfirmation);
        }

        window.addEventListener("pagehide", () => {
            if (controller) {
                controller.abort();
            }
        });

        // Sprawdza również autora już wybranego przy otwarciu formularza.
        // Nie czyści pól ani potwierdzenia zwróconego przez serwer.
        void loadAuthorDetails(selectedAuthorId);
    }

    if (document.readyState === "loading") {
        document.addEventListener(
            "DOMContentLoaded",
            initializeAuthorAutofill,
            { once: true }
        );
    } else {
        initializeAuthorAutofill();
    }
})();