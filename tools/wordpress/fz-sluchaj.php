<?php
/**
 * Lista audiobooków strony „Słuchaj” z CMS-a – snippet do wtyczki Code Snippets (fantazmaty.pl/sluchaj/).
 *
 * JAK WKLEIĆ: Code Snippets → Add New → wklej wszystko OD LINII POD `<?php` (Code Snippets sam dodaje
 * otwierający tag; wklejenie `<?php` daje błąd składni). Typ: „Run snippet everywhere”. Zapisz i aktywuj.
 * Snippet jest niezależny od fz-ekipa.php i fz-katalog.php (własne funkcje fz_sluchaj_*), ale używa tego samego klucza.
 *
 * KONFIGURACJA: adres listy jest wpisany niżej (https://cms.fantazmaty.pl/api/strona/sluchaj/);
 * inny adres można ustawić w wp-config.php: define('FZ_CMS_SLUCHAJ_URL', '…'); – stała ma pierwszeństwo.
 * Klucz (wp-config.php, nad linią „That's all, stop editing”):
 *   define('FZ_CMS_KEY', '<ten sam klucz co SITE_API_KEY w .env CMS-a>');   // wspólny z fz-ekipa.php
 * Bez dostępu do wp-config.php klucz można zapisać w opcji WordPressa (jednorazowo, np. w WP-CLI
 * albo w drugim tymczasowym snippecie): update_option('fz_cms_key', '<klucz>', false);
 * Stała z wp-config.php ma pierwszeństwo przed opcją.
 *
 * SHORTCODE'Y:
 *   [fz_sluchaj_dane]     – <script type="application/json" id="fza-audio-data">…</script> z nagraniami
 *                           (dokładnie format, którego oczekuje mechanizm strony), a za nim akapit
 *                           #fza-data-message i <noscript> jak dotąd; zastępuje blok „DANE AUDIOBOOKÓW”
 *                           wpisywany ręcznie
 *   [fz_sluchaj_licznik]  – „116 nagrań” (odmiana: 1 nagranie / 2–4 nagrania / 5+ nagrań) – statyczny licznik
 *                           nad listą; mechanizm strony i tak go nadpisuje po wczytaniu
 *
 * DIAGNOSTYKA: gdy strona nie ma danych, zalogowany administrator widzi w miejscu shortcode'u czerwoną
 * ramkę z przyczyną (brak klucza, kod odpowiedzi CMS-a, błąd połączenia). Inni widzą komunikat
 * „Nie udało się wczytać listy audiobooków” z odnośnikami do hearthis.at i YouTube.
 *
 * ODŚWIEŻANIE: dane są pamiętane 2 dni (transient fz_sluchaj_dane; liczba 172800 to sekundy), potem pierwsze
 * wejście na stronę pobiera je na nowo. Ostatnia poprawna kopia jest w opcji fz_sluchaj_ostatnie i służy, gdy
 * CMS nie odpowie (wtedy kolejna próba pobrania najwcześniej po godzinie – liczba 3600). Dane w złym kształcie
 * (np. nagranie bez tytułu albo bez żadnego linku, przez które mechanizm strony nie wczytałby listy) są
 * odrzucane jak błąd i nie zastępują ostatniej poprawnej kopii. Zalogowany administrator wchodzi na stronę
 * z ?fz_odswiez=1, żeby pobrać dane od razu (czyszczone są też cache wtyczek, jeśli są zainstalowane) – zakładka
 * „Słuchaj WWW” w CMS-ie ma do tego odnośnik.
 *
 * AKTUALIZACJA SNIPPETU: najpierw dezaktywuj snippet, potem podmień kod, zapisz i aktywuj (inaczej Code
 * Snippets zgłosi „Cannot redeclare function”, bo sprawdza nowy kod przy załadowanym starym).
 */

if ( ! defined( 'FZ_CMS_SLUCHAJ_URL' ) ) {
	define( 'FZ_CMS_SLUCHAJ_URL', 'https://cms.fantazmaty.pl/api/strona/sluchaj/' );
}

function fz_sluchaj_klucz() {
	if ( defined( 'FZ_CMS_KEY' ) && FZ_CMS_KEY ) {
		return FZ_CMS_KEY;
	}
	return (string) get_option( 'fz_cms_key', '' );
}

/** Zapamiętuje przyczynę ostatniego nieudanego pobrania (do diagnostyki przez ?fz_odswiez=1). */
function fz_sluchaj_zapisz_blad( $tekst ) {
	update_option( 'fz_sluchaj_blad', array( 'kiedy' => current_time( 'mysql' ), 'tekst' => $tekst ), false );
	return null;
}

/** Niepusty napis (jak text() w mechanizmie strony). */
function fz_sluchaj_tekst( $wartosc ) {
	return is_string( $wartosc ) && '' !== trim( $wartosc );
}

/** Adres http(s) z hostem (jak url() w mechanizmie strony). */
function fz_sluchaj_adres( $wartosc ) {
	if ( ! fz_sluchaj_tekst( $wartosc ) ) {
		return false;
	}
	$czesci = wp_parse_url( trim( $wartosc ) );
	return is_array( $czesci ) && isset( $czesci['scheme'], $czesci['host'] )
		&& in_array( strtolower( $czesci['scheme'] ), array( 'http', 'https' ), true ) && '' !== $czesci['host'];
}

/** Pole opcjonalne: jak optional() w mechanizmie strony – tylko niepusty napis się liczy. */
function fz_sluchaj_opcjonalne( $nagranie, $klucz ) {
	return isset( $nagranie[ $klucz ] ) && is_string( $nagranie[ $klucz ] ) ? trim( $nagranie[ $klucz ] ) : '';
}

/** Czy dane mają kształt, który przyjmie mechanizm strony (te same warunki co jego readCatalog()). */
function fz_sluchaj_poprawne( $dane ) {
	if ( ! is_array( $dane ) || ! isset( $dane['antologie'] ) || ! is_array( $dane['antologie'] ) ) {
		return false;
	}
	$antologie = array();
	$id_nagran = array();
	foreach ( $dane['antologie'] as $antologia ) {
		if ( ! is_array( $antologia ) || ! isset( $antologia['id'], $antologia['nazwa'], $antologia['okladka'], $antologia['nagrania'] )
			|| ! fz_sluchaj_tekst( $antologia['id'] ) || ! fz_sluchaj_tekst( $antologia['nazwa'] )
			|| ! fz_sluchaj_adres( $antologia['okladka'] ) || ! is_array( $antologia['nagrania'] )
			|| isset( $antologie[ trim( $antologia['id'] ) ] ) ) {
			return false;
		}
		$antologie[ trim( $antologia['id'] ) ] = true;
		foreach ( $antologia['nagrania'] as $nagranie ) {
			if ( ! is_array( $nagranie ) || ! isset( $nagranie['id'], $nagranie['tytul'], $nagranie['autor'] )
				|| ! fz_sluchaj_tekst( $nagranie['id'] ) || ! fz_sluchaj_tekst( $nagranie['tytul'] ) || ! fz_sluchaj_tekst( $nagranie['autor'] ) ) {
				return false;
			}
			$id = trim( $nagranie['id'] );
			if ( ! preg_match( '/^[a-zA-Z0-9_-]+$/', $id ) || isset( $id_nagran[ $id ] ) ) {
				return false;
			}
			$id_nagran[ $id ] = true;
			$youtube = fz_sluchaj_opcjonalne( $nagranie, 'youtube' );
			$hearthis = fz_sluchaj_opcjonalne( $nagranie, 'hearthis' );
			if ( ( '' === $youtube && '' === $hearthis ) || ( '' !== $youtube && ! fz_sluchaj_adres( $youtube ) )
				|| ( '' !== $hearthis && ! fz_sluchaj_adres( $hearthis ) ) ) {
				return false;
			}
		}
	}
	return count( $id_nagran ) > 0;
}

/** Pobiera listę z CMS-a; przy błędzie zwraca null i zapisuje przyczynę. */
function fz_sluchaj_pobierz() {
	$klucz = fz_sluchaj_klucz();
	if ( '' === $klucz ) {
		return fz_sluchaj_zapisz_blad( 'Brak klucza: ustaw FZ_CMS_KEY w wp-config.php albo opcję fz_cms_key.' );
	}
	$odpowiedz = wp_remote_get( FZ_CMS_SLUCHAJ_URL, array(
		'timeout' => 8,
		'headers' => array( 'X-Site-Key' => $klucz, 'Accept' => 'application/json' ),
	) );
	if ( is_wp_error( $odpowiedz ) ) {
		return fz_sluchaj_zapisz_blad( 'Połączenie z ' . FZ_CMS_SLUCHAJ_URL . ' nie powiodło się: ' . $odpowiedz->get_error_message() );
	}
	$kod = (int) wp_remote_retrieve_response_code( $odpowiedz );
	$tresc = wp_remote_retrieve_body( $odpowiedz );
	if ( 200 !== $kod ) {
		return fz_sluchaj_zapisz_blad( 'CMS odpowiedział kodem ' . $kod . ' (' . FZ_CMS_SLUCHAJ_URL . '): ' . mb_substr( wp_strip_all_tags( $tresc ), 0, 200 ) );
	}
	$dane = json_decode( $tresc, true );
	if ( ! fz_sluchaj_poprawne( $dane ) ) {
		return fz_sluchaj_zapisz_blad( 'Odpowiedź CMS-a nie jest oczekiwanym JSON-em (początek: ' . mb_substr( wp_strip_all_tags( $tresc ), 0, 120 ) . ').' );
	}
	delete_option( 'fz_sluchaj_blad' );
	return $dane;
}

/** Dane strony: transient (2 dni) → CMS → ostatnia poprawna kopia → null. */
function fz_sluchaj_dane() {
	static $cache = false;
	if ( false !== $cache ) {
		return $cache;
	}
	// Po ?fz_odswiez=1 (administrator) transient jest pomijany w tym żądaniu, nawet jeśli coś już go wczytało.
	$wymus = ! empty( $GLOBALS['fz_sluchaj_wymus'] );
	$dane = $wymus ? false : get_transient( 'fz_sluchaj_dane' );
	if ( ! fz_sluchaj_poprawne( $dane ) ) {
		$dane = fz_sluchaj_pobierz();
		$GLOBALS['fz_sluchaj_pobrano'] = is_array( $dane );
		if ( is_array( $dane ) ) {
			set_transient( 'fz_sluchaj_dane', $dane, 172800 ); // 2 dni
			update_option( 'fz_sluchaj_ostatnie', $dane, false );
		} else {
			$dane = get_option( 'fz_sluchaj_ostatnie', null );
			$dane = fz_sluchaj_poprawne( $dane ) ? $dane : null;
			if ( $dane ) {
				set_transient( 'fz_sluchaj_dane', $dane, 3600 ); // CMS nie odpowiada – ponowna próba za godzinę, nie przy każdym wejściu
			}
		}
	}
	$cache = $dane;
	return $dane;
}

/** ?fz_odswiez=1 dla zalogowanego administratora: kasuje transient i cache strony. */
function fz_sluchaj_odswiez() {
	if ( empty( $_GET['fz_odswiez'] ) || ! is_user_logged_in() || ! current_user_can( 'manage_options' ) ) {
		return;
	}
	$GLOBALS['fz_sluchaj_wymus'] = true;
	delete_transient( 'fz_sluchaj_dane' );
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
add_action( 'init', 'fz_sluchaj_odswiez', 1 );

/** Ramka z przyczyną braku danych – tylko dla zalogowanego administratora; inni dostają komentarz HTML. */
function fz_sluchaj_brak() {
	$blad = get_option( 'fz_sluchaj_blad', array() );
	$tekst = is_array( $blad ) && ! empty( $blad['tekst'] ) ? $blad['tekst'] : 'brak danych';
	$html = '<!-- fz-sluchaj: ' . esc_html( $tekst ) . ' -->';
	if ( is_user_logged_in() && current_user_can( 'manage_options' ) ) {
		$html .= '<p class="fz-sluchaj-blad" style="border:2px solid #b00;padding:.5rem 1rem"><strong>Nagrania z CMS-a (widzi tylko administrator):</strong> '
			. esc_html( $tekst ) . ( ! empty( $blad['kiedy'] ) ? ' (' . esc_html( $blad['kiedy'] ) . ')' : '' ) . '</p>';
	}
	return $html;
}

/** Akapit #fza-data-message i <noscript> z dotychczasowego bloku danych; bez danych – komunikat błędu jak w JS. */
function fz_sluchaj_komunikat( $udalo ) {
	$poczatek = $udalo ? 'Lista audiobooków wczytuje się automatycznie. ' : 'Nie udało się wczytać listy audiobooków. ';
	return '<p id="fza-data-message" role="status">' . $poczatek . '<a href="https://hearthis.at/fantazmaty/">Otwórz nagrania na hearthis.at</a> lub <a href="https://www.youtube.com/channel/UCYa_9ETx5MRqLaBFdWcXi7A/videos">YouTube</a>.</p>'
		. '<noscript><p>Włącz JavaScript, aby korzystać z wyszukiwarki i filtrów audiobooków.</p></noscript>';
}

/** Polska odmiana: 1 nagranie, 2–4 nagrania, 5+ nagrań (12–14 jak 5+). */
function fz_sluchaj_odmiana( $n, $jeden, $kilka, $wiele ) {
	$n = (int) $n;
	if ( 1 === $n ) {
		return $jeden;
	}
	$ost = $n % 10;
	$dwie = $n % 100;
	return ( $ost >= 2 && $ost <= 4 && ( $dwie < 12 || $dwie > 14 ) ) ? $kilka : $wiele;
}

/** Liczba nagrań: z pola `liczby`, jeśli CMS je dał, inaczej policzona z danych. */
function fz_sluchaj_liczba( $dane ) {
	if ( isset( $dane['liczby']['nagrania'] ) ) {
		return (int) $dane['liczby']['nagrania'];
	}
	$nagrania = 0;
	foreach ( $dane['antologie'] as $antologia ) {
		$nagrania += count( (array) $antologia['nagrania'] );
	}
	return $nagrania;
}

add_shortcode( 'fz_sluchaj_dane', function () {
	$dane = fz_sluchaj_dane();
	if ( ! $dane ) {
		return fz_sluchaj_brak() . fz_sluchaj_komunikat( false );
	}
	$html = '';
	// Potwierdzenie dla administratora po ?fz_odswiez=1: skąd są dane i z jakiej wersji CMS-a.
	if ( ! empty( $_GET['fz_odswiez'] ) && is_user_logged_in() && current_user_can( 'manage_options' ) ) {
		$blad = get_option( 'fz_sluchaj_blad', array() );
		$udalo = ! empty( $GLOBALS['fz_sluchaj_pobrano'] );
		$html .= '<p class="fz-sluchaj-info" style="border:2px solid ' . ( $udalo ? '#080' : '#b00' ) . ';padding:.5rem 1rem"><strong>Nagrania z CMS-a (widzi tylko administrator):</strong> '
			. ( $udalo ? 'pobrano teraz świeże dane z CMS-a, wersja: ' . esc_html( isset( $dane['wersja'] ) ? $dane['wersja'] : '?' )
				: 'pobranie z CMS-a nie powiodło się' . ( is_array( $blad ) && ! empty( $blad['tekst'] ) ? ' – ' . esc_html( $blad['tekst'] ) : '' ) . '; pokazywana jest ostatnia kopia' )
			. '.</p>';
	}
	// Do strony idzie tylko lista antologii (mechanizm JS i tak ignoruje inne klucze).
	// JSON_HEX_TAG: „<” i „>” jako </> – ani „</script”, ani „<!--” w treści nie zepsują bloku <script>.
	$json = wp_json_encode( array( 'antologie' => $dane['antologie'] ), JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_HEX_TAG );
	return $html . '<script type="application/json" id="fza-audio-data">' . $json . '</script>' . fz_sluchaj_komunikat( true );
} );

add_shortcode( 'fz_sluchaj_licznik', function () {
	$dane = fz_sluchaj_dane();
	if ( ! $dane ) {
		return 'Lista nagrań jest chwilowo niedostępna<!-- fz-sluchaj: brak danych -->';
	}
	$nagrania = fz_sluchaj_liczba( $dane );
	return $nagrania . ' ' . fz_sluchaj_odmiana( $nagrania, 'nagranie', 'nagrania', 'nagrań' );
} );
