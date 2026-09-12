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

W projekcie zadeklarowano Django 6.1.1. Dotychczasowe testy lokalne
przeprowadzono na Django 5.2.17, Pythonie 3.12 i izolowanej bazie SQLite,
z osobnymi ustawieniami testowymi. Nie potwierdza to działania całej
konfiguracji zadeklarowanej w requirements.txt.

Dobierz interpreter Pythona zgodny z instalowaną wersją Django.
Nie kopiuj środowiska wirtualnego z innego komputera.

Dotychczasowy requirements.lock jest nieprawidłowy: zawiera kod testów.
Nie używaj go do instalacji. Można go usunąć; prawidłowy plik blokady
należy wygenerować dopiero po ustaleniu i sprawdzeniu zależności.

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

Ostatnie poprawki rekrutacji przygotowano zgodnie z założeniem,
że baza będzie tworzona od nowa.

Użyj nowej, pustej bazy bez dawnych tabel i historii migracji.
Polecenie flush usuwa dane, ale nie resetuje schematu ani historii
migracji — nie zastępuje utworzenia nowej bazy.

Nie usuwaj plików migracji z projektu.

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

Instrukcja dołączona do konkretnej paczki określa, czy można ją zastosować
do istniejącej bazy. Wersja przebudowująca rekrutację wymaga nowej bazy.

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

## Import aktywnego zespołu

Dane aktywnego zespołu znajdują się w pliku
`import_data/czlonkowie_zespolu.tsv`. Jeden adres z kolumny „E-mail”
oznacza jedną osobę i jedno konto, a kolejne wiersze dodają tej osobie
następne funkcje. Adres Dropboxa nie służy do dopasowywania kont.

Najpierw wykonaj podgląd bez zapisu:

```bash
python manage.py import_team_members
```

Jeżeli podgląd nie zgłasza problemów, zapisz dane:

```bash
python manage.py import_team_members --commit
```

Można również podać własny plik TSV jako pierwszy argument. Import jest
atomowy i powtarzalny: błąd wycofuje całość, ponowne uruchomienie nie
tworzy duplikatów, a istniejące dodatkowe role nie są usuwane.

Nowe konta otrzymują login równy adresowi e-mail i nieużywalne hasło.
Hasło należy nadać indywidualnie:

```bash
python manage.py changepassword ADRES_E_MAIL
```

Wpis „Superuser” nie tworzy roli. Ustawia na powiązanym koncie flagi
`is_staff` oraz `is_superuser`; w dołączonych danych dotyczy to Dawida
Wiktorskiego.

## Import zgłoszeń i umowy

Import zgłoszeń wymaga poprawnego podglądu przed zatwierdzeniem.
Zmiana rekordów lub antologii wymaga ponownego sprawdzenia.
Ostrzeżenia dotyczące duplikatów i czarnej listy wymagają potwierdzenia.

Jeżeli rozpoznany autor ma już potwierdzoną umowę, nie trzeba potwierdzać
jej ponownie przy przenoszeniu przyjętego zgłoszenia do procesu.
Nadal wymagane jest odnotowanie powiadomienia autora.

Odnotowanie powiadomienia w CMS-ie samo w sobie nie wysyła wiadomości.

## Ekstrakty

Plik data/ekstrakty.md zawiera dane do importu.
Zachowaj go, jeśli po utworzeniu nowej bazy chcesz odtworzyć ekstrakty.

Podgląd bez zapisu:

```bash
python manage.py import_extracts data/ekstrakty.md --dry-run
```

Zapis importu:

```bash
python manage.py import_extracts data/ekstrakty.md --apply
```

Opcja --update-existing pozwala zastąpić dane istniejących rekordów.
Używaj jej tylko wtedy, gdy plik ma być źródłem aktualnych danych.

Import ekstraktów nie tworzy zwykłych tekstów, recenzji ani etapów workflow.

## Kopiowanie tabel

- Dwuklik kopiuje pojedynczą komórkę.
- Ctrl/Command + klik zaznacza lub odznacza komórki.
- Shift + klik zaznacza prostokątny zakres.
- Ctrl/Command + C kopiuje zaznaczenie.
- Escape usuwa zaznaczenie.

Wklejanie do arkusza zachowuje podział na wiersze i kolumny.

## Dane demonstracyjne

Najpierw zastosuj migracje.

```bash
python manage.py seed_demo --with-password
python manage.py seed_demo_texts
```

Szczegóły opisują DANE-DEMO.txt i TEKSTY-DEMO.txt.

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

Ostatnia lokalna weryfikacja obejmowała 288 testów; 3 wymagające MySQL
pominięto. Testy wykonano na Django 5.2.17 i SQLite z odrębnymi
ustawieniami testowymi, a nie z główną konfiguracją projektu.

Nie wykonano pełnej kontroli wizualnej w przeglądarce ani testów
na docelowym MySQL użytkownika.

## Produkcja

Ustaw DJANGO_ENV=production, wyłącz debugowanie i skonfiguruj dozwolone
hosty, HTTPS, serwowanie plików statycznych oraz pocztę zgodnie
z wymaganiami fantazmaty/settings.py.

Nie używaj runserver jako serwera produkcyjnego.
Resetowanie hasła przez e-mail wymaga działającej konfiguracji poczty.
