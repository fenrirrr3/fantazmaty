# Aktualizacja Fantazmatów — 14 września 2026

Paczka zawiera tylko nowe i zmienione pliki względem `fantazmaty(2).rar` przekazanego w tej rozmowie. Rozpakuj ją w katalogu projektu, obok `manage.py`, zachowując podkatalogi. Nie usuwaj wcześniejszych migracji. Nie wykonuj `flush`.

## Wdrożenie na PythonAnywhere

W konsoli Bash przejdź do projektu. Użyj tego samego środowiska wirtualnego, które jest ustawione w zakładce **Web → Virtualenv**. Jeżeli nie masz jeszcze środowiska, utwórz je dla wersji Pythona używanej przez aplikację. Dla Twojego dotychczasowego Pythona 3.13:

```bash
cd /home/Fenrirrr3/fantazmaty
python3.13 -m venv /home/Fenrirrr3/.virtualenvs/fantazmaty
source /home/Fenrirrr3/.virtualenvs/fantazmaty/bin/activate
python -m pip install -r requirements.txt
python manage.py check_runtime
python manage.py migrate
python manage.py check
python manage.py collectstatic --noinput
```

Jeżeli środowisko już istnieje pod inną nazwą, aktywuj jego rzeczywistą ścieżkę zamiast tworzyć drugie. W **Web → Virtualenv** ustaw tę samą ścieżkę, a następnie kliknij **Reload**. Sama instalacja pakietów w konsoli nie zmienia środowiska aplikacji WWW.

`check_runtime` musi pokazać **Django 5.2.17**. Wersja jest przypięta w `requirements.txt`, a `requirements-dev.txt` korzysta z tego samego pliku. To wydanie LTS; informacje o linii i wsparciu: [oficjalna strona Django](https://www.djangoproject.com/download/). Django w innej wersji powoduje ostrzeżenie podczas zwykłego `check`, a `check_runtime` kończy się błędem.

Przed migracją zachowaj kopię bazy i aktualnego kodu. Paczka nie zawiera bazy, kont, haseł ani lokalnej konfiguracji serwera. Nie zmieniałem ustawień ani pakietów na Twoim serwerze.

## Co się zmienia

- Autor ma własne pole **numer telefonu** w adminie i podglądzie. Wybór autora w formularzu recenzji uzupełnia telefon z profilu autora. Starsze zgłoszenia nie są już źródłem numeru. Nowe pole początkowo pozostaje puste — uzupełnij je w profilach; migracja nie zgaduje właściwego numeru.
- Antologie mają dodatkowy status **Gotowe**. Lista uwag i wybór antologii do zgłoszenia uwagi obejmują tylko **Gotowe / Wydane**. Dawne uwagi do pozostałych antologii pozostają w bazie i adminie.
- Usunięto wskazane kolumny w tekstach autora, moich tekstach i antologiach. W tekstach do wzięcia nagłówek brzmi **Akcja**.
- Kończenie etapu ma małe pole z nazwą etapu i przyciskiem pod nią.
- W globalnym wyszukiwaniu e-maile autorów widzą koordynatorzy oraz osoby przypisane do tekstów danego autora, także w historii. Gdy autor jest również członkiem zespołu, jego e-mail w tych wynikach podlega temu samemu ograniczeniu. U zwykłych członków globalne wyszukiwanie zespołu dopasowuje nazwę, bez wyszukiwania po mailu. Lista Zespół zachowuje dotychczasową dostępność kontaktów zespołowych.
- Logo logowania jest lokalnym plikiem, pobranym ze wskazanego adresu. Ma białe podłoże również w ciemnym motywie.
- Pole szukania uwag korzysta ze wspólnego wyglądu filtrów.
- Moje recenzje i Moje teksty na małych ekranach zmieniają wiersze w karty. Formularz recenzji dostosowuje szerokość pól i przycisku do telefonu.

## Nowe podglądy

**Spójność danych** — na dole menu, wyłącznie dla superusera. Pokazuje rozpoczęte etapy bez wykonawcy, bieżące przydziały nieaktywnych osób, aktywne konta bez profilu zespołu, różne e-maile powiązanego konta i osoby oraz teksty bez autora. Oddzielna zakładka pokazuje możliwe duplikaty tekstów i zgłoszeń. Są to ostrzeżenia z linkami; panel niczego nie naprawia.

**Antologia** — kliknięcie tytułu otwiera jej podgląd. Koordynator widzi listę kontrolną gotowości tekstów, umów, uwag, okładki i zapisanych zadań produkcyjnych. Wszyscy członkowie zespołu widzą zestawienie ukończonych prac do stopki oraz możliwość pobrania CSV. Uwzględniane są także historyczne przypisania z potwierdzonym wykonaniem, recenzje przyjętych tekstów, dostarczone ilustracje i gotowe zadania produkcyjne. Brak dat nie blokuje uwzględnienia potwierdzonej pracy. Lista wymaga sprawdzenia przed publikacją — nie zastępuje uzupełnienia brakujących przypisań.

**Uprawnienia osoby** — link w podglądzie członka zespołu, tylko dla superusera. Pokazuje konto, powiązania, aktywność, role, grupy, flagę koordynatora, skuteczny dostęp oraz dostępne rodzaje prac i liczbę aktualnie możliwych przejęć. Nie loguje na cudze konto.

**Aktualny plik lub folder** — stałe miejsce w podglądzie tekstu. Link mogą zmieniać koordynatorzy i wykonawcy przypisani w bieżącym cyklu. Obsługiwane są adresy HTTP/HTTPS. To zwykły odnośnik, bez API Dropboxa; dostęp do docelowego pliku nadal ustala jego właściciel.

Duplikaty: porównywane są podobne tytuły tego samego autora w tej samej antologii, również między tekstami i zgłoszeniami. Przekazanie przyjętego zgłoszenia do jego własnego tekstu nie jest duplikatem. Ostrzeżenia są też częścią dodawania recenzji i podglądu importu. Nie ma automatycznego łączenia. Istniejące ostrzeżenia o identycznych wcześniejszych zgłoszeniach pozostają.

## Tożsamość i import

Konto służy do logowania, **Person** opisuje członka zespołu, **Author** autora i jego pseudonim. `Person.user` pozostaje trwałym powiązaniem z kontem. Nowe opcjonalne `Person.author_profile` łączy członka zespołu z autorem po ID. Ustaw je świadomie w adminie albo przez import z ID autora. Migracja nie łączy automatycznie podobnych nazwisk, pseudonimów ani e-maili. Pseudonim nadal jest danymi autora, a nie osobnym kontem. Link nazwy konta w menu opiera się teraz wyłącznie na rzeczywistym powiązaniu z profilem.

Import zespołu akceptuje dotychczasowe cztery nagłówki oraz opcjonalne kolumny **ID osoby**, **ID konta**, **ID autora**. Przy zmianie e-maila powiązanego konta podaj **ID osoby**. Zostaną zachowane konto, hasło, przypisania i dotychczasowy login; aktualizuje się adres kontaktowy, a nie login. Sprzeczne identyfikatory i adres należący do innego konta blokują zapis. Role są dodawane do istniejących; nie są automatycznie usuwane.

Eksport zespołu z identyfikatorami:

```bash
python manage.py export_team_members > zespol-z-id.tsv
```

Podgląd i zapis zespołu:

```bash
python manage.py import_team_members zespol-z-id.tsv --report podglad-zespolu.json
python manage.py import_team_members zespol-z-id.tsv --commit --report zapis-zespolu.json
```

Podgląd wykonuje walidację i wycofuje zmiany. Raport zawiera liczby dodanych/zmienionych rekordów, dodawane role i konflikt. Jeśli wystąpi błąd, cały import jest wycofywany; raport obejmuje część sprawdzoną do momentu przerwania, a nie nieprzeczytane rekordy. Przed zatwierdzeniem sprawdź raport, zwłaszcza zmiany ról i znacznik Superuser.

`--report sciezka.json` dodano również do `import_historical_texts`, `import_historical_reviews`, `import_historical_assignments` i `import_extracts`. Pierwsze trzy zapisują z `--commit`; ekstrakty zachowują `--apply` i dotychczasowe `--update-existing`. Zachowują dotychczasowe formaty źródłowe.

Znaczenie pustych wartości:

| Import / pole | Puste lub pominięte | Jawne usunięcie |
|---|---|---|
| Zespół / E-mail Dropbox | zachowuje istniejący adres; przy kilku wierszach osoby przejmuje podaną niepustą wartość | `__CLEAR__` |
| Historyczne teksty / autora `phone_number`, `pseudonym` | zachowuje istniejącą wartość | `__CLEAR__` |
| Historyczne teksty / autora `email` | zachowuje istniejący adres | `__CLEAR__`, wyłącznie ze wskazanym `id` autora |
| Ekstrakty / `phone_number` | zachowuje numer istniejącego udziału | `__CLEAR__` |

Główny e-mail członka zespołu nie może być pusty. ID autora w imporcie zespołu pozostawione puste nie usuwa istniejącego powiązania. Pozostałe pola zachowują wcześniejsze reguły importerów, w szczególności jawne zastępowanie udziału w Ekstraktach przez `--update-existing`. Nie stosuj znacznika `__CLEAR__` w polach, których nie wymieniono powyżej.

Import historycznych tekstów może przyjąć dodatkowe pola autora `phone_number` i `pseudonym`. Zmiana jego e-maila wymaga `id`. Przypisania tekstów do tego autora zachowują to samo ID. Sprzeczne wartości dla jednego autora w tym samym pliku blokują import. Import Ekstraktów zatrzyma się przed utworzeniem autora o identycznym imieniu i nazwisku, ale innym adresie — wtedy trzeba sprawdzić istniejący profil.

Podgląd importu recenzji w CMS podaje liczbę planowanych zgłoszeń, dopasowanych autorów, zgłoszeń bez profilu, błędów i ostrzeżeń. Ten import tworzy zgłoszenia, nie nowe konta i role; pokazuje to wprost. Dane profilu autora nie są nadpisywane pustymi danymi zgłoszenia.

## Testy

Sprawdzono Django **5.2.17**, Python **3.12.14**, SQLite. Nie wykonywałem testów na Twojej bazie MySQL ani na serwerze PythonAnywhere. Interpreter serwera może pozostać 3.13; wersja Django ma być wspólna.

```bash
python manage.py check_runtime
python manage.py test core.test_revision8 core.test_revision7 core.test_revision6 core.test_patch6 core.test_patch7 core.test_requested_changes core.test_workspace_patch people.test_team_import texts.test_extract_import texts.test_historical_reviews_import workflow --settings=fantazmaty.test_settings --noinput
```

Wynik: **150 testów — 146 zaliczonych, 4 pominięte**. Pominięcia dotyczą testów wymagających innych warunków/bazy; nie oznaczają sprawdzenia MySQL. Sprawdzono również zgodność modeli z migracjami.

Pełny odziedziczony zestaw zawiera dodatkowo testy oparte na nieobecnym w archiwum `import_data/teksty_historyczne.json` oraz starszych założeniach o danych demonstracyjnych i widoczności kontaktów. Ich pierwsze uruchomienie wykazało błędy; nie przedstawiam powyższego wyniku jako zaliczenia całego odziedziczonego zestawu. Zaktualizowane testy w paczce dotyczą zachowań zmienianych w tej aktualizacji.
