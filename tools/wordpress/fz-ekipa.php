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
 * ODŚWIEŻANIE: dane są pamiętane godzinę (transient fz_ekipa_dane); ostatnia poprawna kopia jest w opcji
 * fz_ekipa_ostatnie i służy, gdy CMS nie odpowie. Zalogowany administrator wchodzi na stronę
 * z ?fz_odswiez=1, żeby pobrać dane od razu (czyszczone są też cache wtyczek, jeśli są zainstalowane).
 *
 * CSS (do wspólnego pliku CSS strony – działy są teraz w jednym kontenerze Avady, więc odstępy
 * między nimi muszą dać sekcje; dawniej dawały je osobne kontenery):
 *   .fzo-team-sections .fzo-team-section + .fzo-team-section { margin-top: 3rem; }
 *   .fzo-team-sections .fzo-team-section { scroll-margin-top: 6rem; }   (kotwica – id na <section> – pod stałym nagłówkiem)
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

/** Pobiera dane z CMS-a; przy błędzie zwraca null. */
function fz_ekipa_pobierz() {
	$klucz = fz_ekipa_klucz();
	if ( '' === $klucz ) {
		return null;
	}
	$odpowiedz = wp_remote_get( FZ_CMS_URL, array(
		'timeout' => 8,
		'headers' => array( 'X-Site-Key' => $klucz, 'Accept' => 'application/json' ),
	) );
	if ( is_wp_error( $odpowiedz ) || 200 !== (int) wp_remote_retrieve_response_code( $odpowiedz ) ) {
		return null;
	}
	$dane = json_decode( wp_remote_retrieve_body( $odpowiedz ), true );
	if ( ! is_array( $dane ) || ! isset( $dane['dzialy'] ) || ! is_array( $dane['dzialy'] ) ) {
		return null;
	}
	return $dane;
}

/** Dane ekipy: transient (1 h) → CMS → ostatnia poprawna kopia → null. */
function fz_ekipa_dane() {
	static $cache = false;
	if ( false !== $cache ) {
		return $cache;
	}
	$dane = get_transient( 'fz_ekipa_dane' );
	if ( ! is_array( $dane ) ) {
		$dane = fz_ekipa_pobierz();
		if ( is_array( $dane ) ) {
			set_transient( 'fz_ekipa_dane', $dane, HOUR_IN_SECONDS );
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
}
add_action( 'template_redirect', 'fz_ekipa_odswiez', 1 );

function fz_ekipa_brak() {
	return "<!-- fz-ekipa: brak danych -->";
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
	$html = '<nav aria-label="Działy">';
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
		$html .= '<section class="fzo-team-section" id="' . $kotwica . '">';
		$html .= '<h2 class="fz-h2">' . esc_html( $dzial['tytul'] );
		if ( ! empty( $dzial['liczba_tekst'] ) ) {
			$html .= ' <span class="fzo-team-count">' . esc_html( $dzial['liczba_tekst'] ) . '</span>';
		}
		$html .= '</h2>';
		if ( ! empty( $dzial['koordynacja'] ) ) {
			$html .= '<p class="fzo-team-coord"><strong>Koordynacja:</strong> '
				. esc_html( implode( ', ', $dzial['koordynacja'] ) ) . '</p>';
		}
		$grupy = isset( $dzial['grupy'] ) ? (array) $dzial['grupy'] : array();
		if ( ! empty( $dzial['notka'] ) ) {
			$html .= '<p class="fzo-team-note">' . fz_ekipa_notka_kses( $dzial['notka'] ) . '</p>';
		} elseif ( 1 === count( $grupy ) && '' === (string) $grupy[0]['tytul'] ) {
			$html .= '<div class="fzo-team-list">' . fz_ekipa_lista( $grupy[0]['osoby'] ) . '</div>';
		} elseif ( $grupy ) {
			$html .= '<div class="fzo-team-list"><div class="fzo-team-groups">';
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
