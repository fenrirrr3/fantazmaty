"""Panel admina tekstów: zwykłe dodanie tekstu, inline notatek i tłumaczenia, wycofanie zapisu po późnym błędzie,
podglądy na listach, autouzupełnianie, formularze powtórzenia etapów, dodania etapu i statusu oraz usuwanie."""
from unittest.mock import patch

from django.contrib.admin import site
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from core.edit_versions import version_of
from core.tests.people.support import admin_form_data
from fantazmaty.testing import create_superuser, edit_token
from texts.admin import TextAdmin
from texts.models import Anthology, Text, TextNote, Translator
from workflow.models import WorkflowStage

ADD_URL = reverse('admin:texts_text_add')


def without_templates(data):
    """Formularz admina bez pustych wzorców wierszy inline (__prefix__), jak wysyła go przeglądarka."""
    return {key: value for key, value in data.items() if '__prefix__' not in key}


class TextAdminData(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('text-admin')
        cls.book = Anthology.objects.create(title='Antologia luk')
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor', email='jan@example.com',
                                           has_contract=True)
        cls.text = Text.objects.create(title='Opowiadanie', length=100, anthology=cls.book)
        cls.text.authors.add(cls.author)

    def setUp(self):
        self.client.force_login(self.admin)

    def change_url(self, text=None):
        return reverse('admin:texts_text_change', args=[(text or self.text).pk])


class TextAdminFormTests(TextAdminData):
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


class TextWorkflowToolTests(TextAdminData):
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

    def test_delete_model_refuses_text_with_started_work(self):
        WorkflowStage.objects.create(text=self.text, workflow_cycle=1, stage_type='editing',
                                     started_at=timezone.localdate())
        request = RequestFactory().post('/')
        request.user = self.admin
        with self.assertRaisesMessage(PermissionDenied, 'rozpoczętą lub zakończoną pracę'):
            TextAdmin(Text, site).delete_model(request, self.text)
        self.assertTrue(Text.objects.filter(pk=self.text.pk).exists())
