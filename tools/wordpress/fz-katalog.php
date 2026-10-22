<?php
/**
 * Katalog opowiadań szukajki z CMS-a – snippet do wtyczki Code Snippets (fantazmaty.pl/czytaj/szukaj/).
 *
 * JAK WKLEIĆ: Code Snippets → Add New → wklej wszystko OD LINII POD `<?php` (Code Snippets sam dodaje
 * otwierający tag; wklejenie `<?php` daje błąd składni). Typ: „Run snippet everywhere”. Zapisz i aktywuj.
 * Snippet jest niezależny od fz-ekipa.php (własne funkcje fz_katalog_*), ale używa tego samego klucza.
 *
 * KONFIGURACJA: adres katalogu jest wpisany niżej (https://cms.fantazmaty.pl/api/strona/katalog/);
 * inny adres można ustawić w wp-config.php: define('FZ_CMS_KATALOG_URL', '…'); – stała ma pierwszeństwo.
 * Klucz (wp-config.php, nad linią „That's all, stop editing”):
 *   define('FZ_CMS_KEY', '<ten sam klucz co SITE_API_KEY w .env CMS-a>');   // wspólny z fz-ekipa.php
 * Bez dostępu do wp-config.php klucz można zapisać w opcji WordPressa (jednorazowo, np. w WP-CLI
 * albo w drugim tymczasowym snippecie): update_option('fz_cms_key', '<klucz>', false);
 * Stała z wp-config.php ma pierwszeństwo przed opcją.
 *
 * SHORTCODE'Y:
 *   [fz_katalog_dane]        – <script type="application/json" id="fzs-data">…</script> z katalogiem
 *                              (dokładnie format, którego oczekuje mechanizm wyszukiwarki; zastępuje blok
 *                              „DANE KATALOGU” wpisywany ręcznie)
 *   [fz_katalog_antologie]   – „27 antologii” (odmiana: 1 antologia / 2–4 antologie / 5+ antologii)
 *   [fz_katalog_opowiadania] – „297 opowiadań do odkrycia” (odmiana opowiadanie/opowiadania/opowiadań)
 *   [fz_katalog_licznik]     – „297 opowiadań · 27 antologii”
 * Liczniki liczą tylko to, co jest w danych (JS strony liczy to samo po wczytaniu, więc bez JS też się zgadza).
 *
 * DIAGNOSTYKA: gdy strona nie ma danych, zalogowany administrator widzi w miejscu shortcode'u czerwoną
 * ramkę z przyczyną (brak klucza, kod odpowiedzi CMS-a, błąd połączenia). Inni widzą tylko komentarz HTML,
 * a mechanizm wyszukiwarki pokazuje swój komunikat „Nie udało się wczytać katalogu”.
 *
 * ODŚWIEŻANIE: ostatnia poprawna kopia danych jest w opcji fz_katalog_ostatnie (razem z ETag-iem w
 * fz_katalog_etag). Co FZ_KATALOG_CO_ILE sekund (600 = 10 minut; stała niżej, można ją też ustawić w
 * wp-config.php) pierwsze wejście na stronę pyta CMS, czy dane się zmieniły (nagłówek If-None-Match): 304 – kopia
 * zostaje, 200 – nowe dane zastępują kopię i czyszczony jest cache tej strony we wtyczkach cache (jak przy
 * ?fz_odswiez=1), błąd – zostaje stara kopia, a kolejna próba jest dopiero po FZ_KATALOG_CO_ILE sekundach (nie
 * przy każdym wejściu; znacznik to transient fz_katalog_sprawdzono). Dane w złym kształcie (np. opowiadanie bez
 * tytułu, przez które mechanizm wyszukiwarki nie wczytałby katalogu) są odrzucane jak błąd i nie zastępują
 * ostatniej poprawnej kopii. Zalogowany administrator wchodzi na stronę z ?fz_odswiez=1, żeby pobrać dane od razu,
 * bez czekania (czyszczone są też cache wtyczek, jeśli są zainstalowane). Zakładka „Szukajka WWW” w CMS-ie ma do
 * tego odnośnik. Jeśli strona jest serwowana z cache HTML (np. WP Fastest Cache), snippet działa dopiero przy
 * budowie strony od nowa – zmiana z CMS-u pojawi się najpóźniej po wygaśnięciu cache HTML (albo od razu po
 * ?fz_odswiez=1).
 *
 * AKTUALIZACJA SNIPPETU: najpierw dezaktywuj snippet, potem podmień kod, zapisz i aktywuj (inaczej Code
 * Snippets zgłosi „Cannot redeclare function”, bo sprawdza nowy kod przy załadowanym starym).
 */

if ( ! defined( 'FZ_KATALOG_CO_ILE' ) ) {
	define( 'FZ_KATALOG_CO_ILE', 600 ); // co ile sekund sprawdzać w CMS-ie, czy dane się zmieniły (600 = 10 minut)
}

if ( ! defined( 'FZ_CMS_KATALOG_URL' ) ) {
	define( 'FZ_CMS_KATALOG_URL', 'https://cms.fantazmaty.pl/api/strona/katalog/' );
}

function fz_katalog_klucz() {
	if ( defined( 'FZ_CMS_KEY' ) && FZ_CMS_KEY ) {
		return FZ_CMS_KEY;
	}
	return (string) get_option( 'fz_cms_key', '' );
}

/** Zapamiętuje przyczynę ostatniego nieudanego pobrania (do diagnostyki przez ?fz_odswiez=1). */
function fz_katalog_zapisz_blad( $tekst ) {
	update_option( 'fz_katalog_blad', array( 'kiedy' => current_time( 'mysql' ), 'tekst' => $tekst ), false );
	return null;
}

/** Czy dane mają kształt, który przyjmie mechanizm wyszukiwarki (te same warunki co w jego JS). */
function fz_katalog_poprawne( $dane ) {
	if ( ! is_array( $dane ) || ! isset( $dane['antologie'] ) || ! is_array( $dane['antologie'] ) ) {
		return false;
	}
	$opowiadania = 0;
	foreach ( $dane['antologie'] as $antologia ) {
		if ( ! is_array( $antologia ) || ! isset( $antologia['nazwa'], $antologia['opowiadania'] ) || ! is_scalar( $antologia['nazwa'] )
			|| '' === trim( (string) $antologia['nazwa'] ) || ! is_array( $antologia['opowiadania'] ) ) {
			return false;
		}
		foreach ( $antologia['opowiadania'] as $opowiadanie ) {
			if ( ! is_array( $opowiadanie ) || ! isset( $opowiadanie['tytul'] ) || ! is_scalar( $opowiadanie['tytul'] )
				|| '' === trim( (string) $opowiadanie['tytul'] ) ) {
				return false;
			}
			$opowiadania++;
		}
	}
	return $opowiadania > 0;
}

/**
 * Pobiera dane z CMS-a. Z $etag wysyła If-None-Match: 304 daje array( 'dane' => null, 'etag' => $etag ) (bez zmian),
 * 200 – array( 'dane' => …, 'etag' => nowy ETag ). Przy błędzie zwraca null i zapisuje przyczynę.
 */
function fz_katalog_pobierz( $etag = '' ) {
	$klucz = fz_katalog_klucz();
	if ( '' === $klucz ) {
		return fz_katalog_zapisz_blad( 'Brak klucza: ustaw FZ_CMS_KEY w wp-config.php albo opcję fz_cms_key.' );
	}
	$naglowki = array( 'X-Site-Key' => $klucz, 'Accept' => 'application/json' );
	if ( '' !== $etag ) {
		$naglowki['If-None-Match'] = $etag;
	}
	$odpowiedz = wp_remote_get( FZ_CMS_KATALOG_URL, array(
		'timeout' => 8,
		'headers' => $naglowki,
	) );
	if ( is_wp_error( $odpowiedz ) ) {
		return fz_katalog_zapisz_blad( 'Połączenie z ' . FZ_CMS_KATALOG_URL . ' nie powiodło się: ' . $odpowiedz->get_error_message() );
	}
	$kod = (int) wp_remote_retrieve_response_code( $odpowiedz );
	if ( 304 === $kod && '' !== $etag ) {
		delete_option( 'fz_katalog_blad' );
		return array( 'dane' => null, 'etag' => $etag );
	}
	$tresc = wp_remote_retrieve_body( $odpowiedz );
	if ( 200 !== $kod ) {
		return fz_katalog_zapisz_blad( 'CMS odpowiedział kodem ' . $kod . ' (' . FZ_CMS_KATALOG_URL . '): ' . mb_substr( wp_strip_all_tags( $tresc ), 0, 200 ) );
	}
	$dane = json_decode( $tresc, true );
	if ( ! fz_katalog_poprawne( $dane ) ) {
		return fz_katalog_zapisz_blad( 'Odpowiedź CMS-a nie jest oczekiwanym JSON-em (początek: ' . mb_substr( wp_strip_all_tags( $tresc ), 0, 120 ) . ').' );
	}
	delete_option( 'fz_katalog_blad' );
	$nowy_etag = wp_remote_retrieve_header( $odpowiedz, 'etag' );
	return array( 'dane' => $dane, 'etag' => is_string( $nowy_etag ) ? $nowy_etag : '' );
}

/**
 * Dane: ostatnia poprawna kopia (opcja fz_katalog_ostatnie), co FZ_KATALOG_CO_ILE sekund sprawdzana w CMS-ie
 * z If-None-Match (304 – bez zmian, 200 – nowa kopia i czyszczenie cache strony, błąd – stara kopia);
 * null, gdy kopii nie ma.
 */
function fz_katalog_dane() {
	static $cache = false;
	if ( false !== $cache ) {
		return $cache;
	}
	// Po ?fz_odswiez=1 (administrator) dane są pobierane w tym żądaniu w całości (bez If-None-Match).
	$wymus = ! empty( $GLOBALS['fz_katalog_wymus'] );
	$dane = get_option( 'fz_katalog_ostatnie', null );
	$dane = fz_katalog_poprawne( $dane ) ? $dane : null;
	if ( $wymus || ! get_transient( 'fz_katalog_sprawdzono' ) ) {
		// Znacznik przed pytaniem CMS-a: równoczesne wejścia nie pytają drugi raz, a po błędzie kolejna próba
		// jest dopiero za FZ_KATALOG_CO_ILE sekund.
		set_transient( 'fz_katalog_sprawdzono', 1, FZ_KATALOG_CO_ILE );
		$wynik = fz_katalog_pobierz( $dane && ! $wymus ? (string) get_option( 'fz_katalog_etag', '' ) : '' );
		$GLOBALS['fz_katalog_pobrano'] = is_array( $wynik );
		if ( is_array( $wynik ) && null !== $wynik['dane'] ) {
			$zmiana = $wynik['dane'] !== $dane;
			$dane = $wynik['dane'];
			update_option( 'fz_katalog_ostatnie', $dane, false );
			update_option( 'fz_katalog_etag', $wynik['etag'], false );
			if ( $zmiana && ! $wymus ) {
				fz_katalog_wyczysc_cache();
				set_transient( 'fz_katalog_sprawdzono', 1, FZ_KATALOG_CO_ILE ); // wp_cache_flush() mógł skasować znacznik
			}
		}
	}
	$cache = $dane;
	return $dane;
}

/** Czyści cache strony z danymi we wtyczkach cache (jeśli są) – po ?fz_odswiez=1 i gdy CMS dał nowe dane. */
function fz_katalog_wyczysc_cache() {
	$id = get_queried_object_id();
	if ( $id ) {
		if ( function_exists( 'rocket_clean_post' ) ) {
			rocket_clean_post( $id );
		}
		if ( function_exists( 'w3tc_flush_post' ) ) {
			w3tc_flush_post( $id );
		}
		if ( function_exists( 'wp_cache_post_change' ) ) {
			wp_cache_post_change( $id );
		}
		do_action( 'litespeed_purge_post', $id );
	}
	// WP Fastest Cache („Delete Cache” w pasku admina) i Autoptimize – czyszczone w całości.
	if ( function_exists( 'wpfc_clear_all_cache' ) ) {
		wpfc_clear_all_cache( true );
	}
	if ( class_exists( 'autoptimizeCache' ) && method_exists( 'autoptimizeCache', 'clearall' ) ) {
		autoptimizeCache::clearall();
	}
	wp_cache_flush();
}

/** ?fz_odswiez=1 dla zalogowanego administratora: pobranie danych od razu i czyszczenie cache strony. */
function fz_katalog_odswiez() {
	if ( empty( $_GET['fz_odswiez'] ) || ! is_user_logged_in() || ! current_user_can( 'manage_options' ) ) {
		return;
	}
	$GLOBALS['fz_katalog_wymus'] = true;
	fz_katalog_wyczysc_cache();
}
add_action( 'wp', 'fz_katalog_odswiez', 1 ); // po rozpoznaniu strony – get_queried_object_id() zna jej identyfikator

/** Bez danych: komentarz HTML; zalogowany administrator widzi przyczynę na stronie. */
function fz_katalog_brak() {
	$blad = get_option( 'fz_katalog_blad', array() );
	$tekst = is_array( $blad ) && ! empty( $blad['tekst'] ) ? $blad['tekst'] : 'brak danych';
	$html = '<!-- fz-katalog: ' . esc_html( $tekst ) . ' -->';
	if ( is_user_logged_in() && current_user_can( 'manage_options' ) ) {
		$html .= '<p class="fz-katalog-blad" style="border:2px solid #b00;padding:.5rem 1rem"><strong>Katalog z CMS-a (widzi tylko administrator):</strong> '
			. esc_html( $tekst ) . ( ! empty( $blad['kiedy'] ) ? ' (' . esc_html( $blad['kiedy'] ) . ')' : '' ) . '</p>';
	}
	return $html;
}

/** Polska odmiana: 1 antologia, 2–4 antologie, 5+ antologii (12–14 jak 5+). */
function fz_katalog_odmiana( $n, $jeden, $kilka, $wiele ) {
	$n = (int) $n;
	if ( 1 === $n ) {
		return $jeden;
	}
	$ost = $n % 10;
	$dwie = $n % 100;
	return ( $ost >= 2 && $ost <= 4 && ( $dwie < 12 || $dwie > 14 ) ) ? $kilka : $wiele;
}

/** Liczby z danych: antologie i opowiadania (z pola `liczby`, jeśli CMS je dał, inaczej policzone). */
function fz_katalog_liczby( $dane ) {
	if ( isset( $dane['liczby']['antologie'], $dane['liczby']['opowiadania'] ) ) {
		return array( (int) $dane['liczby']['antologie'], (int) $dane['liczby']['opowiadania'] );
	}
	$opowiadania = 0;
	foreach ( $dane['antologie'] as $antologia ) {
		$opowiadania += isset( $antologia['opowiadania'] ) ? count( (array) $antologia['opowiadania'] ) : 0;
	}
	return array( count( $dane['antologie'] ), $opowiadania );
}

add_shortcode( 'fz_katalog_dane', function () {
	$dane = fz_katalog_dane();
	if ( ! $dane ) {
		return fz_katalog_brak();
	}
	$html = '';
	// Potwierdzenie dla administratora po ?fz_odswiez=1: skąd są dane i z jakiej wersji CMS-a.
	if ( ! empty( $_GET['fz_odswiez'] ) && is_user_logged_in() && current_user_can( 'manage_options' ) ) {
		$blad = get_option( 'fz_katalog_blad', array() );
		$udalo = ! empty( $GLOBALS['fz_katalog_pobrano'] );
		$html .= '<p class="fz-katalog-info" style="border:2px solid ' . ( $udalo ? '#080' : '#b00' ) . ';padding:.5rem 1rem"><strong>Katalog z CMS-a (widzi tylko administrator):</strong> '
			. ( $udalo ? 'pobrano teraz świeże dane z CMS-a, wersja: ' . esc_html( isset( $dane['wersja'] ) ? $dane['wersja'] : '?' )
				: 'pobranie z CMS-a nie powiodło się' . ( is_array( $blad ) && ! empty( $blad['tekst'] ) ? ' – ' . esc_html( $blad['tekst'] ) : '' ) . '; pokazywana jest ostatnia kopia' )
			. '.</p>';
	}
	// Do strony idzie tylko lista antologii (mechanizm JS i tak ignoruje inne klucze).
	// JSON_HEX_TAG: „<” i „>” jako \u003C/\u003E – ani „</script”, ani „<!--” w treści nie zepsują bloku <script>.
	$json = wp_json_encode( array( 'antologie' => $dane['antologie'] ), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_HEX_TAG );
	return $html . '<script type="application/json" id="fzs-data">' . $json . '</script>';
} );

add_shortcode( 'fz_katalog_antologie', function () {
	$dane = fz_katalog_dane();
	if ( ! $dane ) {
		return fz_katalog_brak();
	}
	list( $antologie ) = fz_katalog_liczby( $dane );
	return $antologie . ' ' . fz_katalog_odmiana( $antologie, 'antologia', 'antologie', 'antologii' );
} );

add_shortcode( 'fz_katalog_opowiadania', function () {
	$dane = fz_katalog_dane();
	if ( ! $dane ) {
		return fz_katalog_brak();
	}
	list( , $opowiadania ) = fz_katalog_liczby( $dane );
	return $opowiadania . ' ' . fz_katalog_odmiana( $opowiadania, 'opowiadanie', 'opowiadania', 'opowiadań' ) . ' do odkrycia';
} );

add_shortcode( 'fz_katalog_licznik', function () {
	$dane = fz_katalog_dane();
	if ( ! $dane ) {
		return fz_katalog_brak();
	}
	list( $antologie, $opowiadania ) = fz_katalog_liczby( $dane );
	return $opowiadania . ' ' . fz_katalog_odmiana( $opowiadania, 'opowiadanie', 'opowiadania', 'opowiadań' )
		. ' · ' . $antologie . ' ' . fz_katalog_odmiana( $antologie, 'antologia', 'antologie', 'antologii' );
} );
