"""Produkcja audiobooka: etapy jak w procesie tekstów, decyzje koordynatora, Korekta audiobooków."""
from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.core import signing
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from core.edit_versions import version_of
from core.models import Audiobook, AudiobookStage
from texts.models import Anthology, Text, TextTranslation, ForeignAuthor
from workflow.models import WorkflowStage
from workflow.tests import create_member


class AudiobookProductionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('audio-manager', 'audio@example.test', 'test')
        cls.coordinator = create_member('audio-coordinator', 'Koordynator')
        cls.reader = create_member('audio-reader', 'Korektor audiobooków')
        cls.other_reader = create_member('other-reader', 'Korektor audiobooków')
        cls.member = create_member('audio-editor', 'Redaktor')
        cls.book = Anthology.objects.create(title='Audiobookowa antologia', status=Anthology.Status.READY)
        cls.text = Text.objects.create(title='Głos', anthology=cls.book, length=1234, tags='smoki', genre='fantasy')
        cls.text.authors.add(Author.objects.create(first_name='Prywatne', last_name='Nazwisko', pseudonym='Podpis'))
        cls.url = reverse('core:audiobook_detail', args=[cls.text.pk])
        cls.list_url = reverse('core:audiobooks')
        cls.queue_url = reverse('core:audio_proofreading')
        cls.assign_url = reverse('core:assign_audio_proofreader', args=[cls.text.pk])
        cls.claim_url = reverse('core:claim_audio_proofreading', args=[cls.text.pk])
        cls.public = reverse('core:external_audiobooks')

    def token(self, user=None):
        return signing.dumps([(user or self.admin).pk, f'texts.text:{self.text.pk}', version_of(self.text)], salt='cms-edit-version')

    def post(self, action, user=None, **fields):
        user = user or self.admin
        self.client.force_login(user)
        return self.client.post(self.url, {'action': action, '_edit_version': self.token(user), **fields})

    def start(self, user=None, day=None):
        fields = {} if day is False else {'stage-date': str(day or timezone.localdate()) if day != '' else ''}
        return self.post('start_stage', user, **fields)

    def finish(self, user=None, day=None):
        audio = Audiobook.objects.get(text=self.text)
        fields = {'stage_id': audio.active_stage_id}
        if day is not False:
            fields['stage-date'] = '' if day == '' else str(day or timezone.localdate())
        return self.post('finish_stage', user, **fields)

    def audio(self):
        return Audiobook.objects.get(text=self.text)

    def assign(self, person, user=None):
        user = user or self.admin
        self.client.force_login(user)
        return self.client.post(self.assign_url, {'proofreader': person.pk if person else '', '_edit_version': self.token(user)})

    def claim(self, user):
        self.client.force_login(user)
        return self.client.post(self.claim_url, {'_edit_version': self.token(user)})

    def test_preview_and_people_form_preserve_editorial_workflow(self):
        self.client.force_login(self.coordinator)
        count = get_user_model().objects.count()
        page = self.client.get(self.url)
        self.assertContains(page, 'Podpis')
        self.assertFalse(Audiobook.objects.exists())
        self.assertNotContains(page, 'name="proofreader"')
        self.assertContains(page, 'audio-detail-columns')
        response = self.post('people', self.coordinator, **{'people-narrator_name': 'Lektor bez konta',
            'people-narrator_email': 'private-audio@example.test', 'people-engineer_name': 'Dźwiękowiec bez konta',
            'status': 'published', 'proofreader': self.reader.pk})
        self.assertEqual(response.status_code, 302)
        audio = self.audio()
        self.assertEqual(audio.status, 'pending')
        self.assertIsNone(audio.proofreader_id)
        self.assertFalse(AudiobookStage.objects.exists())
        self.assertEqual(get_user_model().objects.count(), count)
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())
        self.client.force_login(self.member)
        self.assertNotContains(self.client.get(self.url), 'private-audio@example.test')
        self.assertEqual(self.post('people', self.member).status_code, 403)

    def test_full_production_in_fixed_order_and_publication(self):
        today = timezone.localdate()
        self.assertEqual(self.client.get(self.public).context['page_obj'].paginator.count, 1)
        # Do wzięcia → Nagrywanie: rozpoczęcie nagrywania to wzięcie tekstu.
        self.assertEqual(self.start(self.reader).status_code, 403)
        self.assertEqual(self.start().status_code, 302)
        audio = self.audio()
        self.assertEqual((audio.status, audio.active_stage.started_at), ('recording', today))
        self.assertEqual(self.finish().status_code, 302)
        # Koniec nagrywania udostępnia korektę; rozpoczyna ją korektor, przejmując ją.
        audio = self.audio()
        self.assertEqual((audio.status, audio.active_stage.queued_at, audio.active_stage.is_started),
                         ('proofreading', today, False))
        self.assertEqual(self.claim(self.reader).status_code, 302)
        audio = self.audio()
        self.assertEqual((audio.status, audio.proofreader, audio.active_stage.performer, audio.active_stage.started_at),
                         ('proofreading', self.reader, self.reader, today))
        self.assertEqual(self.finish(self.reader, day=False).status_code, 302)
        # Koniec korekty – gotowe do nanoszenia poprawek, potem montaż.
        for status in ('corrections', 'editing'):
            audio = self.audio()
            self.assertEqual((audio.status, audio.active_stage.is_started), (status, False))
            self.assertEqual(self.start(self.reader).status_code, 403)
            self.assertEqual(self.start().status_code, 302)
            self.assertEqual(self.finish().status_code, 302)
        # Koniec montażu – Opublikowane; data premiery i linki uzupełnia się w Publikacji.
        audio = self.audio()
        self.assertEqual((audio.status, audio.active_stage_id, audio.is_published), ('published', None, True))
        stages = list(AudiobookStage.objects.filter(text=self.text).order_by('position'))
        self.assertEqual([s.stage_type for s in stages], ['recording', 'proofreading', 'corrections', 'editing'])
        self.assertTrue(all(s.is_completed and s.started_at == today and s.ended_at == today for s in stages))
        self.assertContains(self.client.get(self.url), 'Uzupełnij datę premiery')
        self.assertEqual(self.post('publication', **{'publication-premiere_date': str(today),
            'publication-youtube_url': 'https://youtu.be/film', 'publication-hearthis_url': ''}).status_code, 302)
        self.assertEqual(self.client.get(self.public).context['page_obj'].paginator.count, 0)
        table = html.fromstring(self.client.get(self.list_url).content)
        self.assertEqual(len(table.xpath('//a[@href="https://youtu.be/film"]')), 1)
        self.assertEqual(self.client.get(self.list_url, {'status': 'published'}).context['page_obj'].paginator.count, 1)
        # Usunięcie linków nie cofa publikacji – wynika ona z zakończonego montażu.
        self.post('publication', **{'publication-premiere_date': '', 'publication-youtube_url': '', 'publication-hearthis_url': ''})
        self.assertEqual(self.audio().status, 'published')

    def test_optional_dates_only_for_coordinator(self):
        # Koordynator może zostawić datę pustą – „data nieznana”.
        self.assertEqual(self.start(day='').status_code, 302)
        stage = self.audio().active_stage
        self.assertEqual((stage.started_at, stage.start_unknown, stage.is_started), (None, True, True))
        self.assertEqual(self.finish(day='').status_code, 302)
        stage.refresh_from_db()
        self.assertEqual((stage.ended_at, stage.is_completed), (None, True))
        # Korekta dostępna od dziś (koniec nagrywania nieznany).
        self.assertEqual(self.audio().active_stage.queued_at, timezone.localdate())
        page = html.fromstring(self.client.get(self.url).content)
        row = page.xpath('//table/tbody/tr[td[1][starts-with(normalize-space(.),"Nagrywanie")]]')[0]
        self.assertEqual(["".join(td.itertext()).strip() for td in row.xpath('./td')][2:], ['?', '?', 'Zakończony'])
        # Korektor nie wybiera daty: zawsze dzisiaj.
        self.claim(self.reader)
        self.assertEqual(self.finish(self.reader, day='').status_code, 302)
        done = AudiobookStage.objects.get(text=self.text, stage_type='proofreading')
        self.assertEqual(done.ended_at, timezone.localdate())

    def test_stage_validation_and_stale_or_repeated_posts(self):
        self.assertEqual(self.start(day=timezone.localdate() + timedelta(days=1)).status_code, 400)
        self.assertFalse(AudiobookStage.objects.exists())
        self.assertEqual(self.start().status_code, 302)
        self.assertEqual(self.start().status_code, 400)  # już trwa
        stale = self.token()
        self.assertEqual(self.post('finish_stage', stage_id=999999).status_code, 400)
        self.assertEqual(self.finish(day=timezone.localdate() - timedelta(days=1)).status_code, 400)
        self.assertEqual(self.finish().status_code, 302)
        self.assertEqual(self.client.post(self.url, {'action': 'start_stage', '_edit_version': stale}).status_code, 409)
        self.assertEqual(self.client.post(self.url, {'action': 'start_stage'}).status_code, 409)
        # Dawne decyzje i wstrzymanie nie istnieją.
        for action in ('queue', 'pause'):
            self.assertEqual(self.post(action).status_code, 400)
        self.assertEqual(AudiobookStage.objects.count(), 2)

    def test_proofreading_tab_scope_assignment_and_own_finish(self):
        self.start()
        self.finish()
        other_book = Anthology.objects.create(title='Antologia innego korektora')
        other = Text.objects.create(title='Cudzy audiobook', anthology=other_book, length=1)
        mine = Audiobook.objects.create(text=other, proofreader=self.other_reader)
        busy = AudiobookStage.objects.create(text=other, stage_type='proofreading', position=1,
                                             started_at=timezone.localdate(), performer=self.other_reader)
        mine.active_stage, mine.status = busy, 'proofreading'
        mine.save()
        not_correction = Text.objects.create(title='Trwa montaż', anthology=self.book, length=1)
        Audiobook.objects.create(text=not_correction, status='editing', proofreader=self.reader)
        for user in (self.admin, self.coordinator):
            self.client.force_login(user)
            page = self.client.get(self.queue_url)
            self.assertEqual(page.context['page_obj'].paginator.count, 2)
            self.assertContains(page, 'name="proofreader"')
        # Wolna korekta jest widoczna dla każdego korektora, cudza w toku – nie.
        self.client.force_login(self.reader)
        page = self.client.get(self.queue_url)
        self.assertEqual([r['pk'] for r in page.context['texts']], [self.text.pk])
        self.assertContains(page, 'Przejmij')
        self.assertNotContains(page, 'Cudzy audiobook')
        self.assertNotContains(page, 'name="proofreader"')
        # Koordynator przypisuje oczekującą korektę – korektor rozpoczyna ją sam.
        self.assertEqual(self.assign(self.reader).status_code, 302)
        self.assertEqual(self.assign(self.other_reader, self.reader).status_code, 403)
        self.assertEqual(self.claim(self.other_reader).status_code, 302)  # komunikat: niedostępna
        self.assertEqual(self.audio().proofreader, self.reader)
        self.assertFalse(self.audio().active_stage.is_started)
        self.assertEqual(self.start(self.reader, day=False).status_code, 302)
        audio = self.audio()
        self.assertEqual((audio.status, audio.active_stage.started_at), ('proofreading', timezone.localdate()))
        self.assertEqual(self.finish(self.other_reader, day=False).status_code, 403)
        self.assertEqual(self.finish(self.reader, day=False).status_code, 302)
        self.assertEqual(self.start(self.reader, day=False).status_code, 403)  # poprawki – koordynator
        self.client.force_login(self.reader)
        self.assertEqual(self.client.get(self.queue_url).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.queue_url, {'hide_completed': '0'}).context['page_obj'].paginator.count, 1)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.queue_url).status_code, 403)
        doc = html.fromstring(self.client.get(self.list_url).content)
        self.assertFalse(doc.xpath(f'//nav//a[@href="{self.queue_url}"]|//aside//a[@href="{self.queue_url}"]'))
        self.client.logout()
        self.assertEqual(self.client.get(self.queue_url).status_code, 302)

    def test_assignment_validates_roles_inactive_users_and_status(self):
        self.start()
        self.assertEqual(self.assign(self.reader).status_code, 404)
        self.finish()
        self.assertEqual(self.assign(self.member).status_code, 400)
        self.reader.is_active = False
        self.reader.save(update_fields=['is_active'])
        self.assertEqual(self.assign(self.reader).status_code, 400)
        self.reader.is_active = True
        self.reader.save(update_fields=['is_active'])
        self.assertEqual(self.assign(self.reader).status_code, 302)
        stale = self.token()
        self.assertEqual(self.assign(self.other_reader).status_code, 302)
        self.assertEqual(self.client.post(self.assign_url, {'proofreader': self.reader.pk, '_edit_version': stale}).status_code, 409)
        self.assertEqual(self.audio().proofreader_id, self.other_reader.pk)
        # Zmiana korektora trwającej korekty przenosi też wykonawcę etapu.
        self.start()
        self.assertEqual(self.assign(self.reader).status_code, 302)
        self.assertEqual(self.audio().active_stage.performer, self.reader)
        self.assertEqual(self.post('publication', **{'publication-youtube_url': 'javascript:alert(1)'}).status_code, 400)
        self.assertEqual(self.post('publication', **{'publication-hearthis_url': 'https://hearthis.at.evil.test/file'}).status_code, 400)

    def test_disabled_and_withdrawn_preserve_history_and_forbid_mutations(self):
        self.start()
        self.finish()
        self.assign(self.reader)
        self.start()
        stage = self.audio().active_stage
        Text.objects.filter(pk=self.text.pk).update(audiobook_blacklisted=True)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(self.list_url).context['page_obj'].paginator.count, 0)
        self.assertEqual(self.client.get(self.queue_url).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.finish().status_code, 403)
        self.assertEqual(self.assign(None).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        Text.objects.filter(pk=self.text.pk).update(audiobook_blacklisted=False, for_recording=False)
        self.assertEqual(self.finish().status_code, 403)
        Text.objects.filter(pk=self.text.pk).update(for_recording=True)
        WorkflowStage.objects.create(text=self.text, stage_type='withdrawn')
        self.assertEqual(self.finish().status_code, 403)
        self.assertEqual(self.start().status_code, 403)
        self.assertEqual(self.audio().active_stage_id, stage.pk)
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        chapter = Text.objects.create(title='Rozdział 1', anthology=novel, chapter_number=1, length=1)
        self.assertEqual(self.client.get(reverse('core:audiobook_detail', args=[chapter.pk])).status_code, 404)

    def test_filters_sorting_pagination_and_query_count(self):
        self.start()
        self.finish()
        self.client.force_login(self.admin)
        with CaptureQueriesContext(connection) as single:
            self.client.get(self.queue_url)
        Text.objects.bulk_create([Text(title=f'Tekst {i:02}', anthology=self.book, length=1) for i in range(30)])
        # MySQL nie zwraca kluczy z bulk_create, więc teksty czytamy ponownie.
        texts = list(Text.objects.filter(anthology=self.book, title__startswith='Tekst '))
        AudiobookStage.objects.bulk_create([AudiobookStage(text=t, stage_type='proofreading', position=1) for t in texts])
        stages = {s.text_id: s for s in AudiobookStage.objects.filter(text__in=texts)}
        Audiobook.objects.bulk_create([Audiobook(text=t, status='proofreading', active_stage=stages[t.pk]) for t in texts])
        with CaptureQueriesContext(connection) as many:
            response = self.client.get(self.queue_url)
        self.assertLessEqual(len(many), len(single) + 2)
        self.assertEqual(response.context['page_obj'].paginator.count, 31)
        for route in (self.queue_url, self.list_url):
            page = self.client.get(route, {'q': 'Tekst', 'sort': '-title', 'page': 2})
            self.assertEqual([row['title'] for row in page.context['texts']], [f'Tekst {i:02}' for i in range(4, -1, -1)])
            for sort in ('author', 'status', '-status', 'narrator', 'engineer', 'proofreader', 'recording', 'proofreading',
                         'corrections', 'editing', 'premiere', 'youtube', 'hearthis', 'authors__email'):
                self.assertEqual(self.client.get(route, {'sort': sort}).status_code, 200)
        self.assertEqual(self.client.get(self.list_url, {'status': ['proofreading', 'corrections']}).context['page_obj'].paginator.count, 31)
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
        translation.foreign_authors.add(ForeignAuthor.objects.create(first_name='Foreign', last_name='Author', pseudonym='Foreign Pen'))
        self.client.force_login(self.admin)
        page = self.client.get(self.list_url, {'q': 'Foreign Pen', 'sort': 'author'})
        self.assertEqual(page.context['page_obj'].paginator.count, 1)
        self.assertContains(page, 'Foreign Pen')
        self.assertContains(page, 'Do wzięcia')
        self.assertEqual(self.client.get(self.list_url, {'q': 'Foreign Pen', 'status': 'pending'}).context['page_obj'].paginator.count, 1)
        self.assertFalse(Audiobook.objects.exists())

    def test_admin_preserves_status_and_corrects_history_dates(self):
        self.start(day='')
        audio = self.audio()
        url = reverse('admin:core_audiobook_change', args=[audio.pk])
        self.client.force_login(self.admin)
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="status"')
        token = html.fromstring(page.content).xpath('//input[@name="_edit_version"]/@value')[0]
        stale = self.token()
        data = {'status': 'editing', 'engineer_name': 'Montażysta', 'proofreader': self.reader.pk, '_edit_version': token, '_save': 'Zapisz'}
        self.assertEqual(self.client.post(url, data).status_code, 302)
        audio.refresh_from_db()
        self.assertEqual(audio.status, 'recording')
        self.assertEqual(audio.engineer_name, 'Montażysta')
        self.assertEqual(self.client.post(self.url, {'action': 'people', '_edit_version': stale}).status_code, 409)
        self.assertContains(self.client.get(reverse('admin:index')), 'Historia etapów audiobooków')
        history = reverse('admin:core_audiobookstage_change', args=[audio.active_stage_id])
        page = self.client.get(history)
        self.assertEqual(page.status_code, 200)
        fields = {'queued_at': '', 'started_at': '2026-01-05', 'start_unknown': 'on', 'ended_at': '', 'performer': '', '_save': 'Zapisz'}
        token = html.fromstring(page.content).xpath('//input[@name="_edit_version"]/@value')
        if token:
            fields['_edit_version'] = token[0]
        # Trwający etap nie dostaje końca z panelu – kończy się go na stronie audiobooka.
        self.assertEqual(self.client.post(history, {**fields, 'ended_at': '2026-01-06'}).status_code, 200)
        self.assertEqual(self.client.post(history, fields).status_code, 302)
        stage = AudiobookStage.objects.get(pk=audio.active_stage_id)
        self.assertEqual((stage.started_at, stage.start_unknown, stage.is_completed), (date(2026, 1, 5), False, False))
        self.assertEqual(self.audio().status, 'recording')


class AudiobookWorkflowMigrationTests(TransactionTestCase):
    """0042: dotychczasowe dane (także niepełne) przechodzą na etapy bez wymyślania dat."""

    def test_legacy_rows_from_production_table(self):
        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        executor.migrate([('core', '0041_audiobook_workflow_fields')])
        try:
            apps = MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps
            Anthology_ = apps.get_model('texts', 'Anthology')
            Text_ = apps.get_model('texts', 'Text')
            Audio = apps.get_model('core', 'Audiobook')
            Stage = apps.get_model('core', 'AudiobookStage')
            book = Anthology_.objects.create(title='Migracja audiobooków', status='ready')
            made = {}

            def audio(name, status, stages=(), active=None, **fields):
                text = Text_.objects.create(title=name, anthology=book, length=1)
                row = Audio.objects.create(text=text, status=status, **fields)
                created = [Stage.objects.create(text=text, stage_type=kind, started_at=start, ended_at=end,
                                                is_completed=done) for kind, start, end, done in stages]
                if active is not None:
                    row.active_stage = created[active]
                    row.save()
                made[name] = text.pk

            audio('opublikowany', 'published', premiere_date=date(2025, 3, 1), youtube_url='https://youtu.be/a')
            audio('publikacja jako etap', 'published', [('published', date(2025, 4, 2), None, True)])
            audio('czeka na publikację', 'awaiting_publication', [
                ('recording', None, date(2025, 11, 18), True), ('editing', None, date(2026, 9, 29), True)])
            audio('montaż trwa', 'editing', [('recording', None, None, True), ('editing', None, None, False)], active=1)
            audio('korekta zakończona', 'proofreading', [('proofreading', None, None, True)])
            audio('poprawki trwają', 'corrections', [('corrections', None, None, False)], active=0)
            audio('korekta trwa', 'proofreading', [('proofreading', date(2026, 9, 20), None, False)], active=0)
            audio('montaż zakończony', 'editing')
            audio('nagranie z kolumny', 'recording', recording_started_at=date(2025, 1, 2))
            audio('etap oczekiwania na publikację', 'awaiting_publication', [
                ('editing', date(2026, 8, 1), None, False), ('awaiting_publication', date(2026, 8, 10), None, False)], active=1)
            audio('korekta bez korektora', 'proofreading', [('recording', None, None, True),
                                                            ('proofreading', None, None, False)], active=1)
            MigrationExecutor(connection).migrate(after)
            apps = MigrationExecutor(connection).loader.project_state(after).apps
            Audio = apps.get_model('core', 'Audiobook')
            Stage = apps.get_model('core', 'AudiobookStage')

            def state(name):
                row = Audio.objects.get(text_id=made[name])
                stages = [(s.stage_type, s.started_at, s.ended_at, s.is_completed, s.start_unknown)
                          for s in Stage.objects.filter(text_id=made[name]).order_by('position', 'pk')]
                return row, stages

            row, stages = state('opublikowany')
            self.assertEqual((row.status, stages), ('published', []))
            row, stages = state('publikacja jako etap')
            self.assertEqual((row.status, row.premiere_date, stages), ('published', date(2025, 4, 2), []))
            row, stages = state('czeka na publikację')
            self.assertEqual((row.status, row.active_stage_id), ('published', None))
            self.assertEqual([s[0] for s in stages], ['recording', 'editing'])
            row, stages = state('montaż trwa')
            self.assertEqual(row.status, 'editing')
            self.assertEqual(stages[-1], ('editing', None, None, False, True))
            row, stages = state('korekta zakończona')
            self.assertEqual((row.status, stages[-1]), ('corrections', ('corrections', None, None, False, False)))
            row, stages = state('poprawki trwają')
            self.assertEqual((row.status, stages), ('corrections', [('corrections', None, None, False, True)]))
            row, _ = state('korekta trwa')
            self.assertEqual(row.status, 'proofreading')
            row, stages = state('montaż zakończony')
            self.assertEqual((row.status, stages), ('published', [('editing', None, None, True, False)]))
            row, stages = state('nagranie z kolumny')
            self.assertEqual(row.status, 'proofreading')
            self.assertEqual(stages, [('recording', date(2025, 1, 2), None, True, False),
                                      ('proofreading', None, None, False, False)])
            row, stages = state('etap oczekiwania na publikację')
            self.assertEqual((row.status, stages), ('published', [('editing', date(2026, 8, 1), date(2026, 8, 10), True, False)]))
            row, stages = state('korekta bez korektora')
            self.assertEqual((row.status, stages[-1]), ('proofreading', ('proofreading', None, None, False, False)))
            # Ponowne uruchomienie niczego nie zmienia.
            before = {name: state(name)[1] for name in made}
            from importlib import import_module
            import_module('core.migrations.0044_audiobook_linear_stages').convert(apps, None)
            self.assertEqual({name: state(name)[1] for name in made}, before)
        finally:
            MigrationExecutor(connection).migrate(after)

    def test_statuses_from_previous_version_collapse_to_fixed_order(self):
        """0044: statusy pośrednie i wstrzymanie z wersji 0043 przechodzą na sześć statusów."""
        executor = MigrationExecutor(connection)
        after = executor.loader.graph.leaf_nodes()
        executor.migrate([('core', '0043_remove_audiobook_stage_dates')])
        try:
            apps = MigrationExecutor(connection)._create_project_state(with_applied_migrations=True).apps
            Anthology_ = apps.get_model('texts', 'Anthology')
            Text_ = apps.get_model('texts', 'Text')
            Audio = apps.get_model('core', 'Audiobook')
            Stage = apps.get_model('core', 'AudiobookStage')
            book = Anthology_.objects.create(title='Migracja 0044', status='ready')
            made = {}

            def audio(name, status, stages=(), active=None, **fields):
                text = Text_.objects.create(title=name, anthology=book, length=1)
                row = Audio.objects.create(text=text, status=status, **fields)
                created = [Stage.objects.create(text=text, stage_type=kind, position=n, started_at=start,
                                                ended_at=end, is_completed=done, start_unknown=unknown)
                           for n, (kind, start, end, done, unknown) in enumerate(stages, start=1)]
                if active is not None:
                    row.active_stage = created[active]
                    row.save()
                made[name] = text.pk

            audio('czeka na nagranie', 'recording_waiting', [('recording', None, None, False, False)], active=0)
            audio('po poprawkach', 'after_corrections', [('recording', None, None, True, False),
                ('proofreading', None, None, True, False), ('corrections', None, date(2026, 9, 1), True, False)])
            audio('wstrzymane', 'paused', [('recording', date(2026, 9, 2), None, False, False)],
                  paused_at=date(2026, 9, 5))
            audio('stary etap', 'pending', [('pending', None, None, True, False)])
            audio('oczekuje na publikację', 'awaiting_publication', [('editing', None, None, True, True)])
            MigrationExecutor(connection).migrate(after)
            apps = MigrationExecutor(connection).loader.project_state(after).apps
            Audio = apps.get_model('core', 'Audiobook')
            Stage = apps.get_model('core', 'AudiobookStage')

            def state(name):
                row = Audio.objects.get(text_id=made[name])
                stages = [(s.stage_type, s.queued_at, s.is_completed)
                          for s in Stage.objects.filter(text_id=made[name]).order_by('position', 'pk')]
                current = Stage.objects.get(pk=row.active_stage_id).stage_type if row.active_stage_id else None
                return row.status, current, stages

            self.assertEqual(state('czeka na nagranie'), ('pending', None, []))
            status, current, stages = state('po poprawkach')
            self.assertEqual((status, current, stages[-1]), ('editing', 'editing', ('editing', date(2026, 9, 1), False)))
            self.assertEqual(state('wstrzymane')[:2], ('recording', 'recording'))
            self.assertEqual(state('stary etap'), ('pending', None, []))
            self.assertEqual(state('oczekuje na publikację')[:2], ('published', None))
        finally:
            MigrationExecutor(connection).migrate(after)
