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
 * ODŚWIEŻANIE: dane są pamiętane 2 dni (transient fz_katalog_dane; liczba 172800 to sekundy), potem pierwsze
 * wejście na stronę pobiera je na nowo. Ostatnia poprawna kopia jest w opcji fz_katalog_ostatnie i służy, gdy
 * CMS nie odpowie. Zalogowany administrator wchodzi na stronę z ?fz_odswiez=1, żeby pobrać dane od razu
 * (czyszczone są też cache wtyczek, jeśli są zainstalowane) – zakładka „Szukajka WWW” w CMS-ie ma do tego odnośnik.
 *
 * AKTUALIZACJA SNIPPETU: najpierw dezaktywuj snippet, potem podmień kod, zapisz i aktywuj (inaczej Code
 * Snippets zgłosi „Cannot redeclare function”, bo sprawdza nowy kod przy załadowanym starym).
 */

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

/** Pobiera katalog z CMS-a; przy błędzie zwraca null i zapisuje przyczynę. */
function fz_katalog_pobierz() {
	$klucz = fz_katalog_klucz();
	if ( '' === $klucz ) {
		return fz_katalog_zapisz_blad( 'Brak klucza: ustaw FZ_CMS_KEY w wp-config.php albo opcję fz_cms_key.' );
	}
	$odpowiedz = wp_remote_get( FZ_CMS_KATALOG_URL, array(
		'timeout' => 8,
		'headers' => array( 'X-Site-Key' => $klucz, 'Accept' => 'application/json' ),
	) );
	if ( is_wp_error( $odpowiedz ) ) {
		return fz_katalog_zapisz_blad( 'Połączenie z ' . FZ_CMS_KATALOG_URL . ' nie powiodło się: ' . $odpowiedz->get_error_message() );
	}
	$kod = (int) wp_remote_retrieve_response_code( $odpowiedz );
	$tresc = wp_remote_retrieve_body( $odpowiedz );
	if ( 200 !== $kod ) {
		return fz_katalog_zapisz_blad( 'CMS odpowiedział kodem ' . $kod . ' (' . FZ_CMS_KATALOG_URL . '): ' . mb_substr( wp_strip_all_tags( $tresc ), 0, 200 ) );
	}
	$dane = json_decode( $tresc, true );
	if ( ! is_array( $dane ) || ! isset( $dane['antologie'] ) || ! is_array( $dane['antologie'] ) ) {
		return fz_katalog_zapisz_blad( 'Odpowiedź CMS-a nie jest oczekiwanym JSON-em (początek: ' . mb_substr( wp_strip_all_tags( $tresc ), 0, 120 ) . ').' );
	}
	delete_option( 'fz_katalog_blad' );
	return $dane;
}

/** Dane katalogu: transient (2 dni) → CMS → ostatnia poprawna kopia → null. */
function fz_katalog_dane() {
	static $cache = false;
	if ( false !== $cache ) {
		return $cache;
	}
	// Po ?fz_odswiez=1 (administrator) transient jest pomijany w tym żądaniu, nawet jeśli coś już go wczytało.
	$wymus = ! empty( $GLOBALS['fz_katalog_wymus'] );
	$dane = $wymus ? false : get_transient( 'fz_katalog_dane' );
	if ( ! is_array( $dane ) ) {
		$dane = fz_katalog_pobierz();
		$GLOBALS['fz_katalog_pobrano'] = is_array( $dane );
		if ( is_array( $dane ) ) {
			set_transient( 'fz_katalog_dane', $dane, 172800 ); // 2 dni
			update_option( 'fz_katalog_ostatnie', $dane, false );
		} else {
			$dane = get_option( 'fz_katalog_ostatnie', null );
			$dane = is_array( $dane ) ? $dane : null;
		}
	}
	$cache = $dane;
	return $dane;
}

/** ?fz_odswiez=1 dla zalogowanego administratora: kasuje transient i cache strony. */
function fz_katalog_odswiez() {
	if ( empty( $_GET['fz_odswiez'] ) || ! is_user_logged_in() || ! current_user_can( 'manage_options' ) ) {
		return;
	}
	$GLOBALS['fz_katalog_wymus'] = true;
	delete_transient( 'fz_katalog_dane' );
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
add_action( 'init', 'fz_katalog_odswiez', 1 );

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
	$json = wp_json_encode( array( 'antologie' => $dane['antologie'] ), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES );
	// Zabezpieczenie bloku <script>: „</script” w treści nie może zamknąć bloku.
	$json = str_ireplace( '</script', '<\/script', $json );
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
