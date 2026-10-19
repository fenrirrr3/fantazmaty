# Fantazmaty CMS

System redakcyjny wydawnictwa Fantazmaty: zgłoszenia i recenzje, teksty i ich
przebieg (redakcja, weryfikacje, korekty, skład), ilustracje, powieści,
tłumaczenia, urlopy zespołu, rekrutacja oraz programy do obróbki dokumentów DOCX.
Aplikacja Django 5.2 (LTS) z bazą MySQL.

## Wymagania

- Python 3.12 lub 3.13
- MySQL 8 (utf8mb4) i biblioteki klienta (`default-libmysqlclient-dev`, `pkg-config` na Linuksie)
- Node.js 22 – tylko do testów JavaScript

## Uruchomienie lokalne

```bash
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                 # uzupełnij SECRET_KEY i dane bazy
python manage.py migrate
python manage.py createsuperuser
python manage.py runserver
```

Wszystkie ustawienia są opisane w `.env.example`. Zmienne środowiska procesu
mają pierwszeństwo przed plikiem `.env`.

## Testy i jakość kodu

```bash
# Testy (SQLite w pamięci, konfiguracja produkcyjna poza bazą):
DJANGO_SETTINGS_MODULE=fantazmaty.test_settings python manage.py test --parallel
# lub: pytest  (pyproject.toml wskazuje fantazmaty.test_settings)

# Lint (konfiguracja w pyproject.toml):
ruff check .

# Testy na MySQL (osobna baza test_*):
TEST_MYSQL_USER=root TEST_MYSQL_PASSWORD=... \
DJANGO_SETTINGS_MODULE=fantazmaty.mysql_integration_settings python manage.py test
```

`fantazmaty/test_settings.py` importuje `fantazmaty/settings.py`, więc testy
działają z tym samym językiem, middleware i nagłówkami co produkcja. Lokalny
`.env` jest wtedy pomijany. CI (`.github/workflows/tests.yml`) uruchamia testy
na SQLite i MySQL, `ruff check`, stylelint oraz `pip-audit`.

## Program lokalny (jeden plik)

`python tools/redaktor_docx/build.py` buduje `redaktor_docx.py` – jeden plik z Odkurzaczem,
konwersją DOCX → PDF/EPUB, kolorowaniem powtórzeń i przebudową DOCX oraz oknem programu
(Tkinter). Kod programów jest brany z `core/services`, więc po zmianach wystarczy zbudować plik
ponownie. Na komputerze potrzebny jest Python 3.12+ z pakietami: `python-docx lxml mammoth
EbookLib fpdf2 Pillow rapidfuzz spacy` i modelem `pl_core_news_sm` (polecenia w nagłówku pliku).

## Wdrożenie (PythonAnywhere)

1. `pip install -r requirements.txt`
2. `python manage.py migrate` i `python manage.py collectstatic --noinput`
3. Zmienne środowiskowe jak w `.env.example`, w tym `DJANGO_ENV=production`
   oraz **`AUTH_THROTTLE_CLIENT_IP_HEADER=HTTP_X_REAL_IP`** – bez tego limit prób
   logowania liczyłby wszystkich użytkowników jako jeden adres load balancera –
   i **`DJANGO_TRUST_PROXY_SSL_HEADER=true`** – PythonAnywhere kończy HTTPS na
   load balancerze; bez tego grozi pętla przekierowań i odrzucane formularze.
4. Zadania cykliczne:
   - `python manage.py flush_activity` – przenosi dziennik aktywności do bazy,
   - `python manage.py prune_activity` – usuwa stare wpisy aktywności,
   - `python manage.py clearsessions` – usuwa wygasłe sesje (raz dziennie).
   - `python manage.py send_discord_reminders` – przypomnienia Discord o tekstach i zgłoszeniach do recenzji, które od 7, 14 i 21 dni
     czekają na osobę (raz dziennie; działa, gdy w panelu włączono „Tekst nadal czeka na osobę”).
   - `python manage.py resend_discord_notifications` – ponawia powiadomienia Discord oczekujące od ponad 10 minut
     albo nieudane, nie starsze niż 3 dni (co kilkanaście minut; stan „brak potwierdzenia” nie jest ponawiany).
   - `python manage.py fetch_mailbox_headers` – raz na dobę pobiera same nagłówki (bez treści i załączników)
     ze skrzynek zgłoszeń i rekrutacji, żeby rano strony „Pobieranie zgłoszeń” i „Pobieranie próbek” od razu
     pokazywały listę. Zadanie w zakładce Tasks: godzinę podaje się w UTC, więc 3:00–4:00 czasu polskiego
     to 2:00 UTC (3:00 zimą, 4:00 latem); wpisanie 3:00 UTC da 4:00 zimą i 5:00 latem. Polecenie:
     `cd /home/Fenrirrr3/fantazmaty && /home/Fenrirrr3/.virtualenvs/fantazmaty-venv/bin/python manage.py fetch_mailbox_headers`
     Kod wyjścia 1 oznacza, że żadna skrzynka nie odpowiedziała; wynik widać też w adminie („Automatycznie pobrane nagłówki”).
   Jednorazowo po wdrożeniu z października 2026: `python manage.py align_stage_executions --dry-run`, a po sprawdzeniu
   bez `--dry-run` – wyrównuje numer wykonania otwartych etapów do numeru przydziału (dawny rozjazd „wyk. 1” / „wyk. 2”).
   Scalenie zdublowanego wpisu w spisie „Lektorzy/montażyści” (źródło znika, nagrania i e-mail przechodzą na cel):
   `python manage.py merge_audio_contributor "Karolina Stachacz Bartosik" "Karolina Stachacz-Bartosik" --dry-run`,
   a po sprawdzeniu wyniku to samo bez `--dry-run`. Nazwy muszą być dokładne; przy kilku wpisach o tej samej nazwie
   komenda poda ich ID – wtedy `--source-id`/`--target-id`.
   Konta logowania podążają za osobą (e-mail osoby = login i nazwa użytkownika, odznaczenie „wciąż w ekipie”
   blokuje logowanie). Stare rozjazdy wyrównuje jednorazowo `python manage.py sync_accounts` (sam raport),
   a po sprawdzeniu `python manage.py sync_accounts --apply`. Kolizji adresów i osób bez e-maila komenda nie zmienia,
   tylko je wypisuje (popraw je na ekranie osoby i uruchom ponownie); kont bez osoby (np. superuserów) nie rusza.
   Powiadomienia Discord wysyła aplikacja na bieżąco. Komendy `dispatch_workflow_notifications`
   już nie ma – usuń to zadanie, jeśli jest zaplanowane.
   Dawnej wysyłki każdej zmiany workflow na jeden kanał (`DISCORD_WORKFLOW_CHANNEL`, `DISCORD_AUDIOBOOK_CHANNEL`)
   też już nie ma – te zmienne można usunąć z `.env`; powiadomienia wysyłają tylko reguły opisane niżej.
5. `python manage.py check --deploy`

## Powiadomienia Discord

Ustawienia: panel admina → „Powiadomienia Discord – ustawienia” (tylko superuser). Każde zdarzenie ma kanał
(nazwa z `DISCORD_WEBHOOKS` – adresy webhooków są tylko w zmiennych środowiskowych), opcjonalne oznaczenie roli
i jedną lub więcej treści. Tagi w treści: `[tytuł]`, `[autor]` (podpis jak w stopce tekstu – pseudonim albo imię
i nazwisko, kilku autorów po przecinku; przy zgłoszeniach do recenzji zawsze „(anonimowo)”, bo recenzenci nie widzą
autorów), `[antologia]`, `[gatunek]`, `[długość]`, `[etap]`, `[dni]` (przypomnienia), `[osoba]` (tylko „Zakończenie
etapu” – wykonawca etapu, a bez przypisania osoba, która zamknęła etap), `[rola]`, `[link]`. Pusty tag daje „–”.

„Zakończenie etapu (wszystkie etapy)” to jedna reguła dla każdego etapu. Nie wysyła wiadomości przy przekazaniu
pliku autorowi ani przy jego powrocie od autora (etap „Plik u autora (redakcja)”), przy przejęciu tekstu do redakcji,
„Gotowe” i wycofaniu. Redakcja jest zgłaszana jako zakończona tylko przy ostatnim przekazaniu – do koordynatora
(Kontrola K. redakcji); przekazania do pierwszej i drugiej weryfikacji nie są ogłaszane.

## Struktura

| Katalog | Zawartość |
| --- | --- |
| `core/` | widoki, formularze, serwisy, szablony i zasoby statyczne aplikacji |
| `texts/` | antologie, teksty, recenzje, tłumaczenia, słownik tagów |
| `workflow/` | etapy pracy nad tekstem, przydziały, powtórzenia |
| `people/` | zespół, role, urlopy |
| `authors/` | autorzy i czarna lista |
| `illustrations/` | ilustracje, ilustratorzy, publiczna lista ilustracji |

Uprawnienia: `core/permissions.py` (role i dekoratory widoków) oraz
`core/edit_policy.py` (kto może zapisać obiekt i czy formularz wymaga tokenu
wersji). Ochrona przed nadpisaniem cudzych zmian: `core/edit_versions.py`.

## Style

Arkusze w `core/static/core/` ładuje wspólny szablon `core/includes/stylesheets.html`
w stałej kolejności warstw:

1. `tokens.css` – skale: odstępy 4/8/12/16/24/32/48 px (`--space-1…7`), rozmiary
   tekstu 12–24 px, interlinia 1,25 (nagłówki) i 1,5 (tekst), zaokrąglenia 4/8/12 px
   i pigułka, wysokość kontrolek 36 px (małe 30 px), trzy cienie, pięć warstw `z-index`.
   Ładuje go także panel administracyjny.
2. `themes.css` – kolory motywów (jasny, ciemny, „Jesieniara”), palety ról, wydruk.
3. `base.css` – reset i wygląd elementów HTML (pola, tabele, okna dialogowe).
4. `layout.css` – nagłówek, menu boczne, obszar strony, stopka.
5. `components.css` – przyciski (główny, zwykły, zatwierdzający, niebezpieczny + mały),
   pola formularzy, odznaki, karty, wiersze przycisków, filtry, tabele, komunikaty.
6. `pagination.css`, `illustration-status.css` – komponenty używane też w panelu.
7. `pages.css` – reguły widoków bez własnego arkusza.
8. Arkusze stron (`novels.css`, `recruitment.css`, …) w bloku `extra_css`.

Wartości spoza skal, `!important` i powtórzone selektory blokuje stylelint
(`.stylelintrc.json`, zadanie `css` w CI):

```bash
npx --yes stylelint@16.10.0 "core/static/core/*.css"
```

Progi szerokości: 480 px (telefon), 760 px (tablet), 950 px (zwinięte menu),
1100 px (wąski ekran). Kolory odznak ról i etapów ustala serwer (`core/palettes.py`,
filtry w `core/templatetags/workspace_tags.py`).
