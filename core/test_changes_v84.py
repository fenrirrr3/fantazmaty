"""Audiobook stage order, contacts, public list, AD locks, reviews, post-layout history, extracts flag."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.audiobook_models import resolve_contact
from core.audiobook_services import allowed_next_stages, finish_stage, start_stage
from core.edit_versions import version_of
from core.models import (AudioContributor, Audiobook, AudioDescription, PostLayoutAssignment, WorkflowEvent)
from core.post_layout import bulk_change, change_status, selection_token
from core.selectors.people import profile_assignments
from core.source_reviews import validate_source_review_link
from core.views.search import _search_reviews
from people.models import Person
from texts.models import Anthology, Review, Text
from workflow.tests import create_member


def token(user, label, obj):
    return signing.dumps([user.pk, f'{label}:{obj.pk}', version_of(obj)], salt='cms-edit-version')


class AudiobookQueueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('v84admin', 'v84@example.test', 'test')
        cls.book = Anthology.objects.create(title='Antologia v84', status=Anthology.Status.READY)
        cls.text = Text.objects.create(title='Nagranie', anthology=cls.book, length=100)
        cls.text.authors.add(Author.objects.create(first_name='Anna', last_name='Autorka'))
        cls.url = reverse('core:audiobook_detail', args=[cls.text.pk])

    def start(self, kind, when=None):
        return start_stage(text_id=self.text.pk, user=self.admin, stage_type=kind, started_at=when or timezone.localdate())

    def test_order_publication_is_final_and_pending_is_not_a_stage(self):
        self.assertNotIn('pending', allowed_next_stages(self.text))
        with self.assertRaises(ValidationError):
            self.start('pending')
        stage = self.start('recording')
        finish_stage(text_id=self.text.pk, stage_id=stage.pk, user=self.admin)
        audio = Audiobook.objects.get(text=self.text)
        self.assertTrue(audio.awaiting_next_stage)
        self.assertIn('zakończony', audio.status_label)
        self.assertNotIn('recording', allowed_next_stages(self.text))
        stage = self.start('proofreading')
        finish_stage(text_id=self.text.pk, stage_id=stage.pk, user=self.admin)
        stage = self.start('corrections')
        finish_stage(text_id=self.text.pk, stage_id=stage.pk, user=self.admin)
        # A second proofreading round is allowed after corrections.
        self.assertIn('proofreading', allowed_next_stages(self.text))
        with self.assertRaises(ValidationError):
            self.start('recording')
        published = self.start('published')
        audio.refresh_from_db()
        self.assertEqual(audio.status, 'published')
        self.assertIsNone(audio.active_stage_id)
        self.assertTrue(published.is_completed)
        self.assertEqual(audio.premiere_date, timezone.localdate())
        self.assertEqual(allowed_next_stages(self.text), [])
        self.assertFalse(audio.awaiting_next_stage)
        with self.assertRaises(ValidationError):
            finish_stage(text_id=self.text.pk, stage_id=published.pk, user=self.admin)
        events = WorkflowEvent.objects.filter(kind='audiobook', text=self.text)
        self.assertEqual(events.count(), 7)
        self.assertTrue(all(event.channel for event in events))
        self.client.force_login(self.admin)
        page = self.client.get(self.url)
        self.assertContains(page, 'etap końcowy')
        self.assertNotContains(page, 'name="stage_type"')

    def test_waiting_filter_on_audiobook_list(self):
        stage = self.start('recording')
        finish_stage(text_id=self.text.pk, stage_id=stage.pk, user=self.admin)
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:audiobooks'), {'status': 'waiting_next'})
        self.assertEqual(page.context['page_obj'].paginator.count, 1)
        self.assertContains(page, 'Czeka na kolejny etap')

    def test_audiobook_events_do_not_change_text_status_sorting(self):
        self.start('recording')
        from core.selectors.texts import text_list_context
        rows = text_list_context(user=self.admin, params={})['texts']
        self.assertIsNone(next(row for row in rows if row['pk'] == self.text.pk)['last_status_change'])


class AudioContactTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('v84contacts', 'v84c@example.test', 'test')
        cls.book = Anthology.objects.create(title='Kontakty v84', status=Anthology.Status.READY)
        cls.first = Text.objects.create(title='Pierwszy', anthology=cls.book, length=10)
        cls.second = Text.objects.create(title='Drugi', anthology=cls.book, length=10)

    def test_one_profile_per_person_typos_and_no_renaming_from_audiobook(self):
        person = resolve_contact('Łucja Lektorka', 'lucja@example.test')
        self.assertEqual(resolve_contact('  łucja   lektorka ', ''), person)
        self.assertEqual(resolve_contact('Lucja Lektorka', 'LUCJA@example.test'), person)
        with self.assertRaises(ValidationError):
            resolve_contact('Ktoś Inny', 'lucja@example.test')
        first = Audiobook.objects.create(text=self.first, narrator_name='Łucja Lektorka')
        second = Audiobook.objects.create(text=self.second, engineer_name='lucja lektorka', engineer_email='lucja@example.test')
        self.assertEqual(first.narrator_contact, person)
        self.assertEqual(second.engineer_contact, person)
        self.assertEqual(first.narrator_email, 'lucja@example.test')
        self.assertEqual(AudioContributor.objects.count(), 1)
        self.client.force_login(self.admin)
        page = self.client.get(reverse('core:audio_contributor', args=[person.pk]))
        self.assertEqual(len(page.context['current_works']), 2)
        response = self.client.post(reverse('core:audio_contributor', args=[person.pk]),
                                    {'name': 'Łucja Nowak', 'email': 'lucja@example.test'})
        self.assertEqual(response.status_code, 302)
        first.refresh_from_db()
        self.assertEqual(first.narrator_name, 'Łucja Nowak')

    def test_people_form_creates_new_profile_instead_of_renaming(self):
        person = resolve_contact('Jan Lektor', '')
        audio = Audiobook.objects.create(text=self.first, narrator_contact=person)
        self.client.force_login(self.admin)
        response = self.client.post(reverse('core:audiobook_detail', args=[self.first.pk]), {
            'action': 'people', '_edit_version': token(self.admin, 'texts.text', self.first),
            'people-narrator_name': 'Piotr Lektor', 'people-narrator_contact': person.pk,
            'people-narrator_email': '', 'people-engineer_name': '', 'people-engineer_email': ''})
        self.assertEqual(response.status_code, 302)
        person.refresh_from_db()
        audio.refresh_from_db()
        self.assertEqual(person.name, 'Jan Lektor')
        self.assertEqual(audio.narrator_name, 'Piotr Lektor')
        self.assertNotEqual(audio.narrator_contact_id, person.pk)

    def test_public_list_shows_narrator_and_everything_not_published(self):
        Audiobook.objects.create(text=self.first, narrator_name='Ewa Głos', status='recording')
        Audiobook.objects.create(text=self.second, status='published')
        page = self.client.get(reverse('core:external_audiobooks'))
        self.assertContains(page, 'Ewa Głos')
        self.assertContains(page, '<th>Lektor</th>', html=True)
        self.assertEqual(page.context['page_obj'].paginator.count, 1)
        self.assertNotContains(page, 'Drugi')
        self.assertNotContains(page, '@')


class AnthologyTaskTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('v84tasks', 'v84t@example.test', 'test')
        cls.writer_user = create_member('v84writer', 'Redaktor')
        cls.writer = cls.writer_user.person_profile
        cls.book = Anthology.objects.create(title='AD v84')
        cls.former = Person.objects.create(first_name='Była', last_name='Osoba', is_active=False)
        cls.stranger = Person.objects.create(first_name='Obca', last_name='Nieaktywna', is_active=False)
        other = Anthology.objects.create(title='Inna AD')
        task = other.production_tasks.get(task_type='blurb')
        task.assigned_to, task.status = cls.former, 'commissioned'
        task.save()

    def ad_url(self):
        return reverse('core:audio_description_detail', args=[self.book.pk])

    def test_ad_page_does_not_change_task_status_and_completion_is_final(self):
        task = self.book.production_tasks.get(task_type='audio_description')
        task.assigned_to, task.status = self.writer, 'commissioned'
        task.save()
        self.client.force_login(self.writer_user)
        page = self.client.get(self.ad_url())
        self.assertIsNone(page.context['form'])
        response = self.client.post(self.ad_url(), {'action': 'assignment', 'status': 'ready',
                                                    '_edit_version': token(self.writer_user, 'texts.anthology', self.book)})
        self.assertEqual(response.status_code, 403)
        response = self.client.post(self.ad_url(), {'action': 'stage', 'stage': 'completed',
                                                    '_edit_version': token(self.writer_user, 'texts.anthology', self.book)})
        self.assertEqual(response.status_code, 302)
        task.refresh_from_db()
        self.assertEqual(task.status, 'ready')
        response = self.client.post(self.ad_url(), {'action': 'stage', 'stage': 'writing',
                                                    '_edit_version': token(self.writer_user, 'texts.anthology', self.book)})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AudioDescription.objects.get(anthology=self.book).stage, 'completed')
        # The anthology page keeps a finished task locked and its credit intact.
        self.client.force_login(self.admin)
        anthology = reverse('core:anthology_detail', args=[self.book.pk])
        form = next(f for f in self.client.get(anthology).context['task_forms'] if f.instance.task_type == 'audio_description')
        self.assertTrue(form.fields['status'].disabled)
        self.client.post(anthology, {'remove_task': 'audio_description',
                                     '_edit_version': token(self.admin, 'texts.anthology', self.book)})
        task.refresh_from_db()
        self.assertEqual((task.status, task.assigned_to), ('ready', self.writer))
        # Reopening in admin returns to the last working step.
        description = AudioDescription.objects.get(anthology=self.book)
        description.content = 'Tekst audiodeskrypcji'
        description.save(update_fields=['content'])
        task.status = 'commissioned'
        task.save()
        self.assertEqual(AudioDescription.objects.get(anthology=self.book).stage, 'proofreading')

    def test_people_lists_include_inactive_only_with_history(self):
        self.client.force_login(self.admin)
        form = self.client.get(self.ad_url()).context['form']
        people = form.fields['assigned_to'].queryset
        self.assertTrue(people.filter(pk=self.former.pk).exists())
        self.assertFalse(people.filter(pk=self.stranger.pk).exists())
        self.assertIn('nieaktywny', form.fields['assigned_to'].label_from_instance(self.former))

    def test_cover_commission_date_survives_corrections(self):
        from illustrations.models import Illustrator
        artist = Illustrator.objects.create(first_name='Ola', last_name='Okładka', covers=True)
        self.book.cover_illustrator, self.book.cover_status = artist, 'in_progress'
        self.book.save()
        old = timezone.localdate() - timedelta(days=10)
        Anthology.objects.filter(pk=self.book.pk).update(cover_commissioned_at=old)
        book = Anthology.objects.get(pk=self.book.pk)
        book.cover_notes = 'poprawka'
        book.cover_author = 'Ola Okładka (poprawione)'
        book.save()
        self.assertEqual(Anthology.objects.get(pk=book.pk).cover_commissioned_at, old)
        other = Illustrator.objects.create(first_name='Inna', last_name='Osoba', covers=True)
        book.cover_illustrator = other
        book.save()
        self.assertEqual(Anthology.objects.get(pk=book.pk).cover_commissioned_at, timezone.localdate())


class ReviewVisibilityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.editor = create_member('v84editor', 'Redaktor')
        cls.reviewer = create_member('v84reviewer', 'Recenzent')
        cls.book = Anthology.objects.create(title='Recenzje v84')
        common = dict(anthology=cls.book, author_first_name='A', author_last_name='B', email='a@example.test', length=10)
        cls.accepted = Review.objects.create(title='Szukane przyjęte', status='accepted', **common)
        cls.rejected = Review.objects.create(title='Szukane odrzucone', status='rejected', **common)
        cls.withdrawn = Review.objects.create(title='Szukane wycofane', status='withdrawn', **common)
        cls.open = Review.objects.create(title='Szukane nowe', status='new', **common)

    def test_withdrawn_only_in_admin_and_team_sees_decisions(self):
        # Lista Recenzje jest dla recenzentów; redaktor otwiera tylko pojedyncze recenzje.
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse('core:review_list')).status_code, 403)
        self.client.force_login(self.reviewer)
        current = self.client.get(reverse('core:review_list'))
        titles = {row['title'] for row in current.context['page_obj']}
        self.assertEqual(titles, {'Szukane nowe'})
        archive = self.client.get(reverse('core:review_list'), {'old_reviews': '1'})
        self.assertEqual({row['title'] for row in archive.context['page_obj']}, {'Szukane przyjęte', 'Szukane odrzucone'})
        self.client.force_login(self.editor)
        self.assertEqual(self.client.get(reverse('core:assigned_review_detail', args=[self.withdrawn.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse('core:assigned_review_detail', args=[self.accepted.pk])).status_code, 200)
        found = {row['title'] for row in _search_reviews('Szukane', user=self.editor, include_authors=False)}
        self.assertEqual(found, {'Szukane przyjęte', 'Szukane odrzucone', 'Szukane nowe'})

    def test_my_review_views(self):
        from texts.models import ReviewAssignment
        ReviewAssignment.objects.create(review=self.open, user=self.reviewer, opinion='reading', position=1)
        ReviewAssignment.objects.create(review=self.accepted, user=self.reviewer, opinion='yes', position=1)
        second = Review.objects.create(title='Oddana', status='in_review', anthology=self.book, author_first_name='C',
                                       author_last_name='D', email='c@example.test', length=10)
        ReviewAssignment.objects.create(review=second, user=self.reviewer, opinion='no', position=1)
        self.client.force_login(self.reviewer)
        url = reverse('core:my_reviews')
        counts = {view: self.client.get(url, {'view': view}).context['page_obj'].paginator.count
                  for view in ('active', 'completed', 'decided', 'all')}
        self.assertEqual(counts, {'active': 1, 'completed': 2, 'decided': 2, 'all': 2})

    def test_rejected_cannot_be_linked_but_other_anthology_can(self):
        other = Anthology.objects.create(title='Inna antologia v84')
        text = Text.objects.create(title='Szukane przyjęte', anthology=other, length=10)
        with self.assertRaises(ValidationError):
            validate_source_review_link(text, self.rejected, confirm_mismatch=True)
        validate_source_review_link(text, self.accepted, confirm_mismatch=True)


class PostLayoutHistoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.manager = create_member('v84layout', 'Koordynator korekty')
        cls.reader = create_member('v84reader', 'Korektor poskładowy')
        cls.book = Anthology.objects.create(title='Poskładowa v84')

    def make(self, start, end):
        return PostLayoutAssignment.objects.create(anthology=self.book, proofreader=self.reader, page_from=start,
                                                   page_to=end, created_by=self.manager)

    def test_overlap_revert_and_soft_delete(self):
        first, second = self.make(1, 20), self.make(10, 30)
        first = change_status(user=self.reader, pk=first.pk, status='in_progress', version=first.version)
        first = change_status(user=self.reader, pk=first.pk, status='completed', version=first.version)
        first = change_status(user=self.reader, pk=first.pk, status='in_progress', version=first.version)
        self.assertIsNone(first.completed_on)
        self.assertIsNotNone(first.work_start)
        first = change_status(user=self.reader, pk=first.pk, status='assigned', version=first.version)
        self.assertIsNone(first.work_start)
        bulk_change(user=self.manager, selected=[selection_token(self.manager, second)], operation='delete', confirm_delete=True)
        second.refresh_from_db()
        self.assertIsNotNone(second.deleted_at)
        self.assertEqual(PostLayoutAssignment.objects.count(), 2)
        self.client.force_login(self.manager)
        self.assertEqual(self.client.get(reverse('core:post_layout')).context['page_obj'].paginator.count, 1)
        self.assertEqual(self.client.get(reverse('core:post_layout'), {'show_deleted': '1'}).context['page_obj'].paginator.count, 1)
        bulk_change(user=self.manager, selected=[selection_token(self.manager, second)], operation='restore')
        second.refresh_from_db()
        self.assertIsNone(second.deleted_at)

    def test_profile_counters_include_post_layout(self):
        self.make(1, 5)
        _, summary = profile_assignments(self.reader.person_profile, include_authors=False)
        self.assertEqual(summary['reserved'], 1)


class ExtractsFlagTests(TestCase):
    def test_flag_replaces_title_detection(self):
        named = Anthology.objects.create(title='Ekstrakty jesienne bez flagi')
        self.assertFalse(Text.objects.filter(anthology=named).exists())
        flagged = Anthology.objects.create(title='Zbiorówka', is_extracts=True)
        whole = Text.objects.get(anthology=flagged)
        self.assertTrue(whole.is_extract_volume)
        flagged.is_novel = True
        with self.assertRaises(ValidationError):
            flagged.full_clean()
        busy = Anthology.objects.create(title='Zwykła')
        Text.objects.create(title='Zwykły tekst', anthology=busy, length=5)
        busy.is_extracts = True
        with self.assertRaises(ValidationError):
            busy.full_clean()
