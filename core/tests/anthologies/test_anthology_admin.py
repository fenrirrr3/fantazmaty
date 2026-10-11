"""Antologie, rozdziały i przekłady w adminie: usuwanie antologii, zadania przy dodawaniu, pola powieści, formularz tekstu i listy tłumaczeń."""
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.forms import HiddenInput
from django.test import RequestFactory, TestCase
from django.urls import reverse

from authors.models import Author
from fantazmaty.testing import create_member, create_superuser, form_value
from texts.admin import TextAdminForm
from texts.models import Anthology, AnthologyTask, ForeignAuthor, Text, Translator
from texts.novels import new_chapter

from .support import admin_form_data


class AnthologyDeletionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = create_superuser('delete-admin')

    def request(self):
        request = RequestFactory().get('/')
        request.user = self.user
        return request

    def test_delete_preview_does_not_mutate_and_single_delete_removes_empty_tasks(self):
        book = Anthology.objects.create(title='Pusta do usunięcia')
        tasks = list(book.production_tasks.values_list('pk', flat=True))
        self.client.force_login(self.user)
        url = reverse('admin:texts_anthology_delete', args=[book.pk])
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertFalse(page.context['perms_lacking'])
        self.assertTrue(AnthologyTask.objects.filter(pk__in=tasks).exists())
        token = form_value(page)
        self.assertEqual(self.client.post(url, {'post': 'yes', '_edit_version': token}).status_code, 302)
        self.assertFalse(Anthology.objects.filter(pk=book.pk).exists())
        self.assertFalse(AnthologyTask.objects.filter(pk__in=tasks).exists())

    def test_assigned_or_commissioned_task_protects_parent_and_bulk_is_atomic(self):
        model_admin = admin.site.get_model_admin(Anthology)
        person = create_member('delete-typesetter', 'Składacz').person_profile
        empty = Anthology.objects.create(title='Pusta zachowana')
        book = Anthology.objects.create(title='Z przypisaniem')
        task = book.production_tasks.first()
        for status, assignee in [('not_commissioned', person), ('commissioned', person), ('ready', person)]:
            task.status, task.assigned_to = status, assignee
            task.save()
            request = self.request()
            _, _, permissions, _ = model_admin.get_deleted_objects([book], request)
            self.assertIn(AnthologyTask._meta.verbose_name, permissions)
            self.assertFalse(hasattr(request, '_deleting_anthologies'))
            with self.assertRaises(PermissionDenied):
                model_admin.delete_queryset(request, Anthology.objects.filter(pk__in=[empty.pk, book.pk]))
            self.assertEqual(Anthology.objects.filter(pk__in=[empty.pk, book.pk]).count(), 2)
        self.assertFalse(admin.site.get_model_admin(AnthologyTask).has_delete_permission(self.request(), task))
        task.status, task.assigned_to = 'not_commissioned', None
        task.save()
        self.assertFalse(admin.site.get_model_admin(AnthologyTask).has_delete_permission(self.request(), task))
        model_admin.delete_queryset(self.request(), Anthology.objects.filter(pk__in=[empty.pk, book.pk]))
        self.assertEqual(Anthology.objects.filter(pk__in=[empty.pk, book.pk]).count(), 0)

    def test_other_protected_records_still_block_delete(self):
        book = Anthology.objects.create(title='Antologia z tekstem')
        Text.objects.create(anthology=book, title='Zachowaj mnie', length=1)
        model_admin = admin.site.get_model_admin(Anthology)
        self.assertTrue(model_admin.get_deleted_objects([book], self.request())[3])
        with self.assertRaises(PermissionDenied):
            model_admin.delete_model(self.request(), book)
        self.assertTrue(Anthology.objects.filter(pk=book.pk).exists())

    def test_nested_deletion_preview_restores_outer_scope(self):
        model_admin = admin.site.get_model_admin(Anthology)
        request = self.request()
        request._deleting_anthologies = {'zewnętrzne'}
        model_admin.get_deleted_objects([Anthology.objects.create(title='Wewnętrzna')], request)
        self.assertEqual(request._deleting_anthologies, {'zewnętrzne'})


class AnthologyAdminFormTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('anthology-form-admin')
        cls.person = create_member('anthology-form-typesetter', 'Składacz').person_profile

    def setUp(self):
        self.client.force_login(self.admin)

    def test_tasks_entered_while_adding_fill_default_tasks_instead_of_duplicating(self):
        url = reverse('admin:texts_anthology_add')
        data = admin_form_data(self.client, url, 'anthology_form', title='Nowa z adminu', _save='Zapisz', **{
            'production_tasks-TOTAL_FORMS': '1', 'production_tasks-0-task_type': 'typesetting',
            'production_tasks-0-status': 'commissioned', 'production_tasks-0-assigned_to': self.person.pk})
        self.assertEqual(self.client.post(url, data).status_code, 302)
        book = Anthology.objects.get(title='Nowa z adminu')
        self.assertEqual(book.production_tasks.count(), len(AnthologyTask.TaskType.values))
        task = book.production_tasks.get(task_type='typesetting')
        self.assertEqual((task.status, task.assigned_to), ('commissioned', self.person))
        self.assertIsNotNone(task.commissioned_at)

    def test_novel_form_hides_illustration_and_translation_flags_unless_translation_needs_fixing(self):
        model_admin = admin.site.get_model_admin(Anthology)
        request = RequestFactory().get('/')
        request.user = self.admin

        def fields(book):
            return {name for _, options in model_admin.get_fieldsets(request, book) for name in options['fields']}

        ordinary = Anthology.objects.create(title='Zwykła')
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        self.assertTrue({'has_illustrations', 'is_translated'} <= fields(ordinary))
        self.assertFalse({'has_illustrations', 'is_translated'} & fields(novel))
        page = self.client.get(reverse('admin:texts_anthology_change', args=[novel.pk]))
        self.assertNotContains(page, 'name="has_illustrations"')
        # Dawny błędny zapis „powieść tłumaczona” zostaje widoczny, żeby dało się go poprawić.
        Anthology.objects.filter(pk=novel.pk).update(is_translated=True)
        novel.refresh_from_db()
        self.assertIn('is_translated', fields(novel))
        self.assertNotIn('has_illustrations', fields(novel))

    def test_review_anthology_autocomplete_offers_only_open_regular_anthologies(self):
        open_book = Anthology.objects.create(title='Nabór otwarty')
        Anthology.objects.create(title='Nabór wydany', status='ready')
        Anthology.objects.create(title='Nabór powieść', is_novel=True)
        url = reverse('admin:autocomplete')
        params = {'app_label': 'texts', 'model_name': 'review', 'field_name': 'anthology', 'term': 'Nabór'}
        found = [row['id'] for row in self.client.get(url, params).json()['results']]
        self.assertEqual(found, [str(open_book.pk)])
        params['model_name'] = 'text'
        self.assertEqual(len(self.client.get(url, params).json()['results']), 3)


class TextAdminScopeTests(TestCase):
    """Formularz tekstu w adminie dla rozdziałów, przekładów i zwykłych tekstów oraz listy tłumaczeń."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('text-scope-admin')
        cls.author = Author.objects.create(first_name='Jan', last_name='Kowalski', email=None)
        cls.novel = Anthology.objects.create(title='Powieść adminowa', is_novel=True)
        cls.novel.novel.authors.add(cls.author)
        cls.novel.novel.tags, cls.novel.novel.genre = 'magia', 'fantasy'
        cls.novel.novel.save()
        cls.chapter = new_chapter(cls.novel, cls.novel.novel, chapter_number=1)
        cls.translated = Anthology.objects.create(title='Przekłady adminowe', is_translated=True)
        cls.foreign_text = Text.objects.create(title='Przekład z autorem', anthology=cls.translated, length=10)
        cls.foreign_text.translation.foreign_authors.add(ForeignAuthor.objects.create(
            first_name='Ursula', last_name='Obca', email='ursula@example.test'))
        cls.foreign_text.translation.translators.add(Translator.objects.create(first_name='Anna', last_name='Tłumaczka'))
        cls.bare_text = Text.objects.create(title='Przekład bez osób', anthology=cls.translated, length=10)
        cls.regular_book = Anthology.objects.create(title='Krajowa adminowa')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_chapter_metadata_comes_from_novel_and_cannot_be_edited_in_text_form(self):
        form = TextAdminForm(instance=self.chapter)
        for name in ('title', 'file_url', 'content_warnings'):
            self.assertTrue(form.fields[name].disabled)
            self.assertIsInstance(form.fields[name].widget, HiddenInput)
        self.assertFalse(form.fields['authors'].required)
        self.assertIn('danych całej powieści', form.fields['authors'].help_text)
        form = TextAdminForm(data={'title': 'Podmieniony', 'anthology': self.novel.pk, 'chapter_number': 1,
                                   'current_workflow_cycle': 1}, instance=self.chapter)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['title'], self.chapter.title)

    def test_admin_save_of_chapter_restores_novel_authors_and_vocabulary(self):
        Text.objects.filter(pk=self.chapter.pk).update(tags='stare', genre='inne')
        self.chapter.authors.clear()
        url = reverse('admin:texts_text_change', args=[self.chapter.pk])
        data = admin_form_data(self.client, url, 'text_form', length='1234', _save='Zapisz')
        self.assertEqual(self.client.post(url, data).status_code, 302)
        self.chapter.refresh_from_db()
        self.assertEqual(list(self.chapter.authors.all()), [self.author])
        self.assertEqual((self.chapter.tags, self.chapter.genre, self.chapter.length), ('magia', 'fantasy', 1234))

    def test_author_rules_differ_for_novels_translations_and_regular_texts(self):
        empty_novel = Anthology.objects.create(title='Powieść bez autorów', is_novel=True)
        cases = [
            ({'anthology': empty_novel.pk, 'chapter_number': 1}, None, 'Najpierw przypisz autorów'),
            ({'anthology': self.translated.pk, 'authors': [self.author.pk]}, 'authors', 'sekcji tłumaczenia'),
            ({'anthology': self.regular_book.pk}, 'authors', 'Wybierz autora.'),
        ]
        for values, field, message in cases:
            with self.subTest(values=values):
                form = TextAdminForm(data={'title': 'Nowy', 'length': 10, 'current_workflow_cycle': 1, **values})
                self.assertFalse(form.is_valid())
                errors = form.errors.get(field or '__all__', [])
                self.assertTrue(any(message in error for error in errors), form.errors)
        form = TextAdminForm(data={'title': 'Nowy', 'length': 10, 'current_workflow_cycle': 1,
                                   'anthology': self.regular_book.pk, 'authors': [self.author.pk]})
        self.assertTrue(form.is_valid(), form.errors)

    def test_translation_lists_show_foreign_people_and_dash_when_missing(self):
        Text.objects.create(title='Tekst krajowy', anthology=self.regular_book, length=5).authors.add(self.author)
        response = self.client.get(reverse('admin:texts_texttranslation_changelist'))
        self.assertEqual(response.status_code, 200)
        rows = {str(row.text): row for row in response.context['cl'].result_list}
        self.assertEqual(set(rows), {'Przekład z autorem', 'Przekład bez osób'})
        self.assertContains(response, 'Ursula Obca')
        self.assertContains(response, 'Anna Tłumaczka')
        self.assertContains(response, '<td class="field-display_translators">–</td>', html=True)
        response = self.client.get(reverse('admin:texts_text_changelist'), {'anthology__id__exact': self.translated.pk})
        self.assertContains(response, 'Ursula Obca')
        self.assertContains(response, 'ursula@example.test')
        self.assertNotContains(response, 'Jan Kowalski')
