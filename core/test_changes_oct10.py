"""Zmiany z 10.10: zadania „Nie dotyczy”, blokada nagrywania antologii, spójność danych
dla Ekstraktów, opcje Odkurzacza, korekta poskładowa bez stron i style tabel."""
from pathlib import Path

from django.conf import settings
from django.core import signing
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from core.models import PostLayoutAssignment
from core.odkurzacz_forms import OdkurzaczForm
from core.services.odkurzacz import DEFAULT_EDITORIAL_RULES, EDITORIAL_RULES
from core.supervision import integrity_issues
from texts.models import Anthology, AnthologyTask, Text
from workflow.tests import create_member

STATIC = Path(settings.BASE_DIR) / 'core' / 'static' / 'core'


class NotApplicableTaskTests(TestCase):
    def setUp(self):
        self.member = create_member('na-member', 'Redaktor')
        self.book = Anthology.objects.create(title='Antologia bez banerów')

    def test_not_applicable_is_done_without_an_assignee(self):
        task = self.book.production_tasks.get(task_type='banners')
        task.assigned_to = self.member.person_profile
        task.status = AnthologyTask.Status.COMMISSIONED
        task.save()
        task.status = AnthologyTask.Status.NOT_APPLICABLE
        task.full_clean()
        task.save()
        task.refresh_from_db()
        self.assertIsNone(task.assigned_to_id)
        self.assertIsNone(task.commissioned_at)
        self.assertIn(task.status, AnthologyTask.DONE_STATUSES)
        self.assertEqual(task.get_status_display(), 'Nie dotyczy')

    def test_not_applicable_audio_description_keeps_its_stage(self):
        task = self.book.production_tasks.get(task_type='audio_description')
        task.status = AnthologyTask.Status.NOT_APPLICABLE
        task.save()
        description = self.book.audio_description
        description.content = 'Szkic'
        description.save()
        task.refresh_from_db()
        self.assertEqual(task.status, AnthologyTask.Status.NOT_APPLICABLE)


class BlockRecordingTests(TestCase):
    def setUp(self):
        self.manager = create_member('block-manager', 'Koordynator redakcji')
        self.book = Anthology.objects.create(title='Antologia do zablokowania')
        self.texts = [Text.objects.create(title=f'Opowiadanie {i}', anthology=self.book, length=1) for i in range(3)]
        self.url = reverse('core:anthology_detail', args=[self.book.pk])
        self.client.force_login(self.manager)

    def token(self):
        return signing.dumps([self.manager.pk, f'texts.anthology:{self.book.pk}', version_of(self.book)],
                             salt='cms-edit-version')

    def test_button_blocks_every_text_but_not_the_audio_description(self):
        page = html.fromstring(self.client.get(self.url).content)
        self.assertTrue(page.xpath('//button[@data-dialog-open="block-recording"][normalize-space()="Zablokuj nagrywanie"]'))
        task = self.book.production_tasks.get(task_type='audio_description')
        before = task.status
        response = self.client.post(self.url, {'action': 'block_recording', '_edit_version': self.token()})
        self.assertEqual(response.status_code, 302)
        for text in self.texts:
            text.refresh_from_db()
            self.assertTrue(text.audiobook_blacklisted)
            self.assertFalse(text.for_recording)
        task.refresh_from_db()
        self.assertEqual(task.status, before)
        self.assertContains(self.client.get(self.url), 'Wszystkie teksty są na czarnej liście audiobooków.')

    def test_block_requires_a_form_version(self):
        self.assertEqual(self.client.post(self.url, {'action': 'block_recording'}).status_code, 409)
        self.assertFalse(Text.objects.filter(audiobook_blacklisted=True).exists())


class ExtractsIntegrityTests(TestCase):
    def test_extracts_texts_without_author_are_not_reported(self):
        regular = Anthology.objects.create(title='Zwykła antologia')
        Text.objects.create(title='Bez autora zwykły', anthology=regular, length=1)
        for number in (1, 2, 3):
            book = Anthology.objects.create(title=f'Ekstrakty {number}')
            Text.objects.create(title=f'Ekstrakt {number}', anthology=book, length=1)
        found = {item['detail'] for item in integrity_issues() if item['label'] == 'Tekst bez autora'}
        self.assertIn('Bez autora zwykły', found)
        self.assertFalse({'Ekstrakt 1', 'Ekstrakt 2', 'Ekstrakt 3'} & found)


class OdkurzaczOptionTests(SimpleTestCase):
    def test_form_defaults_and_order(self):
        self.assertEqual(EDITORIAL_RULES[-1][0], 'empty_paragraphs')
        form = OdkurzaczForm()
        initial = set(form.fields['rules'].initial)
        self.assertNotIn('empty_paragraphs', initial)
        self.assertTrue({key for key, *_ in EDITORIAL_RULES} - {'empty_paragraphs'} <= initial)
        self.assertTrue(form.fields['remove_soft_whitespace'].initial)
        # Automatyczne czyszczenie poczty zachowuje dotychczasowe reguły.
        self.assertTrue(DEFAULT_EDITORIAL_RULES)


class PostLayoutWithoutPagesTests(TestCase):
    def setUp(self):
        self.manager = create_member('pl-manager', 'Koordynator redakcji')
        self.reader = create_member('pl-reader', 'Korektor poskładowy')
        self.book = Anthology.objects.create(title='Antologia do korekty')
        self.url = reverse('core:post_layout')
        self.client.force_login(self.manager)

    def test_assignment_is_created_without_pages_and_toolbar_has_three_cards(self):
        page = self.client.get(self.url)
        self.assertNotContains(page, 'name="page_from"')
        document = html.fromstring(page.content)
        self.assertEqual(len(document.xpath('//div[@class="post-layout-toolbar"]/section[@class="card"]')), 3)
        token = page.context['form'].initial['token']
        response = self.client.post(self.url, {'action': 'create', 'token': token, 'anthology': self.book.pk,
                                               'proofreader': self.reader.pk})
        self.assertEqual(response.status_code, 302)
        item = PostLayoutAssignment.objects.get()
        self.assertIsNone(item.page_from)
        self.assertEqual(item.pages_display, 'brak zakresu stron')


class StylesheetTests(SimpleTestCase):
    def css(self, name):
        return (STATIC / name).read_text(encoding='utf-8')

    def test_tables_bleed_to_card_edges(self):
        css = self.css('components.css')
        self.assertIn('margin-inline: calc(-1 * var(--table-bleed, var(--card-padding)))', css)
        self.assertNotIn('.post-layout-table-wrap {', self.css('post-layout.css'))

    def test_every_badge_has_a_border(self):
        css = self.css('components.css')
        self.assertIn('--badge-fg: var(--muted);\n    --badge-border: currentColor;', css)
        self.assertNotIn('--badge-border: transparent', css)

    def test_external_buttons_use_the_accent_colour(self):
        css = self.css('components.css')
        self.assertIn('.audiobook-public-button', css)
        for theme in ('--accent:', '--accent-hover:'):
            self.assertGreaterEqual(self.css('themes.css').count(theme), 3)


class AdBlockerSafeClassTests(SimpleTestCase):
    """Blokery reklam ukrywają elementy o klasach „ad-…” – audiodeskrypcja ich nie używa."""

    def test_no_ad_prefixed_classes(self):
        import re
        pattern = re.compile(r'(?<![\w-])ad-[a-z]')
        root = Path(settings.BASE_DIR) / 'core'
        for path in [*(root / 'templates').rglob('*.html'), *STATIC.glob('*.css')]:
            text = path.read_text(encoding='utf-8')
            classes = ' '.join(re.findall(r'class="([^"]*)"', text)) if path.suffix == '.html' else ' '.join(re.findall(r'\.[\w-]+', text))
            self.assertIsNone(pattern.search(classes.replace('.', ' ')), path.name)
