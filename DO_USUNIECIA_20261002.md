# Elementy oznaczone do usunięcia — audyt 2 października 2026

Lista jest oznaczeniem do porządkowania. Paczka nie usuwa tych plików ani danych.
Przy funkcjach i selektorach usuwaj tylko wskazany fragment, nie cały moduł.

## Potwierdzone puste pozostałości

| Plik | Uzasadnienie | Zalecenie |
| --- | --- | --- |
| `core/audit.py` | Zawiera tylko informację, że dawny dziennik i sygnały usunięto. Brak odwołań projektu do modułu. | Do usunięcia ze źródeł. |
| `core/templates/core/change_log.html` | Zawiera tylko komentarz o usunięciu widoku. Brak render/include/extends i trasy dziennika. | Do usunięcia ze źródeł. |
| `zastosuj_poprawki.py` | Jednorazowy skrypt starszego wdrożenia; wszystkie sześć wymienionych przez niego plików już nie istnieją w dostarczonym projekcie. | Do usunięcia z bieżącego projektu po zakończeniu starego wdrożenia. |

## Materiały poprzedniego wdrożenia

| Plik | Uzasadnienie | Zalecenie |
| --- | --- | --- |
| `PLIKI.json` | Manifest poprzedniej paczki; jego hashe nie opisują obecnego projektu. | Zarchiwizować poza katalogiem wdrożenia lub usunąć jako stary manifest. Nowa paczka ma `PLIKI_AUDYT_20261002.json`. |
| `WDROZENIE.txt` | Instrukcja z 1 października, odwołująca się do starego skryptu i starszego zestawu migracji/testów. | Zarchiwizować. Aktualne instrukcje są w raporcie i `POPRAWKA_PRZYPISAN_20261002.txt`. |
| `ZMIANY.txt` | Historia poprzedniego audytu. Nie jest używana w działaniu aplikacji. | Zarchiwizować, jeśli chcesz zachować opis zmian; zbędna w paczce runtime. |

## Osierocone funkcje — kandydaci po sprawdzeniu integracji zewnętrznych

W dostarczonym projekcie nie znaleziono wywołań poniższych funkcji. Publiczne
funkcje mogą jednak być używane przez osobne skrypty spoza archiwum.

| Moduł | Fragment | Działanie |
| --- | --- | --- |
| `core/exports.py` | `export_texts_csv` | Brak widoku lub wywołania. Nieużywany import w `core/views/texts.py` już usunięto. Funkcja do usunięcia po potwierdzeniu braku zewnętrznych odbiorców. Zachować pozostałe eksporty CSV. |
| `core/permissions.py` | `can_view_review_author`, `can_manage_authors` | Nieużywane pomocniki kompatybilności. Do usunięcia po kontroli osobnych integracji; nie zastępują aktywnych funkcji uprawnień. |
| `core/services/document_repetitions.py` | `extract_word_tokens`, `_fill_lemmas`, `_mark_paragraph` | Pozostałości wcześniejszego sposobu przetwarzania; brak wywołań w aktualnej ścieżce. Kandydaci do usunięcia z testami konwersji. Cały moduł jest aktywny i musi pozostać. |

## Selektory poprzedniego interfejsu

Poniższe rodziny występują w `core/static/core/styles.css`, lecz nie znaleziono
ich użycia w szablonach ani JavaScripcie dostarczonego projektu:

- `.author-dialog`, `.author-dialog-header`, `.author-dialog-form`,
  `.author-dialog-actions`, `.author-dialog-button`, `.dialog-close-button`;
- `.dropdown-menu`, `.dropdown-toggle`, `.dropdown-link`;
- `.copy-review-panel`, `.copy-review-form`, `.copy-review-success`;
- `.compact-details-grid`, `.compact-details-card`.

Oznaczone do usunięcia po wizualnym sprawdzeniu właściwych ekranów. Jeśli
nieużywany selektor jest częścią wspólnej listy przycisków, usuń tylko ten
selektor; zachowaj regułę używaną przez pozostałe komponenty. Przy
`.dropdown-link` uwzględnij też regułę motywu w `themes.css`.

## Zachować mimo wskazań automatycznej analizy

- `core/templatetags/workspace_tags.py`: ładowany dynamicznie przez cztery
  szablony. Brak zwykłego importu Python nie oznacza martwego kodu.
- Wszystkie migracje: potrzebne do odtworzenia i aktualizacji istniejącej bazy.
- Puste `__init__.py`: oznaczają pakiety; nie są zbędnymi modułami.
- Metody admina, formularzy, konwerterów URL, backendu logowania, sygnały,
  polecenia management i bootstrap konwertera: wywoływane przez framework
  lub subprocess, również bez bezpośredniego wywołania w kodzie.
- Selektory `.reviewer-opinion-*`, palety etapów i klasy tworzone przez JS:
  część jest składana dynamicznie. Nie usuwać na podstawie samego wyszukiwania.
- Historia etapów, przydziały jawnych powtórzeń i uczestników przekazań:
  zapis wykonanej pracy, nie martwy kod ani duplikaty do scalenia.
- `var/conversion/conversion.lock`: plik runtime blokujący konwersję. Nie
  umieszczać w paczce źródłowej i nie usuwać w trakcie działającej konwersji.
