"""Tabele Audiobooki i Korekta audiobooków (filtry, sortowanie, okresy etapów, liczba zapytań) oraz publiczna lista zewnętrzna."""
from datetime import date

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, tag
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from lxml import html

from authors.models import Author
from core.audiobook_validators import validate_mega_url
from core.edit_versions import version_of
from core.models import Audiobook, AudiobookStage, AudioContributor, PublicAudiobookSettings
from fantazmaty.testing import create_member
from people.models import Person, Role
from texts.models import Anthology, ForeignAuthor, Text, TextTranslation
from workflow.models import WorkflowStage

from .support import AudiobookProductionTestData


class ProductionTablesTests(AudiobookProductionTestData, TestCase):
    @tag('slow')
    def test_filters_sorting_pagination_and_query_count(self):
        self.start()
        self.finish()
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as single:
            self.client.get(self.queue_url)
        Text.objects.bulk_create([Text(title=f'Tekst {i:02}', anthology=self.book, length=1) for i in range(30)])
        # MySQL nie zwraca kluczy z bulk_create, więc teksty czytamy ponownie.
        texts = list(Text.objects.filter(anthology=self.book, title__startswith='Tekst '))
        AudiobookStage.objects.bulk_create([AudiobookStage(text=t, stage_type='proofreading', position=1)
                                            for t in texts])
        stages = {s.text_id: s for s in AudiobookStage.objects.filter(text__in=texts)}
        Audiobook.objects.bulk_create([Audiobook(text=t, status='proofreading', active_stage=stages[t.pk])
                                       for t in texts])
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.queue_url)
        self.assertLessEqual(len(many), len(single) + 2)
        self.assertEqual(response.context['page_obj'].paginator.count, 31)
        for route in (self.queue_url, self.list_url):
            page = self.client.get(route, {'q': 'Tekst', 'sort': '-title', 'page': 2})
            self.assertEqual([row['title'] for row in page.context['texts']], [f'Tekst {i:02}' for i in range(4, -1, -1)])
            for sort in ('author', 'status', '-status', 'narrator', 'engineer', 'proofreader', 'recording',
                         'proofreading', 'corrections', 'editing', 'premiere', 'youtube', 'hearthis', 'authors__email'):
                self.assertEqual(self.client.get(route, {'sort': sort}).status_code, 200)
        page = self.client.get(self.list_url, {'status': ['proofreading', 'corrections']})
        self.assertEqual(page.context['page_obj'].paginator.count, 31)
        self.assertContains(page, 'audio-status--proofreading')
        self.assertEqual(self.client.get(self.list_url, {'status': 'bad'}).context['page_obj'].paginator.count, 0)
        # Wiersz etapu nieznanego rodzaju (stare dane) nie psuje listy ani filtra kilku statusów.
        AudiobookStage.objects.create(text=self.text, stage_type='published', position=99, is_completed=True)
        page = self.client.get(self.list_url, {'q': '', 'status': ['proofreading', 'corrections', 'editing']})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(self.client.get(self.list_url, {'anthology': '9' * 100}).context['page_obj'].paginator.count, 0)

    def test_translated_author_and_empty_audio_visible(self):
        book = Anthology.objects.create(title='Tłumaczone', is_translated=True)
        text = Text.objects.create(title='Translated', anthology=book, length=1)
        translation, _ = TextTranslation.objects.get_or_create(text=text)
        translation.foreign_authors.add(ForeignAuthor.objects.create(first_name='Foreign', last_name='Author',
                                                                     pseudonym='Foreign Pen'))
        self.client.force_login(self.admin)
        page = self.client.get(self.list_url, {'q': 'Foreign Pen', 'sort': 'author'})
        self.assertEqual(page.context['page_obj'].paginator.count, 1)
        self.assertContains(page, 'Foreign Pen')
        self.assertContains(page, 'Do wzięcia')
        filtered = self.client.get(self.list_url, {'q': 'Foreign Pen', 'status': 'pending'})
        self.assertEqual(filtered.context['page_obj'].paginator.count, 1)
        self.assertFalse(Audiobook.objects.exists())

    def test_author_contact_and_profile_links_and_engineer_sorting(self):
        author = Author.objects.create(first_name='Jan', last_name='Autor', pseudonym='Pseudonim')
        text = Text.objects.create(title='Test nagrania', anthology=self.book, length=1)
        text.authors.add(author)
        narrator = AudioContributor.objects.create(name='Test Lektor')
        engineer = AudioContributor.objects.create(name='Test Montażysta')
        Audiobook.objects.create(text=text, status='proofreading', proofreader=self.reader,
                                 narrator_contact=narrator, narrator_name=narrator.name,
                                 engineer_contact=engineer, engineer_name=engineer.name)
        AudiobookStage.objects.create(text=text, performer=self.reader, stage_type='proofreading')
        self.client.force_login(create_member('audio-lists-manager', 'Koordynator korekty'))
        for name in ('core:audio_proofreading', 'core:audiobooks'):
            page = self.client.get(reverse(name), {'sort': 'engineer', 'q': 'Test nagrania'})
            self.assertEqual(page.status_code, 200)
            for url in (reverse('core:author_detail', args=[author.pk]),
                        reverse('core:anthology_detail', args=[self.book.pk]),
                        reverse('core:person_detail', args=[self.reader.person_profile.pk])):
                self.assertContains(page, url)
            contacts = [reverse('core:audio_contributor', args=[person.pk]) for person in (narrator, engineer)]
            if name == 'core:audiobooks':
                self.assertContains(page, 'Montaż')
                for url in contacts:
                    self.assertContains(page, url)
            else:
                for url in contacts:
                    self.assertNotContains(page, url)
            self.assertEqual(html.fromstring(page.content).xpath('//table//strong/text()'), [text.title])
            self.assertContains(page, 'Pseudonim')
            self.assertNotContains(page, 'Jan Autor')


class AudiobookTableTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('table-audio', 'Korektor audiobooków')
        cls.books = [Anthology.objects.create(title=f'Antologia {n}', status='ready') for n in range(3)]
        cls.texts = [Text.objects.create(title=f'Audio {n}', anthology=cls.books[n], length=1) for n in range(3)]
        cls.audios = [Audiobook.objects.create(text=t, status=status)
                      for t, status in zip(cls.texts, ('recording', 'editing', 'published'))]
        cls.url = reverse('core:audiobooks')

    def setUp(self):
        self.client.force_login(self.member)

    def test_multi_filters_combine_or_within_and_across_fields_and_survive_pagination(self):
        params = {'anthology': [self.books[0].pk, self.books[1].pk], 'status': ['recording', 'editing']}
        page = self.client.get(self.url, params)
        self.assertEqual({r['pk'] for r in page.context['texts']}, {t.pk for t in self.texts[:2]})
        doc = html.fromstring(page.content)
        self.assertEqual(len(doc.xpath('//select[@multiple]')), 2)
        self.assertEqual(len(doc.xpath('//select[@multiple]/option[@selected]')), 4)
        for value in ('invalid', '-1', '99999999999999999999999999999'):
            response = self.client.get(self.url, {**params, 'anthology': [self.books[0].pk, value]})
            self.assertEqual(response.context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.url, {**params, 'status': ['invalid']}).context['page_obj'].paginator.count, 0)
        for n in range(27):
            text = Text.objects.create(title=f'Kolejne {n}', anthology=self.books[0], length=1)
            Audiobook.objects.create(text=text, status='recording')
        page = self.client.get(self.url, {**params, 'page_size': '25', 'page': 2})
        self.assertEqual(page.context['page_obj'].paginator.count, 29)
        self.assertEqual(len(page.context['texts']), 4)
        self.assertContains(page, 'status=recording&amp;status=editing')

    def test_date_ranges_preserve_repeated_and_incomplete_history_and_legacy_start(self):
        audio = self.audios[0]
        for position, (start, end, kind, done) in enumerate([
                (None, date(2026, 1, 2), 'recording', True), (date(2026, 1, 4), None, 'proofreading', True),
                (date(2026, 2, 1), None, 'recording', False), (None, None, 'editing', False)], start=1):
            AudiobookStage.objects.create(text=audio.text, stage_type=kind, started_at=start, position=position,
                                          ended_at=end, is_completed=done)
        page = self.client.get(self.url, {'q': 'Audio 0'})
        row = list(page.context['texts'])[0]
        self.assertEqual(row['periods']['recording'], [
            {'started_at': None, 'ended_at': date(2026, 1, 2), 'is_completed': True},
            {'started_at': date(2026, 2, 1), 'ended_at': None, 'is_completed': False}])
        # Zakończona korekta bez daty końca; oczekujący montaż nie ma jeszcze okresu.
        self.assertEqual(row['periods']['proofreading'],
                         [{'started_at': date(2026, 1, 4), 'ended_at': None, 'is_completed': True}])
        self.assertEqual(row['periods']['editing'], [])
        self.assertNotContains(page, 'Czeka na publikację od')
        self.assertContains(page, '02.01.2026')
        self.assertEqual(len(html.fromstring(page.content).xpath('//table/thead/tr/th')), 14)
        for sort in ('recording', '-recording', 'proofreading', 'corrections', 'editing', 'engineer'):
            self.assertEqual(self.client.get(self.url, {'sort': sort}).status_code, 200)
        AudiobookStage.objects.create(text=self.texts[1], stage_type='recording', started_at=date(2026, 1, 15))
        params = {'anthology': [b.pk for b in self.books[:2]], 'sort': 'recording'}
        self.assertEqual([r['pk'] for r in self.client.get(self.url, params).context['texts']],
                         [self.texts[1].pk, self.texts[0].pk])

    def test_history_prefetch_query_count_does_not_grow_with_rows(self):
        self.client.get(self.url)  # Rozgrzanie sesji i uprawnień.
        with CaptureQueriesContext(connection) as few:
            self.client.get(self.url)
        for n in range(6):
            text = Text.objects.create(title=f'Historia {n}', anthology=self.books[0], length=1)
            Audiobook.objects.create(text=text)
            AudiobookStage.objects.create(text=text, stage_type='editing')
        with CaptureQueriesContext(connection) as more:
            self.client.get(self.url)
        self.assertEqual(len(few), len(more))


class ExternalAudiobookTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('audio-public-admin', 'admin@example.test', 'test')
        cls.member = get_user_model().objects.create_user('audio-public-member', is_staff=True)
        person = Person.objects.create(user=cls.member, first_name='Jan', last_name='Test')
        person.roles.add(Role.objects.get_or_create(name='Koordynator')[0])
        cls.book = Anthology.objects.create(title='Antologia gotowa', status=Anthology.Status.READY)
        cls.story = Text.objects.create(anthology=cls.book, title='Opowiadanie do nagrania', length=100,
                                        tags='kosmos, podróż', genre='science fiction',
                                        coordinator_note='PRYWATNE UWAGI', file_url='https://example.test/private-folder')
        cls.story.authors.add(Author.objects.create(first_name='Prywatne', last_name='Nazwisko',
                                                    email='private@example.test'))
        cls.url = reverse('core:external_audiobooks')
        cls.download = reverse('core:audiobook_guidelines_txt')

    def test_public_projection_is_read_only_and_internal_pages_stay_protected(self):
        for login in (False, True):
            if login:
                self.client.force_login(self.admin)
            response = self.client.get(self.url, {'sort': 'coordinator_note'})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, self.story.title)
            self.assertEqual(set(response.context['rows'][0]), {'anthology__title', 'title', 'tags', 'genre',
                                                                'public_status', 'audio_status',
                                                                'audiobook__narrator_name'})
            for value in ('PRYWATNE UWAGI', 'private-folder', 'private@example.test', 'Prywatne', 'site-sidebar'):
                self.assertNotContains(response, value)
            self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
            self.assertIn('no-store', response['Cache-Control'])
            self.assertEqual(self.client.post(self.url, {}).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('core:audiobooks')).status_code, 302)
        self.assertEqual(self.client.get(reverse('core:assigned_text_detail', args=[self.story.pk])).status_code, 302)
        self.assertFalse(PublicAudiobookSettings.objects.exists())

    def test_lists_narrator_and_everything_not_yet_published(self):
        Audiobook.objects.create(text=self.story, narrator_name='Ewa Głos', status='recording')
        published = Text.objects.create(anthology=self.book, title='Drugi', length=10)
        Audiobook.objects.create(text=published, status='published')
        page = self.client.get(self.url)
        self.assertContains(page, 'Ewa Głos')
        self.assertContains(page, '<th>Lektor</th>', html=True)
        self.assertEqual(page.context['page_obj'].paginator.count, 1)
        self.assertNotContains(page, 'Drugi')
        self.assertNotContains(page, '@')

    def test_all_scope_conditions_and_visibility_after_changes(self):
        preparing = Anthology.objects.create(title='Antologia w przygotowaniu')
        novel = Anthology.objects.create(title='Powieść gotowa', is_novel=True)
        # Dawna powieść oznaczona jako gotowa nadal jest poza zakresem audio.
        Anthology.objects.filter(pk=novel.pk).update(status=Anthology.Status.READY)
        for name, fields in [('Brak zgody', {'for_recording': False}), ('Czarna lista', {'audiobook_blacklisted': True}),
                             ('W przygotowaniu', {'anthology': preparing}), ('Bez antologii', {'anthology': None}),
                             ('Rozdział', {'anthology': novel, 'chapter_number': 1}), ('Wycofany', {})]:
            story = Text.objects.create(**{'anthology': self.book, 'title': name, 'length': 100, **fields})
            if name == 'Wycofany':
                WorkflowStage.objects.create(text=story, stage_type='withdrawn')
            if name == 'Rozdział':
                Text.objects.filter(pk=story.pk).update(for_recording=True)
        # Nawet niespójne dawne dane z oboma znacznikami pozostają prywatne.
        Text.objects.filter(title='Czarna lista').update(for_recording=True)
        response = self.client.get(self.url)
        self.assertEqual([row['title'] for row in response.context['rows']], [self.story.title])
        self.assertEqual(list(response.context['anthologies']),
                         [{'anthology_id': self.book.pk, 'anthology__title': self.book.title}])
        self.story.for_recording = False
        self.story.save(update_fields=['for_recording'])
        self.assertEqual(self.client.get(self.url).context['page_obj'].paginator.count, 0)

    def test_historical_withdrawal_does_not_hide_current_cycle(self):
        WorkflowStage.objects.create(text=self.story, stage_type='withdrawn', workflow_cycle=1, is_current=False)
        self.story.current_workflow_cycle = 2
        self.story.save(update_fields=['current_workflow_cycle'])
        self.assertContains(self.client.get(self.url), self.story.title)

    def test_filters_sorting_and_pagination_cover_only_eligible_texts(self):
        Text.objects.bulk_create([Text(title=f'Tekst {i:02}', anthology=self.book, length=1, tags='smoki',
                                       genre='fantasy') for i in range(30)])
        response = self.client.get(self.url, {'q': 'smoki', 'anthology': self.book.pk, 'sort': '-title', 'page': 2})
        self.assertEqual(response.context['page_obj'].paginator.count, 30)
        self.assertEqual([r['title'] for r in response.context['rows']], [f'Tekst {i:02}' for i in range(4, -1, -1)])
        self.assertIn('q=smoki', response.context['page_obj'].first_url)
        for invalid in ('-1', '1 OR 1=1', '9' * 100, '１２'):
            self.assertEqual(self.client.get(self.url, {'anthology': invalid}).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.url, {'status': 'Wycofany'}).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.url, {'q': 'PODRÓŻ'}).context['page_obj'].paginator.count, 1)
        for sort in ('anthology', 'title', 'tags', 'genre', 'status', 'authors__email'):
            self.assertEqual(self.client.get(self.url, {'sort': '-' + sort}).status_code, 200)

    def test_guidelines_escape_html_and_download_original_utf8_text(self):
        page = self.client.get(self.url)
        self.assertContains(page, 'https://mega.nz/')
        self.assertContains(page, 'Wytyczne zostaną uzupełnione.')
        self.assertEqual(self.client.get(self.download).status_code, 404)
        content = 'Wytyczne: zażółć gęślą jaźń.\n<script>alert(1)</script>\nDrugi akapit.'
        PublicAudiobookSettings.objects.create(mega_url='https://mega.nz/folder/example#key', guidelines=content)
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertContains(response, 'https://mega.nz/folder/example#key')
        self.assertFalse(html.fromstring(response.content).xpath('//details[@class="audiobook-guidelines"][@open]'))
        response = self.client.get(self.download)
        self.assertEqual(response.content.decode('utf-8'), content)
        self.assertEqual(response['Content-Type'], 'text/plain; charset=utf-8')
        self.assertIn('attachment;', response['Content-Disposition'])
        self.assertEqual(self.client.post(self.download, {}).status_code, 405)

    def test_only_safe_mega_urls_are_accepted_and_rendered(self):
        for url in ('javascript:alert(1)', 'http://mega.nz/', 'https://mega.nz.evil.test/', 'https://mega.nz@evil.test/',
                    'https://user@mega.nz/', 'https://mega.nz:8443/'):
            with self.subTest(url=url), self.assertRaises(ValidationError):
                validate_mega_url(url)
        config = PublicAudiobookSettings.objects.create()
        PublicAudiobookSettings.objects.filter(pk=config.pk).update(mega_url='javascript:alert(1)')
        self.assertNotContains(self.client.get(self.url), 'javascript:')
        with transaction.atomic(), self.assertRaises(IntegrityError):
            PublicAudiobookSettings.objects.create(pk=2)

    def test_admin_settings_permissions_versioning_and_navigation(self):
        config = PublicAudiobookSettings.objects.create()
        url = reverse('admin:core_publicaudiobooksettings_change', args=[1])
        self.client.force_login(self.member)
        self.assertIn(self.client.get(url).status_code, (302, 403))
        self.assertNotContains(self.client.get(reverse('core:audiobooks')), 'MegaNZ i wytyczne – ustawienia')
        self.client.force_login(self.admin)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        data = {node.get('name'): node.get('value', '') for node in
                doc.xpath('//form[@id="publicaudiobooksettings_form"]//input[@type="hidden"][@name]')}
        data.update(mega_url='https://mega.nz/folder/demo#key', guidelines='Nowe wytyczne', _save='Zapisz')
        before = version_of(config)
        self.assertEqual(self.client.post(url, data).status_code, 302)
        config.refresh_from_db()
        self.assertEqual(config.guidelines, 'Nowe wytyczne')
        self.assertNotEqual(version_of(config), before)
        self.assertEqual(self.client.post(url, data).status_code, 409)
        doc = html.fromstring(self.client.get(reverse('core:audiobooks')).content)
        link = doc.xpath(f'//a[@href="{self.url}"]')[0]
        self.assertEqual(link.get('target'), '_blank')
        self.assertIn('noopener', link.get('rel'))
        self.assertNotContains(self.client.get(reverse('core:home')), self.url)
        index = html.fromstring(self.client.get(reverse('admin:index')).content)
        self.assertTrue(index.xpath('//*[@id="group-audio"]//a[contains(@href,"zewnetrzne-audiobooki")]'))
