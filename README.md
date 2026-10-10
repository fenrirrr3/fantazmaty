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

## Wdrożenie (PythonAnywhere)

1. `pip install -r requirements.txt`
2. `python manage.py migrate` i `python manage.py collectstatic --noinput`
3. Zmienne środowiskowe jak w `.env.example`, w tym `DJANGO_ENV=production`
   oraz **`AUTH_THROTTLE_CLIENT_IP_HEADER=HTTP_X_REAL_IP`** – bez tego limit prób
   logowania liczyłby wszystkich użytkowników jako jeden adres load balancera.
4. Zadania cykliczne:
   - `python manage.py flush_activity` – przenosi dziennik aktywności do bazy,
   - `python manage.py prune_activity` – usuwa stare wpisy aktywności,
   - `python manage.py dispatch_workflow_notifications` – ponawia niewysłane powiadomienia Discord.
5. `python manage.py check --deploy`

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
