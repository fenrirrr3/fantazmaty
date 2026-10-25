"""Panel admina tekstów i recenzji: ścieżki dotąd nieprzetestowane – zwykłe dodanie tekstu, inline notatek
i tłumaczenia, odmowy popupu „utwórz tekst ze zgłoszenia”, autouzupełnianie, formularze statusu i etapu,
lista recenzji oraz zabezpieczenia widoków przed kontem bez uprawnień superusera."""
import time
from unittest.mock import patch

from django.contrib.admin import site
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.edit_versions import version_of
from core.tests.people.support import admin_form_data
from fantazmaty.testing import create_member, create_superuser, edit_token
from illustrations.models import Illustration
from texts.admin import TextAdmin
from texts.models import Anthology, Review, ReviewAssignment, Text, TextNote, Translator
from workflow.models import WorkflowStage

ADD_URL = reverse('admin:texts_text_add')


def without_templates(data):
    """Formularz admina bez pustych wzorców wierszy inline (__prefix__), jak wysyła go przeglądarka."""
    return {key: value for key, value in data.items() if '__prefix__' not in key}


class TextAdminGapData(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('text-gaps')
        cls.book = Anthology.objects.create(title='Antologia luk')
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor', email='jan@example.com',
                                           has_contract=True)
        cls.text = Text.objects.create(title='Opowiadanie', length=100, anthology=cls.book)
        cls.text.authors.add(cls.author)

    def setUp(self):
        self.client.force_login(self.admin)

    def review(self, **values):
        data = dict(title='Zgłoszenie', anthology=self.book, author_first_name='Jan', author_last_name='Autor',
                    email='jan@example.com', length=100, genre='fantasy', status=Review.Status.ACCEPTED,
                    author_notified_at=timezone.localdate())
        data.update(values)
        return Review.objects.create(**data)

    def change_url(self, text=None):
        return reverse('admin:texts_text_change', args=[(text or self.text).pk])


class TextAdminFormGapTests(TextAdminGapData):
    def test_plain_add_creates_text_ready_for_editing(self):
        data = admin_form_data(self.client, ADD_URL, 'text_form', title='Nowy z panelu', length='1500',
                               anthology=self.book.pk, authors=[str(self.author.pk)], _save='Zapisz')
        response = self.client.post(ADD_URL, without_templates(data))
        self.assertEqual(response.status_code, 302)
        text = Text.objects.get(title='Nowy z panelu')
        self.assertEqual(list(text.authors.all()), [self.author])
        self.assertEqual(list(WorkflowStage.objects.filter(text=text).values_list('stage_type', 'workflow_cycle')),
                         [(WorkflowStage.StageType.READY_FOR_EDITING, 1)])

    def test_note_inline_adds_note_as_current_user_and_deletes_marked_note(self):
        old = TextNote.objects.create(text=self.text, author=None, content='Do usunięcia')
        kept = TextNote.objects.create(text=self.text, author=None, content='Zostaje')
        data = without_templates(admin_form_data(self.client, self.change_url(), 'text_form', _save='Zapisz'))
        index = {data[f'notes-{i}-id']: i for i in range(2)}
        data[f'notes-{index[str(old.pk)]}-DELETE'] = 'on'
        data[f'notes-{index[str(kept.pk)]}-content'] = 'Zostaje poprawiona'
        data.update({'notes-TOTAL_FORMS': '3', 'notes-2-id': '', 'notes-2-text': str(self.text.pk),
                     'notes-2-content': 'Nowa notatka', 'notes-2-is_important': 'on'})
        self.assertEqual(self.client.post(self.change_url(), data).status_code, 302)
        notes = {note.content: note.author for note in TextNote.objects.filter(text=self.text)}
        # Poprawiona notatka zachowuje dotychczasowego autora; nowa dostaje autora z zalogowanego konta.
        self.assertEqual(notes, {'Zostaje poprawiona': None, 'Nowa notatka': self.admin})
        self.assertTrue(TextNote.objects.filter(pk=kept.pk).exists())

    def test_translation_inline_saves_translators(self):
        translated = Anthology.objects.create(title='Przekłady', is_translated=True)
        text = Text.objects.create(title='Przekład', length=10, anthology=translated)
        translator = Translator.objects.create(first_name='Anna', last_name='Tłumaczka')
        data = without_templates(admin_form_data(self.client, self.change_url(text), 'text_form', _save='Zapisz'))
        data['translation-0-translators'] = [str(translator.pk)]
        self.assertEqual(self.client.post(self.change_url(text), data).status_code, 302)
        self.assertEqual(list(text.translation.translators.all()), [translator])

    def test_late_validation_error_of_plain_change_rolls_back_and_returns_400(self):
        data = without_templates(admin_form_data(self.client, self.change_url(), 'text_form',
                                                 title='Zmieniony tytuł', _save='Zapisz'))
        original = TextAdmin.save_related

        def fail_after_save(admin, request, form, formsets, change):
            original(admin, request, form, formsets, change)
            raise ValidationError('Zapis przerwany po walidacji.')

        with patch.object(TextAdmin, 'save_related', fail_after_save):
            response = self.client.post(self.change_url(), data)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content.decode(), 'Zapis przerwany po walidacji.')
        self.text.refresh_from_db()
        self.assertEqual(self.text.title, 'Opowiadanie')

    def test_list_previews_shorten_long_warnings_and_notes(self):
        Text.objects.filter(pk=self.text.pk).update(content_warnings='przemoc')
        Text.objects.create(title='Długie ostrzeżenia', length=10, anthology=self.book,
                            content_warnings='słowo ' * 30)
        TextNote.objects.create(text=self.text, author=self.admin, content='uwaga ' * 30)
        texts = self.client.get(reverse('admin:texts_text_changelist'))
        self.assertContains(texts, '>przemoc<')
        self.assertContains(texts, ('słowo ' * 30)[:77] + '...')
        notes = self.client.get(reverse('admin:texts_textnote_changelist'))
        self.assertContains(notes, ('uwaga ' * 30)[:97] + '...')

    def test_autocomplete_scopes_for_review_link_and_illustration(self):
        Anthology.objects.filter(pk=self.book.pk).update(has_illustrations=True)
        WorkflowStage.objects.create(text=self.text, workflow_cycle=1,
                                     stage_type=WorkflowStage.StageType.READY_FOR_EDITING)
        other_book = Anthology.objects.create(title='Bez ilustracji')
        Text.objects.create(title='Opowiadanie bez etapów', length=10, anthology=other_book)
        url = reverse('admin:autocomplete')
        cases = {
            'review copied_text: tylko teksty z workflow': (('texts', 'review', 'copied_text'), [self.text.pk]),
            'ilustracja: tylko antologie z ilustracjami': (('illustrations', 'illustration', 'text'), [self.text.pk]),
        }
        for case, ((app_label, model_name, field_name), expected) in cases.items():
            with self.subTest(case):
                found = self.client.get(url, {'app_label': app_label, 'model_name': model_name,
                                              'field_name': field_name, 'term': 'Opowiadanie'}).json()['results']
                self.assertEqual([int(row['id']) for row in found], expected)


class SourceReviewPopupGapTests(TextAdminGapData):
    def prefill(self, review, **snapshot):
        session = self.client.session
        session['review_text_prefills'] = {'token': {'user': self.admin.pk, 'at': time.time(),
                                                     'review_id': review.pk, **snapshot}}
        session.save()
        return ADD_URL + '?_popup=1&prefill=token'

    def test_popup_refuses_invalid_expired_missing_or_linked_source(self):
        linked = self.review(title='Już przeniesione', copied_text=self.text)
        fresh = self.review(title='Świeże')
        cases = {
            'wygasły token': (self.prefill(fresh, at=time.time() - 901), 'Dane formularza wygasły'),
            'nieprawidłowe ID': (ADD_URL + '?source_review=abc', 'Nieprawidłowa recenzja źródłowa.'),
            'zbyt długie ID': (ADD_URL + '?source_review=' + '9' * 19, 'Nieprawidłowa recenzja źródłowa.'),
            'brak recenzji': (ADD_URL + '?source_review=999999', 'Recenzja nie istnieje'),
            'recenzja z tekstem': (ADD_URL + f'?source_review={linked.pk}', 'ma już powiązany tekst'),
        }
        count = Text.objects.count()
        for case, (url, message) in cases.items():
            with self.subTest(case):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.content.decode())
        self.assertEqual(Text.objects.count(), count)

    def test_direct_popup_finds_author_by_unique_email_only(self):
        review = self.review(title='Bez profilu autora')
        initial = self.client.get(ADD_URL, {'source_review': review.pk, '_popup': 1}).context['adminform'].form.initial
        self.assertEqual(initial['authors'], [self.author.pk])
        unknown = self.review(title='Nieznany adres', email='nikt@example.com')
        initial = self.client.get(ADD_URL, {'source_review': unknown.pk, '_popup': 1}).context['adminform'].form.initial
        self.assertEqual(initial['authors'], [])

    def test_popup_text_must_stay_in_review_anthology(self):
        review = self.review(title='Inna antologia', author=self.author)
        other = Anthology.objects.create(title='Obca antologia')
        url = self.prefill(review)
        page = self.client.get(url)
        data = {key: value for key, value in page.context['adminform'].form.initial.items() if key != 'source_review_id'}
        for inline in page.context['inline_admin_formsets']:
            data.update({inline.formset.prefix + '-TOTAL_FORMS': '0', inline.formset.prefix + '-INITIAL_FORMS': '0'})
        data.update(anthology=other.pk, source_contract_received='on', _save='Zapisz')
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Tekst musi należeć do antologii zapisanej recenzji.',
                      response.context['adminform'].form.errors['anthology'])
        review.refresh_from_db()
        self.assertIsNone(review.copied_text_id)

    def test_prepare_text_endpoint_refusals(self):
        endpoint = reverse('admin:texts_review_prepare_text')
        coauthor = Author.objects.create(first_name='Ola', last_name='Współ', email='ola@example.com')
        ready = self.review(title='Gotowe')
        ready.coauthors.add(coauthor)
        cases = {
            'GET': ('get', {}, 405, 'POST required'),
            'bez ID': ('post', {'review_id': 'x'}, 400, 'Najpierw zapisz recenzję'),
            'nieistniejąca': ('post', {'review_id': 999999}, 400, 'Najpierw zapisz recenzję'),
            'już przeniesiona': ('post', {'review_id': self.review(copied_text=self.text).pk}, 409,
                                 'ma już powiązany tekst'),
            'nieprzyjęta': ('post', {'review_id': self.review(status=Review.Status.NEW).pk}, 400,
                            'wyłącznie przyjęty tekst'),
            'niezapisani współautorzy': ('post', {'review_id': ready.pk, 'source_coauthors_present': '1'}, 409,
                                         'zapisz zmiany autorów'),
            'inny autor w formularzu': ('post', {'review_id': ready.pk, 'source_author_id': self.author.pk}, 409,
                                        'zapisz zmiany autorów'),
        }
        for case, (method, data, status, message) in cases.items():
            with self.subTest(case):
                response = getattr(self.client, method)(endpoint, data)
                self.assertEqual(response.status_code, status)
                self.assertIn(message, response.json()['error'])
        self.assertNotIn('review_text_prefills', self.client.session)


class TextWorkflowToolGapTests(TextAdminGapData):
    def test_repeat_form_accepts_only_get(self):
        url = reverse('admin:texts_text_repeat', args=[self.text.pk])
        response = self.client.post(url, {'_edit_version': edit_token(self.admin, self.text)})
        self.assertEqual(response.status_code, 405)

    def test_add_stage_and_status_forms_refuse_stale_version(self):
        stale = version_of(self.text) - 1
        cases = {
            'dodanie etapu': ('admin:texts_text_add_stage', {'kind': 'editing', 'version': stale}),
            'zmiana statusu': ('admin:texts_text_manual_status', {'stage': 'editing', 'version': stale, 'confirm': 'on'}),
        }
        for case, (name, data) in cases.items():
            with self.subTest(case):
                url = reverse(name, args=[self.text.pk])
                self.assertEqual(self.client.get(url).status_code, 200)
                response = self.client.post(url, data)
                self.assertContains(response, 'Dane zmieniły się. Odśwież formularz.')
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())

    def test_purge_reports_illustration_added_after_check(self):
        """Ilustracja przypięta między sprawdzeniem a usunięciem: tekst zostaje, formularz pokazuje powód."""
        url = reverse('admin:texts_text_purge', args=[self.text.pk])
        from texts import purge
        original = purge.purge_blocker
        calls = []

        def blocker_with_race(text, using='default'):
            calls.append(text.pk)
            if len(calls) == 2:
                Illustration.objects.create(text=text)
            return original(text, using)

        with patch.object(purge, 'purge_blocker', blocker_with_race):
            response = self.client.post(url, {'version': version_of(self.text), 'confirm': 'on'})
        self.assertContains(response, 'przypisane ilustracje')
        self.assertTrue(Text.objects.filter(pk=self.text.pk).exists())

    def test_delete_model_refuses_text_with_started_work(self):
        WorkflowStage.objects.create(text=self.text, workflow_cycle=1, stage_type='editing',
                                     started_at=timezone.localdate())
        request = RequestFactory().post('/')
        request.user = self.admin
        with self.assertRaisesMessage(PermissionDenied, 'rozpoczętą lub zakończoną pracę'):
            TextAdmin(Text, site).delete_model(request, self.text)
        self.assertTrue(Text.objects.filter(pk=self.text.pk).exists())

    def test_views_refuse_account_without_superuser_even_if_site_check_is_bypassed(self):
        """Obrona w głębi (SuperuserOnlyAdminMixin): uprawnienia modelu odmawiają, gdyby strona admina
        przepuściła konto bez superusera."""
        staff = get_user_model().objects.create_user('text-gaps-staff', is_staff=True)
        request = RequestFactory().post('/', {'review_id': self.review().pk})
        request.user = staff
        request.session = {}
        text_admin = TextAdmin(Text, site)
        get = RequestFactory().get('/')
        get.user = staff
        calls = {
            'powtórzenie etapów': lambda: text_admin.repeat_form(get, self.text.pk),
            'czarna lista audiobooków': lambda: text_admin.block_audiobooks(request, Text.objects.all()),
        }
        for case, call in calls.items():
            with self.subTest(case), self.assertRaises(PermissionDenied):
                call()
        self.assertTrue(Text.objects.filter(pk=self.text.pk, audiobook_blacklisted=False).exists())
        self.assertFalse(WorkflowStage.objects.filter(text=self.text).exists())


class ReviewAdminListGapTests(TextAdminGapData):
    def test_review_list_shows_author_and_transfer_flag(self):
        self.review(title='Przeniesione', copied_text=self.text, author_pseudonym='Pióro')
        self.review(title='Czekające')
        response = self.client.get(reverse('admin:texts_review_changelist'))
        self.assertEqual(response.context['title'], 'Recenzje')
        rows = {row.title: row for row in response.context['cl'].result_list}
        model_admin = response.context['cl'].model_admin
        self.assertEqual({title: model_admin.display_copied_to_text(row) for title, row in rows.items()},
                         {'Przeniesione': True, 'Czekające': False})
        self.assertContains(response, rows['Przeniesione'].author_display_name)

    def test_archived_assignment_can_be_deleted_in_inline(self):
        review = self.review(title='Archiwalne', old_reviews=True)
        removed = ReviewAssignment.objects.create(review=review, user=create_member('rev-gaps-1', 'Recenzent'),
                                                  position=1, opinion=ReviewAssignment.Opinion.YES)
        kept = ReviewAssignment.objects.create(review=review, user=create_member('rev-gaps-2', 'Recenzent'),
                                               position=2, opinion=ReviewAssignment.Opinion.NO)
        url = reverse('admin:texts_review_change', args=[review.pk])
        data = without_templates(admin_form_data(self.client, url, 'review_form'))
        index = {data[f'assignments-{i}-id']: i for i in range(2)}
        data[f'assignments-{index[str(removed.pk)]}-DELETE'] = 'on'
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.assertEqual(list(review.assignments.all()), [kept])

    def test_post_to_missing_review_changes_nothing(self):
        url = reverse('admin:texts_review_change', args=[999999])
        response = self.client.post(url, {'title': 'Nowa'})
        self.assertRedirects(response, reverse('admin:index'))
        self.assertFalse(Review.objects.exists())
