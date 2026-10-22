<?php
/**
 * Test snippetów fz-ekipa.php, fz-katalog.php i fz-sluchaj.php z atrapami funkcji WordPressa (bez WordPressa).
 *
 * Uruchomienie: php tools/wordpress/test-snippets.php – kod wyjścia 0, gdy wszystko się zgadza.
 * Każde wejście na stronę to osobny proces PHP (snippety pamiętają dane w zmiennych statycznych); opcje,
 * transienty i „zegar” są przenoszone między procesami w pliku tymczasowym. Sprawdzane: ponowna walidacja
 * z If-None-Match co FZ_*_CO_ILE sekund (304 – bez zmian, 200 – nowe dane i czyszczenie cache strony, błąd lub
 * złe dane – stara kopia i kolejna próba dopiero po tym czasie) oraz ?fz_odswiez=1 (pobranie bez If-None-Match).
 */

$snippety = array(
	'ekipa'   => array( 'plik' => 'fz-ekipa.php', 'dane' => 'fz_ekipa_dane', 'odswiez' => 'fz_ekipa_odswiez' ),
	'katalog' => array( 'plik' => 'fz-katalog.php', 'dane' => 'fz_katalog_dane', 'odswiez' => 'fz_katalog_odswiez' ),
	'sluchaj' => array( 'plik' => 'fz-sluchaj.php', 'dane' => 'fz_sluchaj_dane', 'odswiez' => 'fz_sluchaj_odswiez' ),
);

/** Poprawne dane w kształcie danego snippetu; $wersja odróżnia treść. */
function dane_testowe( $snippet, $wersja ) {
	switch ( $snippet ) {
		case 'ekipa':
			return array( 'prowadzacy' => 'Anna ' . $wersja, 'strona' => 'Jan', 'wersja' => $wersja, 'dzialy' => array() );
		case 'katalog':
			return array( 'wersja' => $wersja, 'antologie' => array( array( 'nazwa' => 'A', 'opowiadania' => array( array( 'tytul' => 'T ' . $wersja ) ) ) ) );
		default:
			return array( 'wersja' => $wersja, 'antologie' => array( array( 'id' => 'a', 'nazwa' => 'A', 'okladka' => 'https://fantazmaty.pl/a.jpg',
				'nagrania' => array( array( 'id' => 'n1', 'tytul' => 'T ' . $wersja, 'autor' => 'Autor', 'youtube' => 'https://youtu.be/x' ) ) ) ) );
	}
}

// --- proces jednego wejścia na stronę -------------------------------------------------------------------------------

if ( isset( $argv[1] ) && 'wejscie' === $argv[1] ) {
	list( , , $snippet, $plik_stanu ) = $argv;
	$stan = json_decode( file_get_contents( $plik_stanu ), true ); // zmienna globalna – atrapy używają $GLOBALS['stan']
	$GLOBALS['zadania'] = array();
	$GLOBALS['czyszczenia'] = 0;
	$GLOBALS['akcje'] = array();
	class WP_Error {
		public function __construct( $kod, $tekst ) { $this->tekst = $tekst; }
		public function get_error_message() { return $this->tekst; }
	}
	function get_option( $nazwa, $domyslna = false ) { return array_key_exists( $nazwa, $GLOBALS['stan']['opcje'] ) ? $GLOBALS['stan']['opcje'][ $nazwa ] : $domyslna; }
	function update_option( $nazwa, $wartosc, $autoload = null ) { $GLOBALS['stan']['opcje'][ $nazwa ] = $wartosc; return true; }
	function delete_option( $nazwa ) { unset( $GLOBALS['stan']['opcje'][ $nazwa ] ); return true; }
	function get_transient( $nazwa ) {
		$t = isset( $GLOBALS['stan']['transienty'][ $nazwa ] ) ? $GLOBALS['stan']['transienty'][ $nazwa ] : null;
		return ( $t && $t['do'] > $GLOBALS['stan']['czas'] ) ? $t['wartosc'] : false;
	}
	function set_transient( $nazwa, $wartosc, $sekundy ) { $GLOBALS['stan']['transienty'][ $nazwa ] = array( 'wartosc' => $wartosc, 'do' => $GLOBALS['stan']['czas'] + $sekundy ); return true; }
	function delete_transient( $nazwa ) { unset( $GLOBALS['stan']['transienty'][ $nazwa ] ); return true; }
	// Trwały cache obiektów: wp_cache_flush() kasuje też transienty.
	function wp_cache_flush() { $GLOBALS['stan']['transienty'] = array(); return true; }
	function rocket_clean_post( $id ) { $GLOBALS['czyszczenia']++; }
	function do_action( $nazwa, ...$argumenty ) {}
	function add_action( $nazwa, $funkcja, $priorytet = 10 ) { $GLOBALS['akcje'][ $nazwa ][] = $funkcja; }
	function add_shortcode( $nazwa, $funkcja ) { $GLOBALS['shortcody'][ $nazwa ] = $funkcja; }
	function get_queried_object_id() { return 7; }
	function is_user_logged_in() { return ! empty( $GLOBALS['stan']['admin'] ); }
	function current_user_can( $uprawnienie ) { return ! empty( $GLOBALS['stan']['admin'] ); }
	function current_time( $typ ) { return '2026-01-01 00:00:00'; }
	function wp_strip_all_tags( $tekst ) { return strip_tags( $tekst ); }
	function esc_html( $tekst ) { return htmlspecialchars( (string) $tekst, ENT_QUOTES ); }
	function esc_attr( $tekst ) { return esc_html( $tekst ); }
	function esc_url( $tekst ) { return esc_html( $tekst ); }
	function wp_kses( $html, $dozwolone ) { return $html; }
	function wp_parse_url( $adres ) { return parse_url( $adres ); }
	function wp_json_encode( $dane, $flagi = 0 ) { return json_encode( $dane, $flagi ); }
	function is_wp_error( $wartosc ) { return $wartosc instanceof WP_Error; }
	function wp_remote_get( $adres, $argumenty ) {
		$GLOBALS['zadania'][] = $argumenty['headers'];
		$odpowiedz = array_shift( $GLOBALS['stan']['odpowiedzi'] );
		if ( null === $odpowiedz ) {
			throw new RuntimeException( 'Nieoczekiwane zapytanie do CMS-a.' );
		}
		return 'blad' === $odpowiedz['kod'] ? new WP_Error( 'http', 'timeout' ) : $odpowiedz;
	}
	function wp_remote_retrieve_response_code( $odpowiedz ) { return $odpowiedz['kod']; }
	function wp_remote_retrieve_body( $odpowiedz ) { return $odpowiedz['tresc']; }
	function wp_remote_retrieve_header( $odpowiedz, $nazwa ) { return isset( $odpowiedz['etag'] ) ? $odpowiedz['etag'] : ''; }

	define( 'FZ_CMS_KEY', 'klucz' );
	define( 'FZ_CMS_URL', 'https://cms.example/api/strona/ekipa/' );
	require __DIR__ . '/' . $snippety[ $snippet ]['plik'];
	if ( ! empty( $stan['odswiez'] ) ) {
		$_GET['fz_odswiez'] = '1';
	}
	foreach ( isset( $GLOBALS['akcje']['wp'] ) ? $GLOBALS['akcje']['wp'] : array() as $funkcja ) {
		$funkcja();
	}
	$dane = call_user_func( $snippety[ $snippet ]['dane'] );
	$html = 'katalog' === $snippet ? call_user_func( $GLOBALS['shortcody']['fz_katalog_dane'] ) : '';
	$stan['wynik'] = array( 'wersja' => $dane ? $dane['wersja'] : null, 'zadania' => $GLOBALS['zadania'],
		'czyszczenia' => $GLOBALS['czyszczenia'], 'html' => $html );
	file_put_contents( $plik_stanu, json_encode( $stan ) );
	exit( 0 );
}

// --- scenariusz -----------------------------------------------------------------------------------------------------

$bledy = 0;
foreach ( array_keys( $snippety ) as $snippet ) {
	$plik_stanu = tempnam( sys_get_temp_dir(), 'fz' );
	$stan = array( 'opcje' => array(), 'transienty' => array(), 'czas' => 0 );
	$odpowiedz = function ( $wersja ) use ( $snippet ) {
		return array( 'kod' => 200, 'tresc' => json_encode( dane_testowe( $snippet, $wersja ) ), 'etag' => '"' . $wersja . '"' );
	};
	// [czas, odpowiedzi CMS-a, ?fz_odswiez=1 (administrator), oczekiwana wersja, oczekiwane If-None-Match (null – bez
	//  zapytania, '' – zapytanie bez nagłówka), czyszczenia cache, opis]
	$kroki = array(
		array( 0, array( $odpowiedz( 'a' ) ), false, 'a', '', 1, 'pierwsze wejście: pobranie i czyszczenie cache' ),
		array( 300, array(), false, 'a', null, 0, 'przed upływem 10 minut: bez pytania CMS-a' ),
		array( 600, array( array( 'kod' => 304, 'tresc' => '' ) ), false, 'a', '"a"', 0, 'po 10 minutach: 304 – bez zmian' ),
		array( 1200, array( $odpowiedz( 'b' ) ), false, 'b', '"a"', 1, 'zmiana w CMS-ie: 200 – nowe dane i czyszczenie' ),
		array( 1300, array(), false, 'b', null, 0, 'po zmianie znacznik działa dalej (mimo wp_cache_flush)' ),
		array( 1800, array( array( 'kod' => 'blad' ) ), false, 'b', '"b"', 0, 'błąd połączenia: stara kopia' ),
		array( 2000, array(), false, 'b', null, 0, 'po błędzie: bez ponownej próby przy każdym wejściu' ),
		array( 2400, array( array( 'kod' => 500, 'tresc' => 'awaria' ) ), false, 'b', '"b"', 0, 'kod 500: stara kopia' ),
		array( 3000, array( array( 'kod' => 200, 'tresc' => '{"zle":1}', 'etag' => '"z"' ) ), false, 'b', '"b"', 0, 'złe dane: stara kopia' ),
		array( 3100, array( $odpowiedz( 'c' ) ), true, 'c', '', 1, '?fz_odswiez=1: od razu, bez If-None-Match' ),
		array( 3200, array(), false, 'c', null, 0, 'po ?fz_odswiez=1: zwykłe wejście bez pytania' ),
	);
	foreach ( $kroki as list( $czas, $odpowiedzi, $odswiez, $wersja, $if_none_match, $czyszczenia, $opis ) ) {
		$stan['czas'] = $czas;
		$stan['odpowiedzi'] = $odpowiedzi;
		$stan['odswiez'] = $odswiez;
		$stan['admin'] = $odswiez;
		file_put_contents( $plik_stanu, json_encode( $stan ) );
		$wyjscie = array();
		exec( escapeshellarg( PHP_BINARY ) . ' ' . escapeshellarg( __FILE__ ) . ' wejscie ' . $snippet . ' ' . escapeshellarg( $plik_stanu ) . ' 2>&1', $wyjscie, $kod );
		$stan = json_decode( file_get_contents( $plik_stanu ), true );
		$wynik = isset( $stan['wynik'] ) ? $stan['wynik'] : array();
		$zadania = isset( $wynik['zadania'] ) ? $wynik['zadania'] : array();
		$naglowek = null === $if_none_match ? null : ( isset( $zadania[0]['If-None-Match'] ) ? $zadania[0]['If-None-Match'] : '' );
		$problemy = array();
		if ( 0 !== $kod ) {
			$problemy[] = 'proces: ' . implode( ' ', $wyjscie );
		}
		if ( ( isset( $wynik['wersja'] ) ? $wynik['wersja'] : null ) !== $wersja ) {
			$problemy[] = 'wersja ' . var_export( isset( $wynik['wersja'] ) ? $wynik['wersja'] : null, true ) . ' zamiast ' . $wersja;
		}
		if ( count( $zadania ) !== ( null === $if_none_match ? 0 : 1 ) || ( $zadania && $naglowek !== $if_none_match ) ) {
			$problemy[] = 'zapytania do CMS-a: ' . json_encode( $zadania );
		}
		if ( $zadania && 'klucz' !== $zadania[0]['X-Site-Key'] ) {
			$problemy[] = 'brak nagłówka X-Site-Key';
		}
		if ( ( isset( $wynik['czyszczenia'] ) ? $wynik['czyszczenia'] : -1 ) !== $czyszczenia ) {
			$problemy[] = 'czyszczenia cache: ' . ( isset( $wynik['czyszczenia'] ) ? $wynik['czyszczenia'] : '?' );
		}
		if ( 'katalog' === $snippet && false === strpos( isset( $wynik['html'] ) ? $wynik['html'] : '', '<script type="application/json" id="fzs-data">' ) ) {
			$problemy[] = 'shortcode [fz_katalog_dane] bez bloku danych';
		}
		echo ( $problemy ? 'BŁĄD' : 'OK  ' ) . " {$snippet}: {$opis}" . ( $problemy ? ' – ' . implode( '; ', $problemy ) : '' ) . "\n";
		$bledy += $problemy ? 1 : 0;
	}
	unlink( $plik_stanu );
}
echo $bledy ? "Błędy: {$bledy}\n" : "Wszystko OK\n";
exit( $bledy ? 1 : 0 );
