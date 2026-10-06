"""Stable, public error messages; never include uploaded document content."""
MESSAGES = {
    'analysis_limit': 'Dokument przekracza limit analizy. Podziel go na mniejsze części.',
    'tracked_changes': 'Zaakceptuj albo odrzuć śledzone zmiany w dokumencie przed analizą.',
    'unsupported_run': 'Dokument zawiera nieobsługiwaną strukturę tekstu. Zapisz nową kopię DOCX lub wyłącz oznaczanie powtórzeń.',
    'analysis_options': 'Parametry analizy przekraczają dozwolony zakres. Sprawdź ustawienia powtórzeń.',
}


class DocumentInputError(ValueError):
    def __init__(self, code):
        self.public_code = code
        super().__init__(MESSAGES[code])
