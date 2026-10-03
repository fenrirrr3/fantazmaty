# Fantazmaty CMS

Aplikacja Django do obsługi autorów, zgłoszeń, recenzji, procesu
redakcyjnego, antologii, ilustracji, ekstraktów i zespołu.

Interfejs obsługuje jasny i ciemny motyw.

## Baza danych i zależności

Główna konfiguracja aplikacji wymaga MySQL. Nie obsługuje przełączania
na SQLite ani PostgreSQL.

Migracje zawierają operacje przeznaczone dla MySQL 8 oraz kolację
utf8mb4_0900_as_ci.

Zależności aplikacji znajdują się w requirements.txt.
Narzędzia do testów i prac deweloperskich — w requirements-dev.txt.

W requirements.txt zadeklarowano Django 5.2.17. Lokalne testy regresji
można uruchamiać na izolowanej bazie SQLite z ustawieniami
fantazmaty.test_settings. Testy ograniczeń i równoczesnych zapisów
wymagają MySQL; konfigurację zawiera fantazmaty.mysql_integration_settings.

Dobierz interpreter Pythona zgodny z instalowaną wersją Django.
Nie kopiuj środowiska wirtualnego z innego komputera.

## Przygotowanie środowiska

Polecenia wykonuj w katalogu zawierającym manage.py.

Windows — PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Jeżeli masz już działające środowisko projektu, używaj jego interpretera.
Instalacja mysqlclient może wymagać systemowych bibliotek i narzędzi
kompilacyjnych właściwych dla używanego systemu.

## Konfiguracja

Właściwe ustawienia aplikacji znajdują się w fantazmaty/settings.py.

Aplikacja automatycznie wczytuje plik .env z katalogu projektu.
Istniejące zmienne środowiskowe mają pierwszeństwo przed plikiem .env.

Przykład konfiguracji lokalnej:

```dotenv
DJANGO_ENV=development
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

DJANGO_SECRET_KEY=WPISZ_WLASNY_LOSOWY_KLUCZ

DJANGO_DB_ENGINE=mysql
DJANGO_DB_NAME=fantazmaty
DJANGO_DB_USER=fantazmaty
DJANGO_DB_PASSWORD=WPISZ_HASLO_BAZY
DJANGO_DB_HOST=127.0.0.1
DJANGO_DB_PORT=3306
```

Wygeneruj własny klucz:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Nie publikuj pliku .env ani danych dostępowych.

Język aplikacji: polski.
Strefa czasowa: Europe/Warsaw.

## Pierwsze uruchomienie na nowej bazie

Ta sekcja dotyczy pierwszej instalacji. Istniejącą bazę aktualizuj
poleceniem migrate, zachowując dane i wszystkie pliki migracji.

Po utworzeniu bazy i skonfigurowaniu .env:

```bash
python manage.py check --database default
python manage.py migrate
python manage.py createsuperuser
python manage.py collectstatic --noinput
python manage.py runserver
```

Adresy lokalne:

- CMS: http://127.0.0.1:8000/
- Logowanie: http://127.0.0.1:8000/konto/logowanie/
- Panel administracyjny: http://127.0.0.1:8000/panel/
- Reset hasła: http://127.0.0.1:8000/konto/reset-hasla/

runserver służy do pracy lokalnej.

## Aktualizacja plików

1. Zatrzymaj proces aplikacji.
2. Skopiuj pliki z paczki, zachowując strukturę katalogów.
3. Wykonaj:

```bash
python manage.py check --database default
python manage.py migrate
python manage.py collectstatic --noinput
```

4. Uruchom ponownie aplikację.
5. Odśwież stronę z pominięciem pamięci podręcznej, np. Ctrl+F5.

Paczki z 2 października aktualizują istniejącą bazę. Migracje
0011–0013 porządkują przypisania; nie usuwaj historii migracji.
Po ich zastosowaniu audyt workflow i CSS nie wymaga kolejnej migracji.

Pliki statyczne zbierane są do katalogu staticfiles.
Na produkcji ten katalog powinien być obsługiwany przez serwer WWW.

## Konta i uprawnienia

Dostęp do CMS-u wymaga aktywnego konta i aktywnego profilu zespołu;
superuser ma osobny dostęp administracyjny.

Role zespołu określają dostęp do poszczególnych operacji.
Sam status is_staff umożliwia wejście do admina, ale nie zastępuje
kontroli uprawnień do danych i czynności.

Stylowanie może przejąć i otrzymać wyłącznie superuser.

„Moje recenzje” są dostępne dla aktywnych recenzentów.
Dane autorów zgłoszeń podlegają osobnym ograniczeniom dostępu.

## Rekrutacja

W projekcie jest jeden model rekrutacji: core.Recruitment.
Nie ma drugiego modelu ani proxy people.Rekrutacja.

Pola formularza:

- Imię
- Nazwisko
- E-mail
- Dział
- Data nadesłania
- Status: Czeka na ocenę, Przyjęte, Odrzucone
- Czy powiadomiono
- Uwagi
- Uwagi nieoficjalne

Data powiadomienia zapisuje się automatycznie przy zaznaczeniu
„Czy powiadomiono”. Kolejne edycje nie zmieniają tej daty.
Odznaczenie pola czyści datę; ponowne zaznaczenie zapisuje nowy czas.

Model ma również techniczny identyfikator i znacznik updated_at,
używany do wykrywania równoczesnych edycji.

## Role zespołu

Filtr ról ma kolejność:

Recenzent, Redaktor, Korektor, Weryfikator, Korektor poskładowy,
Grafik, Dźwiękowiec, Lektor, Składacz, Tłumacz, Koordynator.

Inne istniejące role są wyświetlane na końcu.
Wystarczy dopasowanie jednej z wybranych ról.

Migracja dodaje rolę „Składacz”, jeśli jeszcze nie istnieje.
Sama kolejność filtra nie tworzy pozostałych brakujących ról.

Role „Koordynator redakcji”, „Koordynator audiobooków”,
„Koordynator weryfikacji”, „Koordynator ilustracji”,
„Koordynator recenzji”, „Koordynator korekty” i
„Koordynator rekrutacji” mają takie same uprawnienia w CMS-ie jak
ogólna rola „Koordynator”. Migracja tworzy te role automatycznie.

## Import archiwum zespołu i prac

Dostępne polecenie importuje przygotowany JSON ze schematem w
workflow/management/commands/import_team_archive.py. Podgląd jest
domyślny i wycofuje wszystkie zapisy; zapis wymaga --commit:

```bash
python manage.py import_team_archive SCIEZKA_DO_ARCHIWUM.json
python manage.py import_team_archive SCIEZKA_DO_ARCHIWUM.json --commit
```

Konflikt wycofuje całość. Nowe konta i profile są nieaktywne, bez
używalnego hasła; import nie nadaje ról zespołu ani uprawnień superusera.

## Import zgłoszeń i umowy

Import zgłoszeń wymaga poprawnego podglądu przed zatwierdzeniem.
Zmiana rekordów lub antologii wymaga ponownego sprawdzenia.
Ostrzeżenia dotyczące duplikatów i czarnej listy wymagają potwierdzenia.

Jeżeli rozpoznany autor ma już potwierdzoną umowę, nie trzeba potwierdzać
jej ponownie przy przenoszeniu przyjętego zgłoszenia do procesu.
Nadal wymagane jest odnotowanie powiadomienia autora.

Odnotowanie powiadomienia w CMS-ie samo w sobie nie wysyła wiadomości.

## Kopiowanie tabel

- Dwuklik kopiuje pojedynczą komórkę.
- Ctrl/Command + klik zaznacza lub odznacza komórki.
- Shift + klik zaznacza prostokątny zakres.
- Ctrl/Command + C kopiuje zaznaczenie.
- Escape usuwa zaznaczenie.

Wklejanie do arkusza zachowuje podział na wiersze i kolumny.

## Testy

Zainstaluj narzędzia:

```bash
python -m pip install -r requirements-dev.txt
```

Uruchom:

```bash
python manage.py check --database default
python manage.py makemigrations --check --dry-run
python manage.py test
```

Testy wymagają osobnej bazy testowej i odpowiednich uprawnień użytkownika
MySQL do jej utworzenia.

Przy poprawkach uruchamiaj testy zmienianego obszaru. Zakres weryfikacji
audytu z 2 października opisuje AUDYT_20261002.md. Zadania CI obejmują
Python 3.12/3.13 oraz osobny MySQL 8.

## Produkcja

Ustaw DJANGO_ENV=production, wyłącz debugowanie i skonfiguruj dozwolone
hosty, HTTPS, serwowanie plików statycznych oraz pocztę zgodnie
z wymaganiami fantazmaty/settings.py.

Nie używaj runserver jako serwera produkcyjnego.
Resetowanie hasła przez e-mail wymaga działającej konfiguracji poczty.


## Daty etapów i przekazanie tekstu w panelu admina

W edycji tekstu, w tabeli workflow, kliknij „Ustaw daty / zakończ etap”.
Ten sam link jest dostępny na liście etapów i w szczegółach etapu.
Formularz pozwala ręcznie ustawić datę rozpoczęcia (również wcześniejszą)
oraz poprawić daty zakończonej pracy. Przed otwarciem formularza zapisz
ewentualne zmiany wykonawców w tabeli tekstu.

Aby zakończyć bieżący etap, podaj obie daty i zaznacz „Zakończ etap
i przekaż tekst dalej”. Dla redakcji wybierz etap docelowy; obowiązują
dotychczasowe warunki weryfikacji. Zakończenie pracy autora wznawia
redakcję, a zakończenie etapu powtórzenia udostępnia następny etap kolejki.
Korekta dat już zakończonej pracy zachowuje wykonawcę i numer wykonania.
Nieznane daty pracy importowanej mogą pozostać puste.

Zmiany nie wymagają nowych migracji bazy ani nowych zależności.

## Odkurzacz (Programy)

Jasna paleta kolorowania powtórzeń losuje składowe RGB w zakresie
160–220, aby ograniczyć bardzo jasne kolory na białych stronach dokumentu.

Strona `/programy/` zastępuje dotychczasowy obrazek formularzem korekty DOCX.
Dostęp mają zalogowani, aktywni członkowie zespołu oraz superużytkownicy.
Wybierz DOCX i reguły, następnie kliknij „Odkurz i pobierz DOCX”. Wynik ma
sufiks `_odkurzony.docx`. Dostępne są 32 reguły, z których 29 jest domyślnie zaznaczonych;
odznaczenie wszystkich nie zmienia tekstu. Korekta obejmuje główne akapity,
bez tabel, nagłówków i przypisów, bez kolorowania i śledzenia zmian.

Po aktualizacji zainstaluj zależności i zbierz pliki statyczne:

```bash
python -m pip install -r requirements.txt
python manage.py collectstatic --noinput
```

Następnie uruchom ponownie proces aplikacji zgodnie z konfiguracją hostingu.
Ta funkcja nie wymaga nowych migracji ani spaCy. Jedyna nowa zależność
aplikacji to `python-docx` (wraz z jej zależnościami). Wynik powstaje w pamięci,
bez tworzenia trwałego rekordu lub publicznego pliku w katalogu media.

Limity: 10 MB pliku wejściowego, 50 MB zawartości ZIP, 2000 elementów ZIP,
500 000 znaków głównego tekstu i 20 000 znaków w pojedynczym akapicie.
Przetwarzanie jest synchroniczne; przy długich tekstach czas zależy od serwera.
Zachowano reguły korekty z dostarczonego programu, w tym wbudowane zamiany
słownikowe. Wynik wymaga przeglądu redakcyjnego.

Testy funkcji:

```bash
python manage.py test core.test_odkurzacz --settings=fantazmaty.test_settings
```

Odkurzacz: reguła tabulatorów zamienia tabulatory i ich mieszanki ze spacjami
na jedną spację. Dostępne są reguły normalizacji jednoznacznych dat i godzin,
separatora dziesiętnego, grupowania liczb i spacji nierozdzielających.
Domyślnie wyłączone są redukcja wielokrotnych pustych akapitów, zamiana
zaimków na małe litery oraz własne zamiany słownikowe. Lista i kolejność
reguł znajdują się w `core/services/odkurzacz.py`.

## Pliki statyczne i lokalne pliki robocze

Źródła CSS i JavaScript znajdują się w katalogach `static` aplikacji.
`staticfiles` jest katalogiem wynikowym i pozostaje w `.gitignore`.
Po wdrożeniu kodu uruchom `python manage.py collectstatic --noinput`
w docelowej konfiguracji środowiska. W produkcji powstają również pliki
z hashem i `staticfiles.json`; stary katalog z archiwum nie zastępuje tego kroku.
Gdy usuwasz zasoby, przygotuj nowy katalog wynikowy lub wyczyść nieaktywną
kopię przed ponownym zebraniem. Nie czyść zasobów obsługujących ruch w trakcie
budowania następnego wydania.

Pliki `.env`, `__pycache__`, lokalny bufor aktywności i blokada konwersji
nie są częścią kodu do publikacji. Nie kasuj `.env` ani danych runtime przy
porządkowaniu repozytorium. Sam wpis w `.gitignore` nie usuwa pliku,
który był wcześniej śledzony przez Git.

## Kolejka aktywności

`python manage.py flush_activity --limit 10000` przenosi wpisy prywatnego
bufora do bazy. Konfiguracja harmonogramu należy do środowiska wdrożenia:
sprawdź, czy polecenie jest wykonywane regularnie i czy monitorowany jest
wiek oraz rozmiar kolejki, błędy przetwarzania i katalog `quarantine`.
Przejściowe błędy bazy powinny powodować ponowienie, nie usuwanie plików kolejki.

Retencję można najpierw obejrzeć bez usuwania danych:
`python manage.py prune_activity --days 180 --dry-run`.
Uruchomienie bez `--dry-run` usuwa stare udane odwiedziny według reguł
polecenia; harmonogram i okres retencji trzeba dopasować do zasad utrzymania.
Ta aktualizacja nie zmienia istniejącego harmonogramu ani nie usuwa dziennika.


### Wycofane automatyczne scalanie przydziałów (2026-10-03)

Skrypt `scal_przypisania.py`, polecenie `merge_workflow_assignments` i funkcja
`merge_duplicates` zostały usunięte.
Migracja `workflow.0013_merge_duplicate_role_assignments` pozostaje jako pusty
znacznik historii: nie scala danych przy nowej instalacji ani aktualizacji.
Nie usuwaj tego pliku ani wpisu migracji z bazy. Nie jest potrzebne `--fake`.
Już wykonane scalenia nie są odwracane i wymagają osobnego odtworzenia danych,
jeśli ich historia ma zostać przywrócona.

Przy nakładaniu paczki usuń dwa stare pliki: `scal_przypisania.py` oraz
`workflow/management/commands/merge_workflow_assignments.py` skryptem porządkowania.
Ręczna korekta wykonawcy w panelu administratora pozostaje dostępna i zachowuje
swoje dotychczasowe grupowanie przydziałów; wspólny moduł `assignment_merge.py`
jest nadal używany przez tę funkcję.

### Decyzja koordynatora redakcji i rzeczywiste przestoje (2026-10-03, v3)

Przy kończeniu Kontroli K. redakcji trzeba jawnie wybrać: przekazanie do
pierwszej korekty albo dalszą redakcję. Dalsza redakcja tworzy kolejny etap
dla przypisanego redaktora, po którym ponownie potrzebna jest kontrola.
Decyzje pozostają widoczne w historii. Reguła obejmuje też powtórzenia etapów.

Praca redaktora jest zakończona po zatwierdzonej kontroli i przekazaniu do
pierwszej korekty. Tekst wraca do jego pracy na etapie Kontroli redaktora,
a nie podczas wcześniejszej Kontroli K. korekty.
Druga weryfikacja wymaga wznowienia redakcji po pierwszej; wyjątek dotyczy
historycznej, zaimportowanej pierwszej weryfikacji bez daty zakończenia.
Nie uzupełniamy brakujących etapów tekstów oznaczonych jako Gotowy.

Raport przestojów liczy dostępność do pracy, a nie czas wcześniejszej
rezerwacji. Przed przekazaniem tekstu rzeczywiste oczekiwanie wynosi zero
i nie kwalifikuje się jako przestój. Zmiana wykonawcy zeruje licznik
otwartego etapu, zachowując historyczną datę rozpoczęcia. Gdy dostępności
nie można ustalić z danych, raport pokazuje brak danych.
Ręczna korekta wykonawców w panelu administratora pozostaje dostępna.

Wdrożenie v3 po zastosowaniu v2:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
```

Następnie uruchom ponownie proces aplikacji. Migracja
`workflow.0014_editorial_decision_and_waiting_reset` dodaje wyłącznie dwa
opcjonalne pola, bez uzupełniania lub przekształcania dawnych etapów.
Paczka v3 nie wymaga usuwania żadnych dodatkowych plików.

### Spójność danych: wielokrotne przypisania (v4)

W sekcji System → Spójność danych zakładkę wyszukiwania duplikatów tekstów
zastępuje lista „Wielokrotne przypisania osób”. Wiersz reprezentuje parę
tekst–konto z co najmniej dwoma rekordami przypisań. Raport rozróżnia tę samą
rolę w kilku przypisaniach (w tym wykonania 1 i 2) oraz różne role tej samej
osoby. Pokazuje wszystkie przypisania danej pary, ich ID, role, numery wykonań,
przebiegi, bieżący lub historyczny charakter i anulowane powtórzenia.

Domyślny zakres obejmuje wszystkie przebiegi i wykonania, również tekstów
Gotowych oraz nieaktywnych kont. Dostępne są filtry antologii, bieżącego
przebiegu (z zachowaniem wcześniejszych wykonań) i rodzaju przypisania.
Kilka etapów powiązanych z jednym rekordem przypisania liczymy jeden raz.
Różnych kont o tych samych nazwiskach nie łączymy. Rekordy bez wykonawcy
nie tworzą wspólnej osoby. Lista jest stronicowana i dostępna superuserowi.

To lista do kontroli, bez automatycznego scalania lub zmian w danych.
Wynik może oznaczać zamierzoną pracę w kilku rolach albo powtórzenie.
Usunięcie wyszukiwania duplikatów w tym raporcie nie zmienia ostrzeżeń przy
przyjmowaniu i imporcie zgłoszeń. Stary adres zakładki otwiera nową listę.

Wdrożenie na zastosowaną v3: nadpisz pliki paczki i uruchom ponownie proces
aplikacji. Brak nowych migracji, zależności, zmienionych zasobów statycznych
i dodatkowych plików do usunięcia.
