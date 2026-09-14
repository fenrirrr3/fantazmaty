# Discord — test wysyłania z CMS

Paczka uzupełnia poprzednią aktualizację `fantazmaty-aktualizacja-2026-09-14.zip`. Wgraj pliki z zachowaniem podkatalogów obok `manage.py`. Nie zastępuje całego projektu.

## Konfiguracja kanałów

1. Na swoim serwerze Discord otwórz ustawienia integracji i utwórz webhook. Wybierz jego docelowy kanał tekstowy, ustaw nazwę nadawcy i skopiuj adres webhooka. Potrzebne są uprawnienia do zarządzania webhookami.
2. Dla każdego kolejnego kanału utwórz osobny webhook. Ten test obsługuje zwykłe kanały tekstowe, bez wyboru wątków i kanałów forum.
3. Na PythonAnywhere dopisz w lokalnym pliku `.env` projektu jedną linię, z prawdziwymi adresami zamiast poniższych przykładowych wartości:

```dotenv
DISCORD_WEBHOOKS='{"Testowy":"https://discord.com/api/webhooks/ID_WEBHOOKA/TOKEN_WEBHOOKA","Redakcja":"https://discord.com/api/webhooks/DRUGIE_ID/DRUGI_TOKEN"}'
```

Nazwy „Testowy” i „Redakcja” to etykiety w menu wyboru CMS. Docelowy kanał określa sam webhook skonfigurowany na Discordzie. Usunięcie wpisu z konfiguracji usuwa możliwość wyboru kanału. Nie wklejaj adresu webhooka do repozytorium — zawiera token pozwalający wysyłać wiadomości.

Dozwolone są adresy `https://discord.com/api/webhooks/...` oraz `https://discord.com/api/v10/webhooks/...`, bez dopisywania parametrów. Prawidłowy ID jest liczbą; wartości przykładowe powyżej trzeba zastąpić.

Alternatywnie testy lub własny plik ustawień mogą ustawić `DISCORD_WEBHOOKS` jako słownik Pythona. Ustawienie Django ma pierwszeństwo przed zmienną środowiskową. Nie zapisuj prawdziwych tokenów w wersjonowanych ustawieniach.

## Uruchomienie

W konsoli Bash, po aktywowaniu środowiska aplikacji:

```bash
cd /home/Fenrirrr3/fantazmaty
python manage.py migrate
python manage.py check
```

Następnie kliknij **Reload** w zakładce Web. Nowych bibliotek nie trzeba instalować. Nie wykonuj `flush`.

Zaloguj się jako superuser. Na dole menu pojawi się **Discord — test**, pod adresem `/discord-test/`. Wybierz kanał, wpisz wiadomość i kliknij **Wyślij na Discorda**. Brak konfiguracji wyświetla komunikat zamiast aktywnego formularza.

## Zachowanie

- Dostęp do strony i wysyłki ma wyłącznie aktywny superuser; sam `is_staff` lub rola koordynatora nie wystarcza.
- Wysyłka następuje po kliknięciu przycisku, jako żądanie POST chronione CSRF. Nie jest uruchamiana przez samo otwarcie strony.
- Do 2000 znaków. Wysyłamy zwykłą treść wiadomości; formatowanie obsługuje Discord. Powiadomienia ze wzmianek o osobach, rolach i `@everyone` są wyłączone.
- Adres webhooka nie trafia do HTML. Połączenie jest wykonywane przez serwer, z limitem oczekiwania 10 sekund, bez przekierowań i automatycznych ponowień.
- Ponowne przesłanie tego samego formularza nie wysyła kolejnej wiadomości. Nowe otwarcie formularza umożliwia świadome wysłanie następnej.
- Sukces jest pokazywany dopiero po otrzymaniu potwierdzenia i ID wiadomości od Discorda. Przy zerwanym połączeniu lub błędzie serwera wynik może być nieznany: najpierw sprawdź kanał, zanim wyślesz nową wiadomość.
- Ostatnie 20 prób widać na stronie. W bazie zapisywane są: użytkownik, etykieta kanału, data, stan i ewentualne ID wiadomości. Treści wiadomości i adresy webhooków nie są tam zapisywane. To lista wysyłek, nie przegląd wiadomości z Discorda.
- Jeżeli hosting blokuje połączenia wychodzące do Discorda, formularz pokaże brak potwierdzenia. Sama konfiguracja webhooka nie usuwa ograniczeń hostingu.

## Weryfikacja

```bash
python manage.py test core.test_discord --settings=fantazmaty.test_settings --noinput
```

Testy używają symulowanych odpowiedzi, więc nie publikują nic na Discordzie. Sprawdzają uprawnienia, CSRF, walidację, ukrywanie tokenu, ochronę przed powtórnym POST, poprawny payload, potwierdzenie wysyłki i błędy HTTP/sieci. Prawdziwej wiadomości nie wysłano — webhook nie został jeszcze dostarczony/skonfigurowany.

Sposób wysyłki, limit treści i potwierdzenie `wait=true` są zgodne z [dokumentacją webhooków Discorda](https://docs.discord.com/developers/resources/webhook#execute-webhook).
