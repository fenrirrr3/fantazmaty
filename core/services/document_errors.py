"""Stable, public error messages; never include uploaded document content."""
MESSAGES = {
    'cleaner_paragraph_limit': 'Jeden z akapitów przekracza 20 000 znaków. Podziel ten akapit na krótsze. Odkurzacz nie ogranicza łącznej liczby znaków dokumentu.',
    'analysis_limit': 'Dokument przekracza limit analizy. Podziel go na mniejsze części.',
    'tracked_changes': 'Zaakceptuj albo odrzuć śledzone zmiany w dokumencie przed analizą.',
    'unsupported_run': 'Dokument zawiera nieobsługiwaną strukturę tekstu. Zapisz nową kopię DOCX lub wyłącz oznaczanie powtórzeń.',
    'analysis_options': 'Parametry analizy przekraczają dozwolony zakres. Sprawdź ustawienia powtórzeń.',
    'unsupported_image': 'Dokument zawiera obraz w formacie, którego konwerter nie obsługuje (np. EMF/WMF z Worda). '
                         'Zapisz ten obraz jako PNG lub JPG i wstaw go ponownie.',
    'image_too_large': 'Jeden z obrazów jest za duży (ponad 20 MB albo ponad 20 mln pikseli). Zmniejsz go przed konwersją.',
    'too_many_images': 'Obrazy w dokumencie zajmują łącznie za dużo miejsca. Zmniejsz je albo podziel dokument.',
}


class DocumentInputError(ValueError):
    def __init__(self, code):
        self.public_code = code
        super().__init__(MESSAGES[code])
