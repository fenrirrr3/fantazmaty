# Historia udziałów bez dat — wersja 2

Pełny importer przygotowanej tabeli: [IMPORT_CALEJ_TABELI.md](IMPORT_CALEJ_TABELI.md). Poniższe polecenie import_historical_assignments służy wyłącznie pojedynczym udziałom przy istniejących tekstach.

Wersja dostosowana do tabeli Wklejony kod markdown(20260912-102004).md:
135 rekordów, 127 Gotowe, 8 WYCOFANY, bez dat rozpoczęcia i zakończenia.
Dane nie zostały zaimportowane; dostarczamy model, migracje, odczyt i importer
udziałów dla istniejących tekstów i członków zespołu.

## Model

Text.is_historical oznacza tekst mający historię. HistoricalTextAssignment
przechowuje udział, niezależnie od aktywnego WorkflowRoleAssignment.
Pola:

- text: istniejący tekst;
- person: członek zespołu (opcjonalny), person_name: zachowana nazwa źródłowa;
- role: rodzaj roli;
- position: numer roli (np. trzecia korekta);
- participant: numer osoby w tej roli (np. druga osoba przy trzeciej korekcie);
- source_row: LP źródłowe, nigdy automatycznie ID tekstu;
- source_status: ready (Gotowe), withdrawn (WYCOFANY), ewentualnie pusty dla dawnych wpisów;
- is_completed: potwierdzony udział, niezależny od dat i statusu aktywnego workflow;
- notes: informacje źródłowe;
- started_at, ended_at: puste. Pola pozostały dla zgodności poprzedniej migracji.

Unikalność: tekst + rodzaj roli + numer roli + numer osoby.
Istniejące wpisy po migracji 0006 otrzymują participant=1. Nie zmieniamy ich
numeracji ani nie zgadujemy, czy dawny numer oznaczał etap czy wykonawcę.

Przykłady etykiet:

| role | position | participant | Etykieta |
|---|---:|---:|---|
| editor | 2 | 1 | Redaktor 2 |
| verifier | 4 | 1 | Weryfikator 4 |
| proofreader | 3 | 2 | Korektor 3 · osoba 2 |
| verifier | 1 | 2 | Weryfikator 1 · osoba 2 |

Dla nienumerowanej redakcji można stosować kolejne position zgodnie z uzgodnioną
etykietą Redaktor 2. Dla korekt i weryfikacji position zawsze odpowiada kolumnie
źródłowej, a dodatkowe osoby zwiększają participant. Wybrany schemat musi być stały
przy ponawianiu importu.

## Brak dat i bieżący workflow

Importer oraz walidacja modelu odrzucają niepuste daty. W interfejsie daty historii
są zawsze puste (–), także dla ewentualnych starszych wpisów zapisanych wcześniej
z datami. Te stare wartości nie są kasowane przez migrację.

Historia nie tworzy WorkflowStage, WorkflowRoleAssignment ani dat przypisania.
Nie zasila aktywnych zadań, rezerwacji, przestojów i sortowania etapów workflow.
W profilu wyświetla się po zwykłych przypisaniach, w stałym porządku tekst/rola/numer/osoba.
W raportach jest dodawana po zwykłych wynikach, bez dat; każdy filtr dat wyklucza
historyczne udziały. is_completed=True zwiększa licznik zakończonych udziałów
profilu nawet bez dat. Licznik mierzy udziały, nie unikalne teksty.

source_status zapisuje stan źródłowy i pokazuje go przy historycznej pozycji w
profilu. Nie zmienia aktualnego etapu istniejącego tekstu. Przy przyszłym imporcie
samych tekstów stan Gotowe/WYCOFANY trzeba zapisać osobno jako odpowiedni terminalny
etap workflow; nie należy tworzyć fikcyjnie datowanych zakończonych etapów.

W sekcji Osoby przypisane do tekstu historyczna podstawowa rola zastępuje pustą
pozycję standardową. Rzeczywiste aktywne przypisania pozostają bez zmian. Podstawowej,
obsadzonej pozycji nie kopiujemy do historii; dodatkowy participant=2 jest dozwolony.
Historyczne role Weryfikator redakcji są również uwzględniane w raporcie weryfikatorów.

## Mapowanie kolumn tabeli

| Kolumna | role | position |
|---|---|---|
| Redaktor | editor | 1, kolejne osoby 2 itd. |
| Koordynator redakcji | editing_coordinator | 1 |
| Weryfikator redakcji | editing_verifier | 1 |
| Korektor 1–4 | proofreader | numer kolumny |
| Weryfikator 1–4 | verifier | numer kolumny |
| Koordynator weryfikacji | verification_coordinator | 1 |
| Sczytanie | final_reader | 1 |

Dodatkowych wykonawców tej samej numerowanej roli zapisujemy z kolejnym participant.
Wielu koordynatorów i weryfikatorów redakcji również można rozróżniać participant.
Nie wolno automatycznie uznawać pierwszej osoby za główną/ostatnią, zwłaszcza przy
zapisie „przejęte Michał Gola”. Zachowujemy tę informację w notes.

„Gotowe” mapujemy do source_status=ready. Oznaczenie „is completed” do
is_completed=true; brak oznaczenia pozostawia false. Samo wycofanie tekstu nie
przesądza, czy dana osoba wykonała pracę. Znaczniki Markdown ** należy usunąć.

Uwaga do tabeli: zawiera dwie kolumny „historyczny” bez nagłówków. Nowsza ma 134
oznaczenia, stara 72; brak w nowszej dotyczy LP 194. Należy wybrać jedną kolumnę
oraz uzgodnić brakujące oznaczenie przed importem. Importer udziałów oznacza tekst
jako historyczny podczas zapisu; nie czyta bezpośrednio tej tabeli Markdown.

## Import JSON

```json
[
  {
    "text_id": 123,
    "person_id": 45,
    "person_name": "Przykładowa Osoba",
    "role": "proofreader",
    "position": 3,
    "participant": 2,
    "source_row": 103,
    "source_status": "ready",
    "is_completed": true,
    "started_at": null,
    "ended_at": null
  }
]
```

ID są przykładowe. Należy wskazać istniejące rekordy bazy. Brak person_id wymaga
person_name i nie pozwala przypisać udziału do konkretnego profilu.

Podgląd (walidacja całej transakcji, wycofanie zapisu):

```bash
python manage.py import_historical_assignments historia.json
```

Zapis:

```bash
python manage.py import_historical_assignments historia.json --commit
```

Importer nie tworzy autorów, tekstów, kont ani aktywnego workflow. Powtarzalny import
pomija identyczne wpisy; konflikt powoduje błąd bez nadpisania historii. Błąd
rekordu wycofuje cały import. Historia pozostaje tylko do odczytu w CMS-ie i adminie.

## Wdrożenie

Podmień pliki, uruchom `python manage.py migrate`, zrestartuj aplikację.
Paczka jest kumulatywna względem archiwum 58343b30-d780-4001-a3fb-d47b4ea8be25.zip.
Zawiera niezmienioną migrację 0005 oraz nową 0006. Działa również po wdrożeniu
poprzedniej paczki historii; nie usuwaj ani nie uruchamiaj ponownie migracji 0005.

Weryfikacja: 66 testów zaliczonych, 1 zależny od MySQL pominięty; kontrola Django
i zgodności migracji bez błędów. Testy na izolowanej SQLite.
