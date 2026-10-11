"""Reguły korekty Odkurzacza na poziomie tekstu oraz pola i limity formularzy programów."""
import json
import sys
import time
from difflib import SequenceMatcher
from io import BytesIO
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from django.test import SimpleTestCase
from docx import Document

from core.odkurzacz_forms import DocumentConversionForm, OdkurzaczForm, RepetitionsForm
from core.services.odkurzacz import (
    DEFAULT_EDITORIAL_RULES, DEFAULT_REPLACEMENTS, EDITORIAL_RULES, FORM_DEFAULT_EDITORIAL_RULES, _change_regions,
    USER_WORD_EXACT, USER_WORD_PATTERNS, _edit_opcodes, clean_docx, correct_editorial_text,
)

from .support import REPETITION_PARAMETERS, saved, sized_docx, upload


class EditorialTextRuleTests(SimpleTestCase):
    def fix(self, text):
        return correct_editorial_text(text, DEFAULT_EDITORIAL_RULES)

    def test_protected_abbreviations_keep_internal_spacing(self):
        """Regresja: skróty typu „o.o.” były rozbijane albo dostawały odstęp jak po zdaniu."""
        for abbreviation in ('ś.p.', 'c.d.n.', 'o.o.', 'j.w.'):
            with self.subTest(abbreviation=abbreviation):
                self.assertIn(abbreviation, self.fix(f'To {abbreviation} przykład.'))
        self.assertEqual(correct_editorial_text('To o.o. przykład.', ['after_punct']), 'To o.o. przykład.')

    def test_period_stays_inside_quote_when_the_sentence_continues(self):
        self.assertEqual(self.fix('Napisał „tak.” i wyszedł.'), 'Napisał „tak.” i\u00a0wyszedł.')
        self.assertEqual(self.fix('Powiedział „tak.”'), 'Powiedział „tak”.')
        self.assertEqual(self.fix('Napisał „tak.” Potem wyszedł.'), 'Napisał „tak”. Potem wyszedł.')

    def test_letter_marks_after_numbers_are_not_units(self):
        self.assertEqual(self.fix('Klasy 2B i 3A wyszły.'), 'Klasy 2B i\u00a03A wyszły.')
        self.assertEqual(self.fix('Telewizor 4K.'), 'Telewizor 4K.')
        self.assertEqual(self.fix('Kabel 2m, 5kg i 20 K.'), 'Kabel 2\u00a0m, 5\u00a0kg i\u00a020\u00a0K.')

    def test_gender_forms_in_brackets_stay_joined(self):
        self.assertEqual(self.fix('Drogi(a) Kliencie, zrobił(a) to.'), 'Drogi(a) Kliencie, zrobił(a) to.')
        self.assertEqual(self.fix('Pies(kundel) szczekał.'), 'Pies (kundel) szczekał.')

    def test_score_keeps_spaces_around_colon(self):
        self.assertEqual(self.fix('Wynik 3 : 1 , a potem :'), 'Wynik 3 : 1, a\u00a0potem:')

    def test_partial_fragment_is_not_a_new_dialogue(self):
        rules = ['hyphen_dash', 'dash_spaces', 'trim']
        self.assertEqual(correct_editorial_text(' – narracja', rules, trim_start=False), ' – narracja')
        self.assertEqual(correct_editorial_text(' - narracja', rules, trim_start=False), ' – narracja')
        self.assertEqual(correct_editorial_text('tekst – ', rules, trim_end=False), 'tekst – ')
        self.assertEqual(correct_editorial_text('  –Hę?', rules), '– Hę?')

    def test_typographic_rules_are_available_and_can_be_disabled(self):
        added = {'single_nbsp', 'unit_nbsp', 'reference_nbsp', 'dates_times', 'decimal', 'digit_groups'}
        self.assertTrue(added <= {key for key, _ in EDITORIAL_RULES})
        source = '2025-1-2 2/1/2025 o 9.30 9:30 12.50 kg 123456 kg A.Kowalski 20°C'
        self.assertEqual(correct_editorial_text(source, []), source)
        self.assertIn('\u00a0', correct_editorial_text(source))

    def test_defaults_and_explicit_empty_paragraph_rule(self):
        self.assertEqual(correct_editorial_text('kot\tkota', {'tabs'}), 'kot kota')
        self.assertEqual(correct_editorial_text('1.02.2025', {'dates_times'}), '01.02.2025')
        self.assertNotIn('empty_paragraphs', DEFAULT_EDITORIAL_RULES)
        self.assertNotIn('user_word_corrections', DEFAULT_EDITORIAL_RULES)
        self.assertEqual(
            correct_editorial_text('np. kot. pies! – powiedział.', {'sentence_case'}),
            'np. kot. Pies! – powiedział.',
        )
        document = Document()
        document.add_paragraph('A')
        document.add_paragraph()
        document.add_paragraph()
        document.add_paragraph('B')
        payload = saved(document).getvalue()
        marked = Document(clean_docx(BytesIO(payload), ['empty_paragraphs']))
        self.assertEqual([p.text for p in marked.paragraphs], ['A', '', 'B'])
        marked = Document(clean_docx(BytesIO(payload), DEFAULT_EDITORIAL_RULES))
        self.assertEqual(len(marked.paragraphs), 4)

    def test_edit_opcodes_handle_the_longest_paragraph_quickly(self):
        source = ('Ala  ma kota a kot ma Alę... ' * 700)[:20000]
        corrected = self.fix(source)
        started = time.monotonic()
        opcodes = _edit_opcodes(source, corrected)
        self.assertLess(time.monotonic() - started, 2)
        rebuilt = ''.join(source[a:b] if tag == 'equal' else corrected[c:d]
                          for tag, a, b, c, d in opcodes if tag != 'delete')
        self.assertEqual(rebuilt, corrected)


class OptionalAndEdgeRuleTests(SimpleTestCase):
    """Reguły spoza domyślnego zestawu oraz przypadki, które reguły celowo zostawiają bez zmian."""

    def fix(self, text):
        return correct_editorial_text(text, DEFAULT_EDITORIAL_RULES)

    def test_personal_pronouns_are_lowered_only_inside_sentences(self):
        self.assertEqual(
            correct_editorial_text('Spotkałem Go wczoraj. On przyszedł. Dałem Mu to: Ona wie. '
                                   'Mówił np. Ona przyjdzie! Ja też. Widziałem JEGO.', {'pronouns_lower'}),
            'Spotkałem go wczoraj. On przyszedł. Dałem mu to: Ona wie. '
            'Mówił np. ona przyjdzie! Ja też. Widziałem jego.')
        self.assertEqual(correct_editorial_text('Pisał – Ona wie, https://example.com/On', {'pronouns_lower'}),
                         'Pisał – ona wie, https://example.com/On')

    def test_user_word_list_keeps_the_original_letter_case(self):
        self.assertEqual(
            correct_editorial_text('Pickupem jechał niechcąco, PICKUPAMI; europejczykiem i azjatami; '
                                   'komedia dellarte; Przekonywujący oddziaływującego', {'user_word_corrections'}),
            'Pikapem jechał niechcący, PIKAPAMI; Europejczykiem i Azjatami; '
            'komedia dell’arte; Przekonujący oddziałującego')

    def test_impossible_dates_and_times_stay_unchanged(self):
        self.assertEqual(correct_editorial_text('31.02.2025, 2025-13-01, godz. 25:00, 24:61 i o 9.30',
                                                {'dates_times'}),
                         '31.02.2025, 2025-13-01, godz. 25:00, 24:61 i o 09:30')

    def test_sentence_case_skips_ellipsis_ordinals_initials_and_file_names(self):
        self.assertEqual(correct_editorial_text('Koniec... dalej. 3. punkt. A. kowalski wrócił. Patrz np. tutaj. '
                                                'dalej', {'sentence_case'}),
                         'Koniec... dalej. 3. punkt. A. kowalski wrócił. Patrz np. tutaj. Dalej')

    def test_sentence_case_does_not_change_addresses_starting_a_sentence(self):
        """Regresja: adres na początku zdania nie dostaje wielkiej litery („Https://…”, „Jan@…”)."""
        for text in ('Zobacz to. https://example.com/x', 'Koniec! jan@example.com napisał'):
            with self.subTest(text=text):
                self.assertEqual(correct_editorial_text(text, {'sentence_case'}), text)

    def test_dashes_between_numbers_and_minus_signs_keep_their_spacing(self):
        self.assertEqual(correct_editorial_text('10–20 i (–5) oraz =–3, a dalej–tak', {'dash_spaces'}),
                         '10–20 i (–5) oraz =–3, a dalej – tak')

    def test_identifiers_are_not_grouped_like_quantities(self):
        self.assertEqual(
            correct_editorial_text('012345 osób, nr 123456, 123456789 i 123456789 osób, 12345 kg',
                                   {'digit_groups'}),
            '012345 osób, nr 123456, 123456789 i 123\u00a0456\u00a0789 osób, 12\u00a0345 kg')

    def test_technical_text_is_protected_from_default_rules(self):
        source = 'Adres: www.example.pl/strona?a=1. Napisz: jan@example.com, wersja v1.2.3, IP 192.168.0.1'
        self.assertEqual(self.fix(source), source)
        # Znak z obszaru prywatnego Unicode w tekście nie myli masek chroniących adresy.
        marked = '\U000f0000 www.example.pl'
        self.assertEqual(self.fix(marked), marked)

    def test_unknown_rule_is_refused(self):
        with self.assertRaisesMessage(ValueError, 'Nieznane reguły korekty'):
            correct_editorial_text('x', {'nieznana'})

    def test_edit_opcodes_fall_back_to_difflib(self):
        with patch.dict(sys.modules, {'rapidfuzz.distance': None}):
            opcodes = _edit_opcodes('Ala  ma', 'Ala ma')
        self.assertEqual(opcodes, SequenceMatcher(None, 'Ala  ma', 'Ala ma', autojunk=False).get_opcodes())


class ReplacementListTests(SimpleTestCase):
    """Słownik zamian jako dane: DEFAULT_REPLACEMENTS i własna lista `replacements`."""

    def fix(self, text, replacements=None):
        return correct_editorial_text(text, {'user_word_corrections'}, replacements=replacements)

    def test_default_list_is_data_equivalent_to_the_builtin_list(self):
        self.assertTrue(all(set(entry) == {'kind', 'source', 'target'} and entry['kind'] in ('word', 'pattern')
                            for entry in DEFAULT_REPLACEMENTS))
        self.assertEqual(len(DEFAULT_REPLACEMENTS), len(USER_WORD_EXACT) + len(USER_WORD_PATTERNS) + 1)
        json.dumps(DEFAULT_REPLACEMENTS)  # nadaje się do zapisania w CMS / konfiguracji zadania
        samples = ('Pickupem jechał niechcąco, PICKUPAMI; europejczykiem, Europejczykiem i EUROPEJCZYKIEM; '
                   'azjatami; komedia dellarte, KOMEDIA DELLARTE, KoMeDiA dEllArte, komediami Dellarte; '
                   'ZACZYM, Zaczym; MASSMEDIAMI; miałczeniem; nieprzekonywującego; wymyśleć; kanister')
        expected = ('Pikapem jechał niechcący, PIKAPAMI; Europejczykiem, Europejczykiem i EUROPEJCZYKIEM; '
                    'Azjatami; komedia dell’arte, KOMEDIA DELL’ARTE, KoMeDiA dell’arte, komediami Dell’arte; '
                    'ZA CZYM, Za czym; MASS MEDIAMI; miauczeniem; nieprzekonującego; wymyślić; kanister')
        self.assertEqual(self.fix(samples), expected)
        self.assertEqual(self.fix(samples, DEFAULT_REPLACEMENTS), expected)

    def test_custom_list_replaces_the_builtin_one(self):
        replacements = [{'kind': 'word', 'source': 'kot', 'target': 'pies'},
                        {'kind': 'word', 'source': 'nie wiem', 'target': 'nie mam pojęcia'}]
        self.assertEqual(self.fix('Kot, KOT i kot; kotek; pickup. Nie wiem.', replacements),
                         'Pies, PIES i pies; kotek; pickup. Nie mam pojęcia.')
        self.assertEqual(self.fix('pickup', []), 'pickup')

    def test_patterns_use_named_groups_and_capital_targets(self):
        replacements = [
            {'kind': 'pattern', 'source': r'\bwarszawiak(?P<ending>iem|owi|a|u)?\b', 'target': r'Warszawiak\g<ending>'},
            {'kind': 'pattern', 'source': r'\bwziąść\b', 'target': 'wziąć'},
            {'kind': 'word', 'source': 'polak', 'target': 'Polak'},
        ]
        self.assertEqual(self.fix('Z warszawiakiem, WARSZAWIAKOWI; Wziąść i WZIĄŚĆ; polak', replacements),
                         'Z Warszawiakiem, WARSZAWIAKOWI; Wziąć i WZIĄĆ; Polak')

    def test_invalid_and_costly_entries_are_skipped_without_stopping_the_cleaner(self):
        good = {'kind': 'word', 'source': 'kot', 'target': 'pies'}
        broken = (
            {'kind': 'pattern', 'source': '(kot', 'target': 'x'},                  # błąd kompilacji
            {'kind': 'pattern', 'source': r'\bkot\b', 'target': r'\g<brak>'},      # nieznana grupa
            {'kind': 'pattern', 'source': r'\bkot\b', 'target': '\\q'},            # zły znak ucieczki
            {'kind': 'pattern', 'source': r'(a+)+$', 'target': 'x'},               # zagnieżdżone powtórzenie
            {'kind': 'pattern', 'source': r'(?:ab|a)*c', 'target': 'x'},           # alternatywa w powtórzeniu
            {'kind': 'pattern', 'source': r'x*', 'target': 'y'},                   # pasuje do pustego napisu
            {'kind': 'pattern', 'source': 'a' * 501, 'target': 'x'},               # za długi wzorzec
            {'kind': 'word', 'source': 'ala', 'target': 'x' * 201},                # za długi target
            {'kind': 'regex', 'source': 'ala', 'target': 'x'},                     # nieznany rodzaj
            {'kind': 'word', 'source': '', 'target': 'x'},
            {'kind': 'word', 'source': 'ala', 'target': None},
            'ala=x',
        )
        text = 'Ala ma kota, kot ' + 'a' * 30 + '!'
        start = time.perf_counter()
        for entry in broken:
            with self.subTest(entry=str(entry)[:40]):
                self.assertEqual(self.fix(text, [entry, good]), text.replace('kot ', 'pies '))
        self.assertLess(time.perf_counter() - start, 2)
        # Bezpieczne powtórzenia i opcjonalne alternatywy są dozwolone.
        self.assertEqual(self.fix('kotów kocur', [
            {'kind': 'pattern', 'source': r'\bkot(?P<e>ów|a)?\b', 'target': r'pies\g<e>'},
            {'kind': 'pattern', 'source': r'\bkoc\w+', 'target': 'pies'}]), 'piesów pies')

    def test_replacements_reach_the_docx_cleaner(self):
        document = Document()
        document.add_paragraph('Pickup i kot.')
        result = Document(clean_docx(saved(document), ['user_word_corrections'],
                                     replacements=[{'kind': 'word', 'source': 'kot', 'target': 'pies'}]))
        self.assertEqual(result.paragraphs[0].text, 'Pickup i pies.')
        result = Document(clean_docx(saved(document), ['user_word_corrections']))
        self.assertEqual(result.paragraphs[0].text, 'Pikap i kot.')


class ChangeRegionTests(SimpleTestCase):
    """Granularność zmian (śledzenie zmian i podsumowanie): cały wyraz albo sama interpunkcja/odstęp."""

    def regions(self, original, corrected):
        return [(original[a:b], corrected[c:d]) for a, b, c, d in _change_regions(original, corrected)]

    def test_word_changes_cover_the_whole_word_and_spacing_changes_stay_minimal(self):
        self.assertEqual(self.regions('Jechał pickupem.', 'Jechał pikapem.'), [('pickupem', 'pikapem')])
        self.assertEqual(self.regions('Ala  ma kota...', 'Ala ma kota…'), [(' ', ''), ('...', '…')])
        self.assertEqual(self.regions('w 2025r. i a b', 'w 2025 r. i a b'), [('', ' '), (' ', ' ')])
        self.assertEqual(self.regions('1.2.2025', '01.02.2025'), [('1', '01'), ('2', '02')])
        self.assertEqual(self.regions('bez zmian', 'bez zmian'), [])

    def test_summary_counting_does_not_change_the_text(self):
        for text in ('Ala  ma kota... „tak” – rzekł. On  przyszedł,a potem 1.2.2025 r. 5-7 kg.', '   ', 'a'):
            counts = {}
            self.assertEqual(correct_editorial_text(text, FORM_DEFAULT_EDITORIAL_RULES, counts=counts),
                             correct_editorial_text(text, FORM_DEFAULT_EDITORIAL_RULES))


class EditorialRuleRegressionTests(SimpleTestCase):
    """Regresje znalezione w przeglądzie Odkurzacza (październik 2026)."""

    def fix(self, text, rules=FORM_DEFAULT_EDITORIAL_RULES):
        return correct_editorial_text(text, rules)

    def test_pronouns_in_capitals_stay_in_titles_and_shouting(self):
        """Napis wersalikami i partykuła „NIE” nie są wyróżnionym zaimkiem."""
        rules = {'pronouns_lower'}
        for text in ('ROZDZIAŁ PIERWSZY. ON I ONA', 'Krzyknął: NIE CHCĘ GO WIDZIEĆ!', 'Powiedziałem NIE.'):
            with self.subTest(text=text):
                self.assertEqual(correct_editorial_text(text, rules), text)
        self.assertEqual(correct_editorial_text('Widziałem JEGO, nie ją.', rules), 'Widziałem jego, nie ją.')

    def test_straight_quote_inside_polish_quote_becomes_second_level(self):
        self.assertEqual(self.fix('„Powiedział: "nie"” – zaśmiał się.', {'quotes'}),
                         '„Powiedział: «nie»” – zaśmiał się.')
        self.assertEqual(self.fix('"Tak" i "nie".', {'quotes'}), '„Tak” i „nie”.')

    def test_postal_code_is_not_a_number_range(self):
        self.assertEqual(self.fix('Ul. Polna 3, 00-950 Warszawa.', {'range_dash', 'range_spaces'}),
                         'Ul. Polna 3, 00-950 Warszawa.')
        self.assertEqual(self.fix('Od 10-200 osób.', {'range_dash'}), 'Od 10–200 osób.')

    def test_missing_space_before_capitalized_word_is_not_a_domain(self):
        """Regresja: „domu.Co”, „kinie.De” wyglądały jak adresy .co/.de i nie dostawały spacji."""
        for text, expected in (('Wrócił do domu.Co dalej?', 'Wrócił do domu. Co dalej?'),
                               ('Był w kinie.De facto nic.', 'Był w kinie. De facto nic.')):
            with self.subTest(text=text):
                self.assertEqual(self.fix(text, {'after_punct'}), expected)
        self.assertEqual(self.fix('Strona fantazmaty.pl i onet.com.pl.', {'after_punct', 'sentence_case'}),
                         'Strona fantazmaty.pl i onet.com.pl.')

    def test_dotted_technical_names_are_protected(self):
        text = 'Zobacz core.views.tags. dalej'
        self.assertEqual(self.fix(text, {'after_punct'}), text)
        self.assertEqual(self.fix(text, {'sentence_case'}), 'Zobacz core.views.tags. Dalej')

    def test_period_after_time_ends_the_sentence(self):
        self.assertEqual(self.fix('Wyszedł o 9:30. potem wrócił.', {'sentence_case'}),
                         'Wyszedł o 9:30. Potem wrócił.')
        self.assertEqual(self.fix('Rozdział 3. punkt 2.', {'sentence_case'}), 'Rozdział 3. punkt 2.')

    def test_ellipsis_between_words_gets_a_space(self):
        self.assertEqual(self.fix('Pół godziny…a potem cisza.', {'after_punct'}), 'Pół godziny… a potem cisza.')
        self.assertEqual(self.fix('…i wtedy, co?…', {'after_punct'}), '…i wtedy, co?…')

    def test_typewriter_double_hyphen_becomes_a_dash(self):
        rules = {'hyphen_dash', 'dash_spaces', 'range_dash'}
        for text, expected in (('Tak--powiedział.', 'Tak – powiedział.'), ('-- Wiem -- rzekł.', '– Wiem – rzekł.'),
                               ('Strony 10--20.', 'Strony 10–20.')):
            with self.subTest(text=text):
                self.assertEqual(self.fix(text, rules), expected)

    def test_typewriter_comma_quotes_become_polish_quotes(self):
        """Regresja: „,,tak''” było psute na „, tak''” przez regułę powtórzonych przecinków."""
        rules = {'quotes', 'duplicate_punct', 'after_punct'}
        self.assertEqual(self.fix("Mówił ,,tak'' i ,,nie\".", rules), 'Mówił „tak” i „nie”.')
        self.assertEqual(self.fix('Liczby a,, b.', rules), 'Liczby a, b.')

    def test_long_paragraph_with_many_numbers_pronouns_and_dashes_is_fast(self):
        """Reguły patrzą tylko na krótki kontekst; wcześniej koszt rósł z kwadratem długości akapitu."""
        source = ('– Nie wiem – powiedział Jan. On go widział nr 12345 i 67890 kg, o 9.30, ' * 300)[:20000]
        started = time.monotonic()
        result = self.fix(source)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertIn('nr\u00a012345', result)
        self.assertIn('67\u00a0890\u00a0kg', result)
        self.assertIn('. On go', result)
        self.assertIn('Jan. On go widział', result)


class ProgramFormTests(SimpleTestCase):
    def test_cleaner_rule_defaults_and_order(self):
        self.assertEqual(EDITORIAL_RULES[-1][0], 'empty_paragraphs')
        form = OdkurzaczForm()
        initial = set(form.fields['rules'].initial)
        self.assertNotIn('empty_paragraphs', initial)
        self.assertTrue({key for key, *_ in EDITORIAL_RULES} - {'empty_paragraphs'} <= initial)
        self.assertTrue(form.fields['remove_soft_whitespace'].initial)
        # Automatyczne czyszczenie poczty zachowuje dotychczasowe reguły.
        self.assertTrue(DEFAULT_EDITORIAL_RULES)

    def test_only_the_cleaner_offers_formatting_and_soft_whitespace_options(self):
        self.assertNotIn('remove_soft_whitespace', DocumentConversionForm(prefix='convert').fields)
        self.assertNotIn('normalize_formatting', DocumentConversionForm().fields)
        self.assertNotIn('normalize_formatting', RepetitionsForm().fields)

    def test_all_program_forms_accept_five_mb_and_reject_one_extra_byte(self):
        data = {'formats': ['pdf'], **REPETITION_PARAMETERS}
        for form_class in (OdkurzaczForm, DocumentConversionForm, RepetitionsForm):
            with self.subTest(form=form_class.__name__):
                form = form_class(data, {'document': sized_docx(5 * 1024 * 1024)})
                self.assertTrue(form.is_valid(), form.errors)
                form = form_class(data, {'document': sized_docx(5 * 1024 * 1024 + 1)})
                self.assertFalse(form.is_valid())
                self.assertIn('5 MB', str(form.errors['document']))

    def test_mailbox_validator_can_keep_its_existing_ten_mb_limit(self):
        form = OdkurzaczForm({}, {'document': sized_docx(5 * 1024 * 1024 + 1)},
                             max_document_bytes=10 * 1024 * 1024)
        self.assertTrue(form.is_valid(), form.errors)

    def test_zip_expansion_limit(self):
        stream = BytesIO()
        with ZipFile(stream, 'w', ZIP_DEFLATED) as archive:
            archive.writestr('word/document.xml', b'a' * (51 * 1024 * 1024))
            archive.writestr('[Content_Types].xml', b'')
        form = OdkurzaczForm({}, {'document': upload(stream.getvalue())})
        self.assertFalse(form.is_valid())
        self.assertIn('50 MB', str(form.errors))
