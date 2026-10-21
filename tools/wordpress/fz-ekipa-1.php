<?php
/**
 * Ekipa Fantazmatów z CMS-a – snippet do wtyczki Code Snippets (fantazmaty.pl).
 *
 * JAK WKLEIĆ: Code Snippets → Add New → wklej wszystko OD LINII POD `<?php` (Code Snippets sam dodaje
 * otwierający tag; wklejenie `<?php` daje błąd składni). Typ: „Run snippet everywhere”. Zapisz i aktywuj.
 *
 * KONFIGURACJA (wp-config.php, nad linią „That's all, stop editing”):
 *   define('FZ_CMS_URL', 'https://<domena-cms>/api/strona/ekipa/');
 *   define('FZ_CMS_KEY', '<ten sam klucz co SITE_API_KEY w .env CMS-a>');
 * Bez dostępu do wp-config.php klucz można zapisać w opcji WordPressa (jednorazowo, np. w WP-CLI
 * albo w drugim tymczasowym snippecie): update_option('fz_cms_key', '<klucz>', false);
 * Stała z wp-config.php ma pierwszeństwo przed opcją.
 *
 * SHORTCODE'Y:
 *   [fz_ekipa_nawigacja]  – przyciski „Przejdź do działu” (działy z w_nawigacji) + „Strona” na końcu
 *                           (atrybut strona="" wyłącza, strona="Inna etykieta" zmienia napis)
 *   [fz_ekipa_prowadzacy] – <p>Imię Nazwisko</p> redaktora prowadzącego
 *   [fz_ekipa_strona]     – <p>Imię Nazwisko</p> opiekuna strony
 *   [fz_ekipa_dzialy]     – wszystkie działy (nagłówek z liczbą osób, koordynacja, listy albo notka)
 *
 * DIAGNOSTYKA: gdy strona nie ma danych, zalogowany administrator widzi w miejscu shortcode'u czerwoną
 * ramkę z przyczyną (brak klucza, kod odpowiedzi CMS-a, błąd połączenia). Inni widzą tylko komentarz HTML.
 *
 * ODŚWIEŻANIE: dane są pamiętane 2 dni (transient fz_ekipa_dane; liczba 172800 w fz_ekipa_dane to sekundy),
 * potem pierwsze wejście na stronę pobiera je na nowo. Ostatnia poprawna kopia jest w opcji fz_ekipa_ostatnie
 * i służy, gdy CMS nie odpowie. Zalogowany administrator wchodzi na stronę z ?fz_odswiez=1, żeby pobrać
 * dane od razu (czyszczone są też cache wtyczek, jeśli są zainstalowane).
 *
 * AKTUALIZACJA SNIPPETU: najpierw dezaktywuj snippet, potem podmień kod, zapisz i aktywuj (inaczej Code
 * Snippets zgłosi „Cannot redeclare function”, bo sprawdza nowy kod przy załadowanym starym).
 *
 * CSS (do wspólnego pliku CSS strony – działy są teraz w jednym kontenerze Avady, więc odstępy
 * między nimi muszą dać sekcje; dawniej dawały je osobne kontenery):
 *   .fzo-team-sections .fzo-team-section { scroll-margin-top: 6rem; }   (kotwica – id na <section> – pod stałym nagłówkiem)
 *   Odstęp między działami daje reguła .fz-page.fzo-team-section { margin-bottom: 34px } z CSS strony.
 */

if ( ! defined( 'FZ_CMS_URL' ) ) {
	define( 'FZ_CMS_URL', 'https://<domena-cms>/api/strona/ekipa/' );  // ustaw w wp-config.php (patrz wyżej)
}

function fz_ekipa_klucz() {
	if ( defined( 'FZ_CMS_KEY' ) && FZ_CMS_KEY ) {
		return FZ_CMS_KEY;
	}
	return (string) get_option( 'fz_cms_key', '' );
}

/** Zapamiętuje przyczynę ostatniego nieudanego pobrania (do diagnostyki przez ?fz_odswiez=1). */
function fz_ekipa_zapisz_blad( $tekst ) {
	update_option( 'fz_ekipa_blad', array( 'kiedy' => current_time( 'mysql' ), 'tekst' => $tekst ), false );
	return null;
}

/** Pobiera dane z CMS-a; przy błędzie zwraca null i zapisuje przyczynę. */
function fz_ekipa_pobierz() {
	$klucz = fz_ekipa_klucz();
	if ( '' === $klucz ) {
		return fz_ekipa_zapisz_blad( 'Brak klucza: ustaw FZ_CMS_KEY w wp-config.php albo opcję fz_cms_key.' );
	}
	$odpowiedz = wp_remote_get( FZ_CMS_URL, array(
		'timeout' => 8,
		'headers' => array( 'X-Site-Key' => $klucz, 'Accept' => 'application/json' ),
	) );
	if ( is_wp_error( $odpowiedz ) ) {
		return fz_ekipa_zapisz_blad( 'Połączenie z ' . FZ_CMS_URL . ' nie powiodło się: ' . $odpowiedz->get_error_message() );
	}
	$kod = (int) wp_remote_retrieve_response_code( $odpowiedz );
	$tresc = wp_remote_retrieve_body( $odpowiedz );
	if ( 200 !== $kod ) {
		return fz_ekipa_zapisz_blad( 'CMS odpowiedział kodem ' . $kod . ' (' . FZ_CMS_URL . '): ' . mb_substr( wp_strip_all_tags( $tresc ), 0, 200 ) );
	}
	$dane = json_decode( $tresc, true );
	if ( ! is_array( $dane ) || ! isset( $dane['dzialy'] ) || ! is_array( $dane['dzialy'] ) ) {
		return fz_ekipa_zapisz_blad( 'Odpowiedź CMS-a nie jest oczekiwanym JSON-em (początek: ' . mb_substr( wp_strip_all_tags( $tresc ), 0, 120 ) . ').' );
	}
	delete_option( 'fz_ekipa_blad' );
	return $dane;
}

/** Dane ekipy: transient (2 dni) → CMS → ostatnia poprawna kopia → null. */
function fz_ekipa_dane() {
	static $cache = false;
	if ( false !== $cache ) {
		return $cache;
	}
	// Po ?fz_odswiez=1 (administrator) transient jest pomijany w tym żądaniu, nawet jeśli coś już go wczytało.
	$wymus = ! empty( $GLOBALS['fz_ekipa_wymus'] );
	$dane = $wymus ? false : get_transient( 'fz_ekipa_dane' );
	if ( ! is_array( $dane ) ) {
		$dane = fz_ekipa_pobierz();
		$GLOBALS['fz_ekipa_pobrano'] = is_array( $dane );
		if ( is_array( $dane ) ) {
			set_transient( 'fz_ekipa_dane', $dane, 172800 ); // 2 dni
			update_option( 'fz_ekipa_ostatnie', $dane, false );
		} else {
			$dane = get_option( 'fz_ekipa_ostatnie', null );
			$dane = is_array( $dane ) ? $dane : null;
		}
	}
	$cache = $dane;
	return $dane;
}

/** ?fz_odswiez=1 dla zalogowanego administratora: kasuje transient i cache strony. */
function fz_ekipa_odswiez() {
	if ( empty( $_GET['fz_odswiez'] ) || ! is_user_logged_in() || ! current_user_can( 'manage_options' ) ) {
		return;
	}
	$GLOBALS['fz_ekipa_wymus'] = true;
	delete_transient( 'fz_ekipa_dane' );
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
add_action( 'init', 'fz_ekipa_odswiez', 1 );

/** Bez danych: komentarz HTML; zalogowany administrator widzi przyczynę na stronie. */
function fz_ekipa_brak() {
	$blad = get_option( 'fz_ekipa_blad', array() );
	$tekst = is_array( $blad ) && ! empty( $blad['tekst'] ) ? $blad['tekst'] : 'brak danych';
	$html = '<!-- fz-ekipa: ' . esc_html( $tekst ) . ' -->';
	if ( is_user_logged_in() && current_user_can( 'manage_options' ) ) {
		$html .= '<p class="fz-ekipa-blad" style="border:2px solid #b00;padding:.5rem 1rem"><strong>Ekipa z CMS-a (widzi tylko administrator):</strong> '
			. esc_html( $tekst ) . ( ! empty( $blad['kiedy'] ) ? ' (' . esc_html( $blad['kiedy'] ) . ')' : '' ) . '</p>';
	}
	return $html;
}

function fz_ekipa_notka_kses( $html ) {
	return wp_kses( $html, array( 'a' => array( 'href' => true ), 'strong' => array(), 'em' => array() ) );
}

function fz_ekipa_lista( $osoby ) {
	$osoby = array_filter( (array) $osoby, 'strlen' );
	if ( ! $osoby ) {
		return '';
	}
	$html = '<ul>';
	foreach ( $osoby as $osoba ) {
		$html .= '<li>' . esc_html( $osoba ) . '</li>';
	}
	return $html . '</ul>';
}

add_shortcode( 'fz_ekipa_nawigacja', function ( $atts = array() ) {
	$dane = fz_ekipa_dane();
	if ( ! $dane ) {
		return fz_ekipa_brak();
	}
	// strona="" wyłącza końcowy przycisk do sekcji „Strona” (kotwica ekipa-strona zostaje w Avadzie).
	$atts = shortcode_atts( array( 'strona' => 'Strona' ), $atts, 'fz_ekipa_nawigacja' );
	$html = '';
	// Potwierdzenie dla administratora po ?fz_odswiez=1: skąd są dane i z jakiej wersji CMS-a.
	if ( ! empty( $_GET['fz_odswiez'] ) && is_user_logged_in() && current_user_can( 'manage_options' ) ) {
		$blad = get_option( 'fz_ekipa_blad', array() );
		$udalo = ! empty( $GLOBALS['fz_ekipa_pobrano'] );
		$html .= '<p class="fz-ekipa-info" style="border:2px solid ' . ( $udalo ? '#080' : '#b00' ) . ';padding:.5rem 1rem"><strong>Ekipa z CMS-a (widzi tylko administrator):</strong> '
			. ( $udalo ? 'pobrano teraz świeże dane z CMS-a, wersja: ' . esc_html( isset( $dane['wersja'] ) ? $dane['wersja'] : '?' )
				: 'pobranie z CMS-a nie powiodło się' . ( is_array( $blad ) && ! empty( $blad['tekst'] ) ? ' – ' . esc_html( $blad['tekst'] ) : '' ) . '; pokazywana jest ostatnia kopia' )
			. '.</p>';
	}
	$html .= '<nav aria-label="Działy">';
	foreach ( $dane['dzialy'] as $dzial ) {
		if ( empty( $dzial['w_nawigacji'] ) ) {
			continue;
		}
		$html .= '<a class="fz-btn fz-btn--ghost" href="#' . esc_attr( $dzial['kotwica'] ) . '">'
			. esc_html( $dzial['etykieta_nawigacji'] ) . '</a>';
	}
	if ( '' !== $atts['strona'] && ! empty( $dane['strona'] ) ) {
		$html .= '<a class="fz-btn fz-btn--ghost" href="#ekipa-strona">' . esc_html( $atts['strona'] ) . '</a>';
	}
	return $html . '</nav>';
} );

add_shortcode( 'fz_ekipa_prowadzacy', function () {
	$dane = fz_ekipa_dane();
	return $dane && ! empty( $dane['prowadzacy'] ) ? '<p>' . esc_html( $dane['prowadzacy'] ) . '</p>' : fz_ekipa_brak();
} );

add_shortcode( 'fz_ekipa_strona', function () {
	$dane = fz_ekipa_dane();
	return $dane && ! empty( $dane['strona'] ) ? '<p>' . esc_html( $dane['strona'] ) . '</p>' : fz_ekipa_brak();
} );

add_shortcode( 'fz_ekipa_dzialy', function () {
	$dane = fz_ekipa_dane();
	if ( ! $dane ) {
		return fz_ekipa_brak();
	}
	$html = '';
	foreach ( $dane['dzialy'] as $dzial ) {
		$kotwica = esc_attr( $dzial['kotwica'] );
		// Znaczniki jak z Avady (klasy kontenera, fusion-title, fusion-text), żeby CSS strony działał bez zmian.
		$html .= '<section class="fz-page fz-about fzo-body fzo-team-section" id="' . $kotwica . '">';
		$html .= '<div class="fusion-title title fusion-title-size-two fz-h2">'
			. '<h2 class="fusion-title-heading title-heading-left fz-h2">' . esc_html( $dzial['tytul'] );
		if ( ! empty( $dzial['liczba_tekst'] ) ) {
			$html .= ' <span class="fzo-team-count">' . esc_html( $dzial['liczba_tekst'] ) . '</span>';
		}
		$html .= '</h2></div>';
		if ( ! empty( $dzial['koordynacja'] ) ) {
			$html .= '<div class="fusion-text fzo-team-coord"><p><strong>Koordynacja:</strong> '
				. esc_html( implode( ', ', $dzial['koordynacja'] ) ) . '</p></div>';
		}
		$grupy = isset( $dzial['grupy'] ) ? (array) $dzial['grupy'] : array();
		if ( ! empty( $dzial['notka'] ) ) {
			$html .= '<div class="fusion-text fzo-team-note"><p>' . fz_ekipa_notka_kses( $dzial['notka'] ) . '</p></div>';
		} elseif ( 1 === count( $grupy ) && '' === (string) $grupy[0]['tytul'] ) {
			$html .= '<div class="fusion-text fzo-team-list">' . fz_ekipa_lista( $grupy[0]['osoby'] ) . '</div>';
		} elseif ( $grupy ) {
			$html .= '<div class="fusion-text fzo-team-list"><div class="fzo-team-groups">';
			foreach ( $grupy as $grupa ) {
				if ( empty( $grupa['osoby'] ) ) {
					continue;
				}
				$html .= '<div>';
				if ( '' !== (string) $grupa['tytul'] ) {
					$html .= '<h3 class="fzo-team-sub">' . esc_html( $grupa['tytul'] ) . '</h3>';
				}
				$html .= fz_ekipa_lista( $grupa['osoby'] ) . '</div>';
			}
			$html .= '</div></div>';
		}
		$html .= '</section>';
	}
	return $html;
} );
