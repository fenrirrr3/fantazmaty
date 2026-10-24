"""Wytyczne stron zewnętrznych (audiobooki i ilustracje): parser znaczników i zwykłego tekstu, escapowanie
i dozwolone linki, PDF (struktura, polskie znaki, numery stron), widoki z pamięcią podręczną PDF i admin."""
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from fpdf import FPDF
from lxml import html

from core.models import PublicAudiobookSettings
from core.public_guidelines import SYNTAX_HELP, parse, rendered, to_html
from core.services import guidelines_pdf
from fantazmaty.testing import create_superuser
from illustrations.models import PublicIllustrationSettings

SAMPLE = """# Wymagania
Zażółć **gęślą** _jaźń_ – napisz na kontakt@example.test.
Druga linia: [formularz](https://example.test/formularz).

## Pliki
- WAV, 44,1 kHz
- mono
1. pierwszy
2. drugi"""


def html_of(text):
    return str(to_html(parse(text)))


class GuidelinesParserTests(SimpleTestCase):
    def test_markup_becomes_structured_html(self):
        """Znaczniki: nagłówki (# → h3, ## → h4, bo panel ma h2), akapity i pojedynczy Enter, listy z różnymi
        punktorami i numerami (także od innego numeru), wcięta kontynuacja punktu, pogrubienie, kursywa,
        zagnieżdżenia, linia oddzielająca i polskie znaki."""
        cases = [
            ('# Tytuł\n## Podtytuł', '<h3>Tytuł</h3>\n<h4>Podtytuł</h4>'),
            ('Pierwsza\ndruga\n\n\nNowy akapit', '<p>Pierwsza<br>druga</p>\n<p>Nowy akapit</p>'),
            ('- a\n* b\n• c\n– d', '<ul><li>a</li><li>b</li><li>c</li><li>d</li></ul>'),
            ('1. a\n2) b\n\n3. c', '<ol><li>a</li><li>b</li></ol>\n<ol start="3"><li>c</li></ol>'),
            ('- a\n1. b', '<ul><li>a</li></ul>\n<ol><li>b</li></ol>'),
            ('- punkt\n  ciąg dalszy\nAkapit po liście', '<ul><li>punkt<br>ciąg dalszy</li></ul>\n<p>Akapit po liście</p>'),
            ('**Zażółć** _gęślą_ **_jaźń_**',
             '<p><strong>Zażółć</strong> <em>gęślą</em> <strong><em>jaźń</em></strong></p>'),
            ('plik_nazwa_pliku.wav, 2*3*4 i __x__', '<p>plik_nazwa_pliku.wav, 2*3*4 i __x__</p>'),
            ('Przed\n---\n* * *\nPo', '<p>Przed</p>\n<hr>\n<hr>\n<p>Po</p>'),
            ('', ''),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(html_of(text), expected)

    def test_plain_text_headings_are_cautious(self):
        """Zwykły tekst bez „#”: krótka linia WERSALIKAMI → nagłówek, krótka linia z dwukropkiem na końcu →
        podtytuł; zdania, długie linie, linie z linkiem, skróty i tekst po dwukropku zostają akapitem.
        Gdy w tekście jest choć jeden nagłówek „#”, heurystyka jest wyłączona."""
        cases = [
            ('WYMAGANIA TECHNICZNE\nTekst.', '<h3>WYMAGANIA TECHNICZNE</h3>\n<p>Tekst.</p>'),
            ('Wstęp.\nFORMAT PLIKÓW\n- WAV', '<p>Wstęp.</p>\n<h3>FORMAT PLIKÓW</h3>\n<ul><li>WAV</li></ul>'),
            ('Format plików:\nWAV, mono.', '<h4>Format plików:</h4>\n<p>WAV, mono.</p>'),
            ('Uwaga: pliki wysyłamy w WAV.', '<p>Uwaga: pliki wysyłamy w WAV.</p>'),
            ('Nagraj tekst. Potem wyślij:', '<p>Nagraj tekst. Potem wyślij:</p>'),
            ('Bardzo długa linia, która kończy się dwukropkiem, ale jest zdaniem:',
             '<p>Bardzo długa linia, która kończy się dwukropkiem, ale jest zdaniem:</p>'),
            ('Pisz na a@example.test:', '<p>Pisz na <a href="mailto:a@example.test">a@example.test</a>:</p>'),
            ('MP3', '<p>MP3</p>'),
            ('# Tytuł\nKoordynacja:\nWERSALIKI TU', '<h3>Tytuł</h3>\n<p>Koordynacja:<br>WERSALIKI TU</p>'),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(html_of(text), expected)

    def test_everything_is_escaped_and_only_safe_links_are_kept(self):
        """Surowy HTML jest tekstem; linki tylko http, https i mailto (także sam adres e-mail i „www.”),
        inne schematy zostawiają samą etykietę; adresy w tekście stają się linkami bez kropki czy nawiasu
        kończącego zdanie; atrybutu nie da się dopisać przez cudzysłów w adresie."""
        cases = [
            ('<script>alert(1)</script> <b onclick="x">', '<p>&lt;script&gt;alert(1)&lt;/script&gt; '
                                                         '&lt;b onclick=&quot;x&quot;&gt;</p>'),
            ('[x](javascript:alert(1))', '<p>x</p>'),
            ('[x](JaVaScRiPt:alert)', '<p>x</p>'),
            ('[x](data:text/html,abc)', '<p>x</p>'),
            ('[x](//evil.example.test/)', '<p>x</p>'),
            ('[x](https://example.test/"onmouseover="alert(1))',
             '<p><a href="https://example.test/&quot;onmouseover=&quot;alert(1)" target="_blank" '
             'rel="noopener noreferrer">x</a></p>'),
            ('[x](https://example.test/a?b=1&c=(2))',
             '<p><a href="https://example.test/a?b=1&amp;c=(2)" target="_blank" rel="noopener noreferrer">x</a></p>'),
            ('[**Ważne** info](mailto:a@example.test?subject=Hej)',
             '<p><a href="mailto:a@example.test?subject=Hej"><strong>Ważne</strong> info</a></p>'),
            ('[napisz](a@example.test)', '<p><a href="mailto:a@example.test">napisz</a></p>'),
            ('Zobacz www.example.test, (https://example.test/x).',
             '<p>Zobacz <a href="https://www.example.test" target="_blank" rel="noopener noreferrer">www.example.test</a>, '
             '(<a href="https://example.test/x" target="_blank" rel="noopener noreferrer">https://example.test/x</a>).</p>'),
            ('Adres: jan_kowalski@example.test.',
             '<p>Adres: <a href="mailto:jan_kowalski@example.test">jan_kowalski@example.test</a>.</p>'),
            ('Bez linku: https://', '<p>Bez linku: https://</p>'),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(html_of(text), expected)

    def test_long_words_and_addresses_stay_whole(self):
        """Długi adres to jeden link (łamanie zostawiamy CSS-owi i PDF), długie słowo nie ginie; nawet wiele
        niedomkniętych znaczników w jednym wierszu pozostaje zwykłym tekstem."""
        url = 'https://example.test/' + 'a' * 300
        doc = html.fromstring(html_of(f'Plik: {url} i {"ż" * 500}'))
        self.assertEqual(doc.xpath('//a/@href'), [url])
        self.assertIn('ż' * 500, doc.text_content())
        unclosed = '**a [b _c ' * 2000
        self.assertEqual(html.fromstring(html_of(unclosed)).text_content(), unclosed.strip())

    def test_title_heading_is_not_repeated_and_blank_text_is_empty(self):
        """Pierwszy nagłówek równy tytułowi panelu (np. wklejony z dokumentu) nie jest powtarzany; inny zostaje.
        Same spacje i sam tytuł dają pusty stan."""
        title = 'Wytyczne dla ilustratorów'
        self.assertEqual(str(rendered(title, '# Wytyczne dla ilustratorów\n\nTekst')[1]), '<p>Tekst</p>')
        self.assertEqual(str(rendered(title, '# Organizacja\n\nTekst')[1]), '<h3>Organizacja</h3>\n<p>Tekst</p>')
        for text in ('  \n\t\n', '# Wytyczne dla ilustratorów'):
            with self.subTest(text=text):
                self.assertEqual(rendered(title, text), ((), ''))


class GuidelinesPdfTests(SimpleTestCase):
    def render(self, text, title='Wytyczne testowe'):
        """Zwraca (bajty PDF, wywołania write: (tekst, styl z „U” dla podkreślenia, rozmiar, link), teksty komórek)."""
        writes, cells = [], []
        original_write, original_cell = FPDF.write, FPDF.cell

        def write(pdf, h=None, text='', link='', **kwargs):
            writes.append((text, pdf.font_style + ('U' if pdf.underline else ''), pdf.font_size_pt, link))
            return original_write(pdf, h, text, link, **kwargs)

        def cell(pdf, *args, **kwargs):
            cells.append(kwargs.get('text', args[2] if len(args) > 2 else ''))
            return original_cell(pdf, *args, **kwargs)

        with patch.object(FPDF, 'write', write), patch.object(FPDF, 'cell', cell):
            content = guidelines_pdf.render_guidelines_pdf(parse(text), title)
        return content, writes, cells

    def test_same_structure_as_the_page(self):
        """Nagłówki pogrubione i większe od tekstu, pogrubienie i kursywa, podkreślone klikalne linki
        (https i mailto), punktory i numery list, polskie znaki bez braków w czcionce, numer strony w stopce."""
        with self.assertNoLogs('fpdf', 'WARNING'):
            content, writes, cells = self.render(SAMPLE)
        self.assertTrue(content.startswith(b'%PDF-'))
        styles = {text: (style, size, link) for text, style, size, link in writes}
        self.assertEqual(styles['Wymagania'][:2], ('B', 15))
        self.assertEqual(styles['Pliki'][:2], ('B', 12.5))
        self.assertEqual(styles['gęślą'][:2], ('B', 11.5))
        self.assertEqual(styles['jaźń'][0], 'I')
        self.assertEqual(styles['kontakt@example.test'], ('U', 11.5, 'mailto:kontakt@example.test'))
        self.assertEqual(styles['formularz'][2], 'https://example.test/formularz')
        for uri in (b'/URI (https://example.test/formularz)', b'/URI (mailto:kontakt@example.test)'):
            self.assertIn(uri, content)
        self.assertEqual([cell for cell in cells if cell in ('•', '1.', '2.')], ['•', '•', '1.', '2.'])
        self.assertIn('Wytyczne testowe · strona 1 z {nb0}', cells)
        self.assertEqual(re.findall(rb'/Count (\d+)', content), [b'1'])

    def test_long_text_breaks_pages_and_keeps_alias_and_long_words(self):
        """Długie wytyczne mają kolejne strony z numerami; tekst zawierający alias liczby stron go nie psuje;
        bardzo długi adres i słowo łamią się zamiast przerywać skład."""
        text = '\n\n'.join([f'Akapit {number} {{nb0}} ' + 'tekst ' * 80 for number in range(40)]
                           + ['https://example.test/' + 'a' * 400, 'ż' * 600])
        content, writes, cells = self.render(text)
        pages = int(re.findall(rb'/Count (\d+)', content)[0])
        self.assertGreater(pages, 3)
        self.assertIn(f'Wytyczne testowe · strona {pages} z {{nb1}}', cells)
        self.assertIn('Akapit 0 {nb0} ', [text[:len('Akapit 0 {nb0} ')] for text, *_ in writes])

    def test_link_after_text_moves_whole_to_the_next_line(self):
        """Adres e-mail (osobny fragment) po zwykłym tekście nie jest dzielony po literze na końcu wiersza:
        jeśli nie mieści się w reszcie wiersza, zaczyna następny."""
        starts = []
        original = FPDF.write

        def write(pdf, h=None, text='', link='', **kwargs):
            if link:
                room = pdf.w - pdf.r_margin - pdf.x
                starts.append(pdf.x <= pdf.l_margin + 0.01 or pdf.get_string_width(text) <= room)
            return original(pdf, h, text, link, **kwargs)

        text = '\n\n'.join('słowo ' * count + 'kontakt@example.test' for count in range(1, 30))
        with patch.object(FPDF, 'write', write):
            guidelines_pdf.render_guidelines_pdf(parse(text), 'Wytyczne testowe')
        self.assertEqual(len(starts), 29)
        self.assertTrue(all(starts))

    def test_missing_glyphs_are_reported_by_fpdf(self):
        """Polskie znaki są w czcionce; znak spoza niej (np. emoji) tylko zgłasza ostrzeżenie, PDF powstaje."""
        with self.assertLogs('fpdf', 'WARNING') as logs:
            content, _, _ = self.render('Zażółć ✓')
        self.assertTrue(content.startswith(b'%PDF-'))
        self.assertIn('✓', logs.output[0])
        self.assertNotIn('ż', logs.output[0])


class PublicGuidelinesPagesTests(TestCase):
    """Te same zachowania na obu stronach zewnętrznych: przycisk w rzędzie akcji, panel, PDF i admin."""
    PAGES = [
        {'name': 'audiobooki', 'model': PublicAudiobookSettings, 'page': 'core:external_audiobooks',
         'pdf': 'core:audiobook_guidelines_pdf', 'title': 'Wytyczne do nagrywania',
         'filename': 'wytyczne-audiobooki.pdf', 'admin': 'admin:core_publicaudiobooksettings_change'},
        {'name': 'ilustracje', 'model': PublicIllustrationSettings, 'page': 'illustrations:external_illustrations',
         'pdf': 'illustrations:illustration_guidelines_pdf', 'title': 'Wytyczne dla ilustratorów',
         'filename': 'wytyczne-ilustracje.pdf', 'admin': 'admin:illustrations_publicillustrationsettings_change'},
    ]

    @classmethod
    def setUpTestData(cls):
        # Migracja illustrations 0012 wpisuje wytyczne – testy zaczynają od pustych ustawień.
        PublicIllustrationSettings.objects.all().delete()
        cls.admin = create_superuser('guidelines-admin')
        cls.staff = get_user_model().objects.create_user('guidelines-staff', is_staff=True)

    def setUp(self):
        cache.clear()

    def panel(self, page):
        response = self.client.get(reverse(page['page']))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
        self.assertEqual(response['Referrer-Policy'], 'no-referrer')
        details = html.fromstring(response.content).xpath('//*[contains(@class, "actions")]/details')
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0].get('class'), 'guidelines')
        self.assertIsNone(details[0].getnext())  # ostatni w rzędzie przycisków
        return details[0]

    def test_empty_state_has_button_but_no_pdf(self):
        """Bez wytycznych: przycisk i panel z informacją o uzupełnieniu, bez „Pobierz PDF”; PDF – 404."""
        for page in self.PAGES:
            with self.subTest(page['name']):
                details = self.panel(page)
                summary = details.xpath('./summary')[0]
                self.assertIn('secondary-button', summary.get('class'))
                self.assertEqual([span.text for span in summary.xpath('./span[@data-label]')],
                                 ['Otwórz wytyczne', 'Zamknij wytyczne'])
                self.assertIsNone(details.get('open'))
                self.assertEqual(details.xpath('.//h2/text()'), [page['title']])
                self.assertEqual(details.xpath('.//article/p/text()'), ['Wytyczne zostaną uzupełnione.'])
                self.assertFalse(details.xpath('.//a[@download]'))
                self.assertEqual(self.client.get(reverse(page['pdf'])).status_code, 404)
                page['model'].objects.create(guidelines='  \n ')
                self.assertEqual(self.client.get(reverse(page['pdf'])).status_code, 404)

    def test_panel_shows_formatted_escaped_text_and_pdf_is_cached(self):
        """Treść w panelu jest sformatowana i escapowana; PDF ma typ, nazwę pliku, nagłówki stron zewnętrznych
        i powstaje raz dla danej treści (zmiana treści – nowy PDF); POST jest niedozwolony."""
        for page in self.PAGES:
            with self.subTest(page['name']):
                page['model'].objects.update_or_create(pk=1, defaults={'guidelines': SAMPLE + '\n<script>x</script>'})
                details = self.panel(page)
                article = details.xpath('.//article')[0]
                self.assertEqual(article.xpath('./h3/text() | ./h4/text()'), ['Wymagania', 'Pliki'])
                self.assertEqual(article.xpath('.//strong/text()'), ['gęślą'])
                self.assertEqual(article.xpath('.//a/@href'), ['mailto:kontakt@example.test',
                                                               'https://example.test/formularz'])
                self.assertIn('<script>x</script>', article.text_content())
                self.assertFalse(details.xpath('.//script'))
                link = details.xpath('.//a[@download]')[0]
                self.assertEqual((link.get('href'), link.text), (reverse(page['pdf']), 'Pobierz PDF'))
                with patch.object(guidelines_pdf, 'render_guidelines_pdf',
                                  wraps=guidelines_pdf.render_guidelines_pdf) as render:
                    for _ in range(2):
                        response = self.client.get(link.get('href'))
                        self.assertEqual(response.status_code, 200)
                    self.assertEqual(render.call_count, 1)
                    self.assertEqual(render.call_args.args[1], page['title'])
                    page['model'].objects.filter(pk=1).update(guidelines='Nowa treść')
                    self.assertTrue(self.client.get(link.get('href')).content.startswith(b'%PDF-'))
                    self.assertEqual(render.call_count, 2)
                self.assertEqual(response['Content-Type'], 'application/pdf')
                self.assertEqual(response['Content-Disposition'], f'attachment; filename="{page["filename"]}"')
                self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
                self.assertEqual(response['Referrer-Policy'], 'no-referrer')
                self.assertIn('no-store', response['Cache-Control'])
                self.assertEqual(self.client.post(link.get('href')).status_code, 405)

    def test_old_txt_address_redirects_to_pdf(self):
        """Dawny adres wytycznych TXT (linki mogły trafić do lektorów) przekierowuje na stały adres PDF."""
        response = self.client.get('/audiobooki/zewnetrzne/wytyczne.txt')
        self.assertEqual((response.status_code, response['Location']),
                         (301, reverse('core:audiobook_guidelines_pdf')))

    def test_admin_help_link_limit_and_superuser_only(self):
        """Admin (tylko superuser): ściąga składni przy polu, odnośnik do strony, limit 40 000 znaków
        i zapis widoczny na stronie zewnętrznej."""
        for page in self.PAGES:
            with self.subTest(page['name']):
                page['model'].objects.get_or_create(pk=1)
                url = reverse(page['admin'], args=[1])
                self.client.force_login(self.staff)
                self.assertIn(self.client.get(url).status_code, (302, 403))
                self.client.force_login(self.admin)
                response = self.client.get(url)
                self.assertContains(response, SYNTAX_HELP)
                doc = html.fromstring(response.content)
                self.assertEqual(doc.xpath('//a[text()="Zobacz na stronie"]/@href'), [reverse(page['page'])])
                form = doc.xpath('//form[contains(@id, "_form")]')[0]
                data = {field.get('name'): field.get('value', '') for field in form.xpath('.//input[@name]')
                        if field.get('type') not in ('submit', 'checkbox')}
                data.update(guidelines='x' * 40001, _save='Zapisz')
                errors = self.client.post(url, data).context['adminform'].form.errors
                self.assertIn('40000', errors['guidelines'][0])
                data['guidelines'] = '# Zapisane\nTreść z **admina**.'
                self.assertEqual(self.client.post(url, data).status_code, 302)
                self.client.logout()
                self.assertEqual(self.panel(page).xpath('.//article/h3/text()'), ['Zapisane'])
