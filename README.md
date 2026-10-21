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
   Scalenie zdublowanego wpisu w spisie „Lektorzy/montażyści” (źródło znika, nagrania i e-mail przechodzą na cel):
   `python manage.py merge_audio_contributor "Karolina Stachacz Bartosik" "Karolina Stachacz-Bartosik" --dry-run`,
   a po sprawdzeniu wyniku to samo bez `--dry-run`. Nazwy muszą być dokładne; przy kilku wpisach o tej samej nazwie
   komenda poda ich ID – wtedy `--source-id`/`--target-id`.
   Import decyzji do naboru Ekstraktów z pliku TSV (UTF-8, tabulatory; nagłówek
   `imię i nazwisko | pseudonim | e-mail | tytuły (rozdzielone |) | decyzja`, decyzja „przyjęte” albo „odrzucone”,
   tytuły rozdzielone `|` – przecinki są częścią tytułu): najpierw podgląd
   `python manage.py import_extracts plik.tsv --recruitment "Nazwa naboru" [--date RRRR-MM-DD]`, a po sprawdzeniu
   to samo z `--apply`. Autor jest dopasowywany po e-mailu, potem po imieniu i nazwisku (imię = pierwsze słowo,
   nazwisko = reszta); jego dane nie są zmieniane. Błąd dowolnego wiersza (np. czarna lista, tytuł z przeciwną
   decyzją) oznacza, że nic nie zostanie zapisane. Plików z danymi autorów nie dodawaj do repozytorium (`import_data/`).
   Import publikacji audiobooków z pliku TSV (UTF-8, tabulatory; nagłówek `antologia | autor | tytuł | data premiery |
   youtube | hearthis | kolejne części | lektor | uwagi`, data DD.MM.RRRR, kolejne części rozdzielone `|`, uwagi pomijane):
   podgląd `python manage.py import_audiobook_publications plik.tsv`, a po sprawdzeniu to samo z `--apply`.
   Tekst jest dopasowywany po tytule antologii i tytule tekstu (kolumna autora rozstrzyga tylko powtórzone tytuły).
   Brakujący audiobook powstaje jako „Opublikowane” bez etapów; istniejącemu komenda tylko uzupełnia puste pola
   (data, linki, lektor ze spisu – nowy wpis w spisie, gdy go nie ma) i dopisuje kolejne części, a różnice wypisuje
   w raporcie bez zmian (`--overwrite-premiere` zastępuje datę premiery). Statusu i etapów nie zmienia. Wpis w dzienniku
   zapisuje konto z `--user adres@e-mail` (domyślnie pierwszy superuser) bez powiadomień Discord. Błąd dowolnego wiersza
   oznacza, że nic nie zostanie zapisane.
   Import ostrzeżeń dotyczących treści z pliku TSV (nagłówek zaczyna się od `antologia | tytuł | tytuł w bazie |
   ostrzeżenia`, dalsze kolumny są pomijane): podgląd `python manage.py import_content_warnings plik.tsv`, a po
   sprawdzeniu to samo z `--apply`. Tekst jest szukany po „tytule w bazie” albo „tytule”; gdy „tytuł” jest inny, staje
   się tytułem tekstu. Komenda uzupełnia tylko puste ostrzeżenia, inne istniejące wypisuje (`--overwrite` je zastępuje).
   Błąd dowolnego wiersza oznacza, że nic nie zostanie zapisane.
   Konta logowania podążają za osobą (e-mail osoby = login i nazwa użytkownika, odznaczenie „wciąż w ekipie”
   blokuje logowanie). Stare rozjazdy wyrównuje jednorazowo `python manage.py sync_accounts` (sam raport),
   a po sprawdzeniu `python manage.py sync_accounts --apply`. Kolizji adresów i osób bez e-maila komenda nie zmienia,
   tylko je wypisuje (popraw je na ekranie osoby i uruchom ponownie); kont bez osoby (np. superuserów) nie rusza.
   Pomocniczy spis zespołu w TSV (wiersz na osobę i rolę; kolumny: nazwisko i imię, e-mail, e-mail Dropbox, funkcja,
   ID osoby, konta i autora) wypisuje `python manage.py export_team_members > zespol.tsv`; z `--include-inactive`
   także dawni członkowie. To tylko raport z identyfikatorami do porównań, nie kopia bazy ani format importu.
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
autorów), `[antologia]`, `[gatunek]`, `[długość]`, `[etap]`, `[zdarzenie]` („Rozpoczęto” albo „Zakończono”), `[dni]`
(przypomnienia), `[osoba]` (przy „Rozpoczęcie i zakończenie etapu” wykonawca etapu, a bez przypisania osoba, która
wykonała operację), `[rola]`, `[link]`. Pusty tag daje „–”.

„Rozpoczęcie i zakończenie etapu (wszystkie etapy)” to jedna reguła dla każdego etapu: ogłasza rozpoczęcie (przejęcie
z rozpoczęciem pracy, rozpoczęcie przypisanego etapu, także przejęcie tekstu do redakcji z „Teksty do wzięcia”
i wznowienie redakcji po weryfikacji) oraz zakończenie. Etap rozpoczęty i zakończony w jednej operacji daje tylko
wiadomość o zakończeniu, a sama rezerwacja pierwszej weryfikacji – żadnej. Reguła nie wysyła wiadomości przy
przekazaniu pliku autorowi ani przy jego powrocie od autora (etap „Plik u autora (redakcja)”), przy „Gotowe”, wycofaniu
ani przy korektach historii w adminie. Każde przekazanie redakcji (do pierwszej i drugiej weryfikacji, do koordynatora,
a w Ekstraktach do W1 albo K1) jest ogłaszane, a `[etap]` wskazuje cel, np. „Redakcja → Pierwsza weryfikacja”.

Pole antologii „Powiadomienia Discord” (strona antologii lub powieści – koordynator – i panel admina): „Włączone”
(domyślnie), „Wyłączone” – żadnych wiadomości o tekstach tej antologii (do wzięcia, rozpoczęcia i zakończenia etapów,
przypomnienia, produkcja audiobooków), „Tylko rozpoczęcia i zakończenia etapów” – wyłącznie ta reguła. Zgłoszeń do
recenzji i testów z panelu to nie dotyczy. `python manage.py discord_diagnose` wypisuje antologie z ograniczeniem.

## Strona WWW (ekipa i szukajka na fantazmaty.pl)

Strona fantazmaty.pl pobiera dane z CMS-a (CMS niczego nie wysyła) dwoma endpointami bez logowania, ale z nagłówkiem
`X-Site-Key` równym zmiennej `SITE_API_KEY` z `.env` (bez niej odpowiadają 503, ze złym kluczem 403; odpowiedź
`Cache-Control: no-store`). Obie zakładki są w menu System (tylko superuser) i pokazują dokładnie to, co dostanie
strona (`?format=json` – sam JSON), stan klucza, datę ostatniego pobrania i ostrzeżenia.

### Ekipa WWW

Strona https://fantazmaty.pl/o-projekcie/ekipa/ pobiera działy ekipy z `GET /api/strona/ekipa/`. Działy, podgrupy
(z ról albo ręczna lista nazwisk), koordynację (role koordynatorów i osoby wpisane ręcznie), kolejność i notki
ustawia się w panelu admina („Ustawienia” → „Strona WWW – działy ekipy”), a redaktora prowadzącego i opiekuna strony
w zakładce System → „Ekipa WWW” (`/system/strona-www/`), która pokazuje też ostrzeżenia (osoby z rolą bez działu,
działy z ról bez osób). JSON ma pola `wersja`, `prowadzacy`, `strona` i `dzialy` (tytuł, kotwica, liczba osób
z odmianą, koordynacja, grupy z nazwiskami albo notka); nigdy e-maile, konta ani role wewnętrzne.

### Szukajka WWW

Wyszukiwarka opowiadań https://fantazmaty.pl/czytaj/szukaj/ pobiera katalog z `GET /api/strona/katalog/`: JSON
`{"wersja", "liczby": {antologie, opowiadania, gatunki, tagi}, "antologie": [{"nazwa", "url", "opowiadania":
[{"tytul", "autor", "gatunki": [...], "tagi": [...]}]}]}` – dokładnie format bloku `#fzs-data`, który dotąd był
wpisywany na stronie ręcznie (mechanizm JS strony pomija nieznane klucze). Katalog (`SiteCatalogAnthology`,
`SiteCatalogStory`, seed z `core/site_catalog_seed.json`: 27 antologii, 297 opowiadań ze strony) edytuje się
w panelu admina („Ustawienia” → „Szukajka WWW – katalog opowiadań”: kolejność, włączenie, adres antologii,
opowiadania z gatunkami i tagami po jednym w wierszu). Wyłączona antologia nie trafia do JSON-a.

Zakładka System → „Szukajka WWW” (`/system/szukajka-www/`) pokazuje liczniki jak na stronie, pozwala ustawić adres
szukajki (odnośnik „Odśwież stronę” z `?fz_odswiez=1`), dodać gotową antologię z CMS-u (select antologii spoza
katalogu, adres strony, podgląd opowiadań → „Dodaj do katalogu”) i dla antologii powiązanych z CMS-em sprawdzić
aktualizacje (`/system/szukajka-www/<id>/aktualizuj/`: nowe opowiadania, brakujące gatunki i tagi, rozbieżne tytuły
i autorzy tylko w raporcie → „Zastosuj”). Aktualizacja dopisuje, nigdy nie usuwa ani nie nadpisuje ręcznie
dopracowanych wpisów. Źródło w CMS-ie: tytuł tekstu, autorzy jak w stopce (pseudonim albo imię i nazwisko, w
tłumaczeniach autor zagraniczny), gatunki z pola „gatunek” (rozdzielone `,`, `;` albo `|`) i tagi; pomijane są
teksty wycofane. Import (dodanie i aktualizacja) działa tylko dla antologii o statusie „Gotowa”, które nie są
Ekstraktami; powieść trafia jako jedna pozycja w całości (tytuł, autorzy, gatunki i tagi z danych powieści,
rozdziały pomijane, bez powiązania z tekstem). Powiązania wpisów katalogu z antologiami i tekstami CMS-u ustala
migracja seedu (`core.site_catalog.link_catalog_to_cms`: tytuł antologii bez wielkości liter i diakrytyków,
rzymskie I–IV jak 1–4; tytuł tekstu w antologii z ujednoliconymi cudzysłowami); niedopasowane wpisy można powiązać
w adminie. Ostrzeżenia zakładki: włączona antologia bez adresu, opowiadania bez gatunku lub tagów, gotowe antologie
CMS-u poza katalogiem, wpisy bez powiązania, powtórzone tytuły.

### Wdrożenie

1. Na PythonAnywhere dopisz `SITE_API_KEY=<długi losowy ciąg>` do `.env` i przeładuj aplikację; w `wp-config.php`
   strony dodaj `define('FZ_CMS_KEY', '<ten sam klucz>');` oraz adresy endpointów:
   `define('FZ_CMS_URL', 'https://<domena-cms>/api/strona/ekipa/');` (`fz-katalog.php` ma już wpisany adres
   `https://cms.fantazmaty.pl/api/strona/katalog/`; inny można nadpisać stałą `FZ_CMS_KATALOG_URL`).
2. Wklej `tools/wordpress/fz-ekipa.php` i `tools/wordpress/fz-katalog.php` (bez pierwszej linii `<?php`) jako
   osobne snippety w Code Snippets i aktywuj.
3. Podmień kod strony „Ekipa” w Avadzie na `tools/wordpress/ekipa-strona.txt` (shortcode'y `[fz_ekipa_nawigacja]`,
   `[fz_ekipa_prowadzacy]`, `[fz_ekipa_dzialy]`, `[fz_ekipa_strona]`); odstępy między działami w jednym
   kontenerze daje kilka linii CSS z komentarza na początku snippetu.
4. Podmień kod strony szukajki na `tools/wordpress/szukajka-strona.txt`: zmienione są tylko liczniki w nagłówku
   (`[fz_katalog_antologie]`, `[fz_katalog_opowiadania]`) i blok „DANE KATALOGU” (`[fz_katalog_dane]` w elemencie
   tekstowym zamiast bloku kodu); wyszukiwarka, filtry, lista wyników, mechanizm JS i CSS strony bez zmian.
   W zakładce „Szukajka WWW” wpisz adres szukajki, żeby mieć odnośnik „Odśwież stronę”.
5. Strona pamięta dane ekipy i katalogu 2 dni; po zmianach w CMS-ie zalogowany administrator wchodzi na stronę
   z `?fz_odswiez=1`. Gdy CMS nie odpowiada, strona pokazuje ostatnią poprawną kopię.

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
