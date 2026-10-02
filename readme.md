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
sufiks `_odkurzony.docx`. Wszystkie 25 reguł jest domyślnie zaznaczonych;
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

Odkurzacz: reguła tabulatorów usuwa znaki tabulacji bez dodawania spacji.
Usunięto normalizację dat i godzin, konwersję separatora dziesiętnego,
grupowanie liczb i reguły wstawiające spacje nierozdzielające. Odstępy przy
inicjałach i temperaturach są zwykłymi spacjami.
