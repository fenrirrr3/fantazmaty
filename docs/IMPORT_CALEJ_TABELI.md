# Import całej tabeli historycznej

Źródło: Wklejony kod markdown(20260912-102004).md.
Plik: import_data/teksty_historyczne.json (UTF-8). Nazwy pól odpowiadają modelom;
relacje mają postać zagnieżdżonych obiektów i list.
135 tekstów, 15 antologii, 103 zapisy autorów, 149 osób i 1239 historycznych udziałów.
Liczba nowo utworzonych osób zależy od zawartości docelowej bazy.

## Uruchomienie

Skopiuj zawartość katalogu fantazmaty z paczki do projektu zawierającego manage.py.
W tym katalogu, w środowisku aplikacji, wykonaj:

```bash
python manage.py migrate
python manage.py import_historical_texts import_data/teksty_historyczne.json
```

Drugie polecenie to podgląd z pełną walidacją i wycofaniem zapisu.
Po sprawdzeniu podsumowania uruchom zapis:

```bash
python manage.py import_historical_texts import_data/teksty_historyczne.json --commit
```

Zrestartuj aplikację po wdrożeniu kodu. Baza nie jest czyszczona. Błąd dowolnego
rekordu wycofuje całą transakcję. Ponowny identyczny import nie dubluje rekordów.
Zmiana zapisanego tekstu, historii lub workflow powoduje odmowę nadpisania.

## Tworzone dane

Text otrzymuje tytuł, długość, antologię, autorów, uwagi, is_historical=true oraz
historical_source i historical_source_row (LP). LP nie jest PK docelowej bazy.

Autorzy mają osobne rekordy także dla tekstów wieloautorskich. Brakujące osoby są
tworzone jako Person bez kont User, grup i uprawnień. Nowe profile mają is_active=false,
ponieważ tabela nie potwierdza aktualnego członkostwa. Są widoczne w historii tekstu,
raportach i szczególe osoby, ale nie automatycznie na liście aktywnego zespołu.
Istniejącym osobom nie zmieniamy aktywności, ról, kont ani prawdziwych e-maili.

Wszystkie udziały trafiają do HistoricalTextAssignment, także pierwszy redaktor,
korektorzy i weryfikatorzy. Nie tworzymy aktywnych WorkflowRoleAssignment.
Numer roli (position) i numer uczestnika tej roli (participant) są rozdzielone:
Korektor 3 · osoba 2 nie staje się Korektorem 4.

Każdy tekst otrzymuje jeden terminalny WorkflowStage: 127 ready (Gotowe), 8 withdrawn
(WYCOFANY), bez dat, is_completed=false, cykl 1. To stan końcowy tekstu, nie fikcyjnie
zakończony etap pracy. Historia udziałów is_completed=true pochodzi z kolumny
„is completed”; nie oznacza is_completed=true terminalnego etapu.
Historyczne udziały pozostają poza aktywnymi zadaniami, rezerwacjami, przestojami
oraz sortowaniem etapów po datach. Są tylko do odczytu w interfejsie.

## Brak e-maili

W najnowszej tabeli wszystkie e-maile są puste. Migracje pozwalają na NULL w
Author.email i Person.email. Nie generujemy fikcyjnych adresów. Unikalność prawdziwych
adresów pozostaje wymagana. Zwykłe formularze nadal wymagają e-maila; brak dopuszcza
importer historii. Znane adresy dopasowanych osób pozostają bez zmian.
Nowi autorzy bez e-maila mają contact=false; istniejące umowy pozostają zachowane.
LP 194 ustawia is_blacklisted=true i contact=false zgodnie z notatką źródłową.
Brak kontaktu z uwag LP 216 i 222 również zapisano jako contact=false.
Przy nowym zgłoszeniu autora historycznego formularz poprosi o uzupełnienie adresu.

## Dopasowanie

Osoby i autorzy są dopasowywani po pełnym imieniu i nazwisku z normalizacją Unicode,
spacji i wielkości liter, z zachowaniem diakrytyków. Kilka istniejących dopasowań
powoduje błąd z listą ID. Wtedy dopisz pole id do właściwego obiektu person lub authors
w JSON. Można tak również jawnie wskazać osobę ze zmienionym nazwiskiem lub aliasem.
Nie dopasowujemy automatycznie po samym nazwisku ani podobieństwie. Inny zapis pełnego
imienia i nazwiska bez id będzie uznany za inną osobę — sprawdź znane aliasy przed zapisem.
Istniejący tekst o tej samej antologii i tytule, bez identyfikatora tego importu,
również powoduje błąd zamiast cichego utworzenia duplikatu lub nadpisania.

## Porządkowanie źródła

Szczegóły również w import_data/uwagi_przygotowania.json:

- Usunięto Markdown i rozdzielono osoby z wieloosobowych komórek.
- Naprawiono „Anna Dwojnych-Kajkowska-Kajkowska” przy LP 144; oryginał zachowano
  w person_name.
- Wszystkie 135 rekordów oznaczono historycznie, również LP 194 z brakującą flagą.
  Zduplikowaną starą kolumnę flag pominięto.
- Nie scalono automatycznie Joanna Gajzler i Joanna Winifreda Gajzler.
- Długości przepisano z najnowszej tabeli bez przeliczania; wszystkie są dodatnie.
- Nie zmieniano statusów istniejących antologii. Nowe mają wartości domyślne modelu;
  gotowy tekst nie oznacza automatycznie wydanej antologii.

## Testy

137 testów zaliczonych, 1 zależny od MySQL pominięty. SQLite: cały zestaw 135 tekstów
oraz 1239 udziałów, podgląd, zapis, powtórny import, dopasowanie istniejących osób,
rollback całości po błędzie, ochrona istniejących danych, puste adresy, widoki
oraz aktywny workflow. Kontrole Django i migracji bez błędów.
Import nie został uruchomiony na bazie użytkownika.
