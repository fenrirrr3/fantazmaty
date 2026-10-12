"""Ilustracje: lista i formularz w panelu admina, wyścig przy zapisie kontaktu w spisie, nieprawidłowy token
wspólnego linku oraz odmowy komendy merge_illustrator – przypadki dotąd nieobjęte testami."""
from io import StringIO
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.urls import reverse

from fantazmaty.testing import create_member, create_superuser
from illustrations.directory import IllustratorForm
from illustrations.models import Illustration, Illustrator, PublicIllustrationSettings
from texts.models import Anthology, Text


class IllustrationAdminGapTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('illustration-gaps')
        cls.book = Anthology.objects.create(title='Ilustrowana', has_illustrations=True)
        cls.text = Text.objects.create(title='Z antologii', anthology=cls.book, length=10)
        cls.illustration = Illustration.objects.get(text=cls.text)
        cls.loose = Illustration.objects.create(text=Text.objects.create(title='Bez antologii', length=10))

    def setUp(self):
        self.client.force_login(self.admin)

    def test_list_shows_title_anthology_status_and_missing_illustrator(self):
        response = self.client.get(reverse('admin:illustrations_illustration_changelist'))
        model_admin = response.context['cl'].model_admin
        rows = {model_admin.display_title(row): row for row in response.context['cl'].result_list}
        self.assertEqual({title: model_admin.display_anthology(row) for title, row in rows.items()},
                         {'Z antologii': 'Ilustrowana', 'Bez antologii': '–'})
        self.assertEqual(model_admin.display_illustrator(rows['Z antologii']), 'Nie przypisano')
        self.assertContains(response, '<span class="illustration-status illustration-status-unassigned">Nieprzypisane</span>',
                            html=True)

    def test_change_form_validates_story_link(self):
        url = reverse('admin:illustrations_illustration_change', args=[self.illustration.pk])
        page = self.client.get(url)
        data = {'status': 'unassigned', 'story_url': 'ftp://example.com/opowiadanie', 'trigger_warnings': '',
                'coordinator_notes': '', 'illustrated_excerpt': '', '_save': 'Zapisz',
                '_edit_version': page.context['request'].edit_version_token}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 200)
        self.assertIn('story_url', response.context['adminform'].form.errors)
        response = self.client.post(url, {**data, 'story_url': 'https://example.com/opowiadanie'})
        self.assertEqual(response.status_code, 302)
        self.illustration.refresh_from_db()
        self.assertEqual(self.illustration.story_url, 'https://example.com/opowiadanie')


class DirectoryAndPublicLinkGapTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('directory-gaps')
        cls.coordinator = create_member('koord-gaps', 'Koordynator redakcji')

    def test_contact_added_concurrently_with_same_email_is_reported(self):
        """Wpis z tym samym adresem powstał między walidacją a zapisem: formularz pokazuje błąd, nic się nie psuje."""
        self.client.force_login(self.admin)
        original = IllustratorForm.save

        def save_after_concurrent_insert(form, *args, **kwargs):
            Illustrator.objects.create(first_name='Równoległa', last_name='Osoba', email='rysuje@example.test')
            return original(form, *args, **kwargs)

        with patch.object(IllustratorForm, 'save', save_after_concurrent_insert), \
                patch('core.activity_spool.enqueue_activity'):
            response = self.client.post(reverse('illustrations:illustrator_add'), {
                'first_name': 'Ewa', 'last_name': 'Rysuje', 'email': 'rysuje@example.test'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('nie został już dodany', ' '.join(response.context['form'].errors['email']))
        # Zapis kontaktu został wycofany razem z próbą (równoległy wpis należy do innej transakcji).
        self.assertFalse(Illustrator.objects.filter(first_name='Ewa').exists())

    def test_shared_link_with_forged_version_is_refused(self):
        self.client.force_login(self.coordinator)
        response = self.client.post(reverse('illustrations:illustration_list'), {
            'drive_url': 'https://drive.google.com/drive/folders/x', 'version': 'podrobiony-token'})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(PublicIllustrationSettings.objects.exclude(drive_url='').exists())


class MergeIllustratorRefusalTests(TestCase):
    def merge(self, first='Przemek', last='Świszcz', pseudonym='Graphos'):
        call_command('merge_illustrator', first_name=first, last_name=last, pseudonym=pseudonym, apply=True,
                     stdout=StringIO())

    def test_refusals_change_nothing(self):
        target = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz')
        cases = {
            'pseudonim taki jak imię i nazwisko': (lambda: None, {'pseudonym': ' przemek  świszcz '},
                                                  'odrębny pseudonim'),
            'brak wpisu imiennego': (lambda: None, {'first': 'Nikt'}, 'znaleziono: 0'),
            'wpis imienny ma inny pseudonim': (
                lambda: Illustrator.objects.filter(pk=target.pk).update(pseudonym='Inny'), {}, 'inny pseudonim'),
            'wpis do scalenia ma drugi pseudonim': (
                lambda: Illustrator.objects.create(first_name='Graphos', pseudonym='Grafik'), {},
                'dodatkowy, inny pseudonim'),
            'pseudonim za długi dla wpisu': (lambda: None, {'pseudonym': 'P' * 101}, 'Nic nie zapisano'),
        }
        for case, (prepare, arguments, message) in cases.items():
            with self.subTest(case):
                Illustrator.objects.filter(pk=target.pk).update(pseudonym='')
                Illustrator.objects.exclude(pk=target.pk).delete()
                prepare()
                before = list(Illustrator.objects.order_by('pk').values())
                with self.assertRaisesMessage(CommandError, message):
                    self.merge(**arguments)
                self.assertEqual(list(Illustrator.objects.order_by('pk').values()), before)
