"""Kontakty zewnętrzne (lektorzy, ilustratorzy): historia i prywatność, podpowiedzi, aktywność, duplikaty i konflikty zapisu."""
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from lxml import html

from core.edit_versions import version_of
from core.models import AudioContributor, Audiobook
from fantazmaty.testing import create_member, create_superuser, form_value
from illustrations.editing import AssignmentForm, edit_token
from illustrations.models import Illustration, Illustrator, PublicIllustrationSettings
from illustrations.public import link_version
from texts.models import Anthology, NovelProfile, Text
from texts.novels import new_chapter


class AudioContactTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.coordinator = create_member('v63-editor', 'Koordynator redakcji')
        cls.member = create_member('v63-member', 'Redaktor')
        cls.book = Anthology.objects.create(title='Antologia v63', status='ready')

    def test_contact_history_survives_rename_and_privacy(self):
        """Wpis spisu nie jest powiązany z kontem – także gdy adres zgadza się z kontem członka zespołu."""
        narrator = create_member('v63-narrator', 'Lektor')
        contact = AudioContributor.objects.create(name='Czytający', email=narrator.person_profile.email.upper())
        self.assertFalse(hasattr(contact, 'user_id'))
        for i in range(3):
            text = Text.objects.create(title=f'Nagranie {i}', anthology=self.book, length=1)
            Audiobook.objects.create(text=text, narrator_contact=contact, status='published' if i < 2 else 'recording')
        contact.name = 'Poprawiony podpis'
        contact.save()
        self.assertEqual(Audiobook.objects.filter(narrator_name=contact.name).count(), 3)
        url = reverse('core:audio_contributor', args=[contact.pk])
        self.client.force_login(narrator)
        page = self.client.get(url)
        self.assertEqual(len(page.context['works']), 2)
        self.assertContains(self.client.get(reverse('core:audiobooks')), url)
        self.assertNotContains(page, contact.email)
        self.assertNotContains(self.client.get(reverse('core:person_detail', args=[narrator.person_profile.pk])), url)
        self.client.force_login(self.coordinator)
        self.assertContains(self.client.get(url), contact.email)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_suggestions_require_coordinator_and_search_name_or_email(self):
        contact = AudioContributor.objects.create(name='Jan Lektor', email='lekt@example.test')
        Illustrator.objects.create(first_name='Anna', last_name='Rysuje', email='rys@example.test')
        Illustrator.objects.create(first_name='Ukryta', last_name='Osoba', email='old@example.test', is_active=False)
        url = reverse('core:contact_suggestions')
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(url, {'kind': 'audio', 'q': 'Jan'}).status_code, 403)
        self.client.force_login(self.coordinator)
        for query in ('Jan', 'lekt@'):
            rows = self.client.get(url, {'kind': 'audio', 'q': query}).json()['results']
            self.assertEqual(rows, [{'id': contact.pk, 'name': 'Jan Lektor', 'email': 'lekt@example.test'}])
        rows = self.client.get(url, {'kind': 'illustration', 'q': 'rys@'}).json()['results']
        self.assertEqual(rows[0]['name'], 'Anna Rysuje')
        self.assertEqual(self.client.get(url, {'kind': 'illustration', 'q': 'old@'}).json()['results'], [])


class IllustratorContactTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('ui34')
        cls.active = Illustrator.objects.create(first_name='Anna', last_name='Aktywna')
        cls.inactive = Illustrator.objects.create(first_name='Jan', last_name='Nieaktywny', is_active=False)
        cls.book = Anthology.objects.create(title='Antologia', has_illustrations=True)
        cls.story = Text.objects.create(title='Tekst', anthology=cls.book, length=100)
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        new_chapter(novel, NovelProfile.objects.get_or_create(anthology=novel)[0], chapter_number=1)

    def setUp(self):
        self.client.force_login(self.admin)
        self.row = Illustration.objects.get(text=self.story)

    def token(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return form_value(response)

    def test_inactive_filter_is_opt_in_and_works_with_search(self):
        url = reverse('illustrations:illustrator_list')
        response = self.client.get(url)
        self.assertEqual(list(response.context['page_obj']), [self.active])
        self.assertFalse(html.fromstring(response.content).xpath('//input[@name="show_inactive"][@checked]'))
        response = self.client.get(url, {'show_inactive': '1'})
        self.assertEqual(set(response.context['page_obj']), {self.active, self.inactive})
        self.assertContains(response, '(nieaktywny)')
        self.assertTrue(html.fromstring(response.content).xpath('//input[@name="show_inactive"][@checked]'))
        response = self.client.get(url, {'show_inactive': '1', 'q': 'Nieaktywny'})
        self.assertEqual(list(response.context['page_obj']), [self.inactive])

    def test_pagination_preserves_inactive_filter(self):
        Illustrator.objects.bulk_create([Illustrator(first_name='Kontakt', last_name=str(i)) for i in range(30)])
        response = self.client.get(reverse('illustrations:illustrator_list'), {'show_inactive': '1', 'page_size': '25'})
        links = html.fromstring(response.content).xpath('//a[contains(@href,"page=2")]/@href')
        self.assertTrue(links)
        self.assertTrue(all('show_inactive=1' in link for link in links))

    def test_picker_retains_existing_inactive_selection_without_exposing_full_list(self):
        self.row.set_artists([self.active, self.inactive], status='assigned')
        response = self.client.get(reverse('illustrations:illustration_detail', args=[self.row.pk]))
        self.assertEqual(response.status_code, 200)
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//*[@id="illustrator-source"][@hidden]//input[@checked]')), 2)
        self.assertTrue(doc.xpath('//*[@id="illustrator-search-results"][@hidden]'))
        self.assertFalse(doc.xpath('//select[@name="illustrators"]'))
        self.assertRegex(response.content.decode(), r'illustrator_search(\.[0-9a-f]+)?\.js')

    def test_inactive_new_assignment_blocked_both_forms_existing_credit_retained(self):
        artist = Illustrator.objects.create(first_name='Nieaktywny', is_active=False)
        form = AssignmentForm(data={'illustrators': [artist.pk], 'status': 'assigned',
                                    'version': edit_token(self.admin, self.row)}, instance=self.row, can_assign=True)
        self.assertFalse(form.is_valid())
        self.assertIn('illustrators', form.errors)
        url = reverse('admin:illustrations_illustration_change', args=[self.row.pk])
        response = self.client.post(url, {'_edit_version': self.token(url), 'text': self.story.pk,
                                          'illustrators': [artist.pk], 'status': 'assigned', '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('illustrators', response.context['adminform'].form.errors)
        self.assertFalse(self.row.illustrators.exists())
        self.row.set_artists([artist], status='assigned')  # Historyczne przypisanie.
        response = self.client.post(url, {'_edit_version': self.token(url), 'text': self.story.pk,
                                          'illustrators': [artist.pk], 'status': 'delivered', '_save': 'Zapisz'})
        self.assertEqual(response.status_code, 302)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'delivered')
        self.assertEqual(list(self.row.illustrators.all()), [artist])
        found = self.client.get(reverse('admin:autocomplete'), {
            'app_label': 'illustrations', 'model_name': 'illustration', 'field_name': 'illustrators',
            'term': 'Nieaktywny'})
        self.assertEqual(found.status_code, 200)
        self.assertEqual(found.json()['results'], [])

    def test_drive_conflicts_between_admin_and_website_and_two_admin_forms(self):
        old = 'https://drive.google.com/drive/folders/old'
        new = 'https://drive.google.com/drive/folders/new'
        last = 'https://drive.google.com/drive/folders/last'
        settings = PublicIllustrationSettings.objects.create(drive_url=old)
        url = reverse('admin:illustrations_publicillustrationsettings_change', args=[1])
        token = self.token(url)
        version = version_of(settings)
        internal = reverse('illustrations:illustration_list')
        self.assertEqual(self.client.post(internal, {'drive_url': new,
                                                     'version': link_version(self.admin, old)}).status_code, 302)
        self.assertGreater(version_of(settings), version)
        self.assertEqual(self.client.post(url, {'_edit_version': token, 'drive_url': old,
                                                '_save': 'Zapisz'}).status_code, 409)
        settings.refresh_from_db()
        self.assertEqual(settings.drive_url, new)
        token = self.token(url)
        self.assertEqual(self.client.post(url, {'_edit_version': token, 'drive_url': last,
                                                '_save': 'Zapisz'}).status_code, 302)
        self.assertEqual(self.client.post(url, {'_edit_version': token, 'drive_url': old,
                                                '_save': 'Zapisz'}).status_code, 409)
        self.assertEqual(self.client.post(internal, {'drive_url': old,
                                                     'version': link_version(self.admin, new)}).status_code, 409)
        settings.refresh_from_db()
        self.assertEqual(settings.drive_url, last)

    def test_duplicate_alias_blocked_on_both_forms_and_full_name_collision(self):
        artist = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz', pseudonym='Graphos')
        count = Illustrator.objects.count()
        payload = {'first_name': 'Inny', 'last_name': 'Artysta', 'pseudonym': '  gRaPhOs  ', 'is_active': 'on'}
        for url in (reverse('illustrations:illustrator_add'), reverse('admin:illustrations_illustrator_add')):
            result = self.client.post(url, payload)
            self.assertEqual(result.status_code, 200)
            self.assertContains(result, f'ID {artist.pk}')
        self.assertEqual(Illustrator.objects.count(), count)
        result = self.client.post(reverse('illustrations:illustrator_add'),
                                  {'first_name': 'Graphos', 'last_name': '', 'pseudonym': '', 'is_active': 'on'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Illustrator.objects.count(), count)
        Illustrator.objects.create(first_name='Drugi', last_name='Artysta')
        result = self.client.post(reverse('illustrations:illustrator_add'), {**payload, 'pseudonym': 'Drugi Artysta'})
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Illustrator.objects.count(), count + 1)

    def test_legacy_duplicate_labels_have_ids_and_can_edit_contacts_then_merge(self):
        artist = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz', pseudonym='Graphos')
        duplicate = Illustrator.objects.create(first_name='Graphos')
        count = Illustrator.objects.count()
        page = self.client.get(reverse('illustrations:illustrator_list'))
        self.assertContains(page, f'Przemek Świszcz (Graphos) (ID {artist.pk})')
        self.assertContains(page, f'Graphos (ID {duplicate.pk})')
        form = AssignmentForm(instance=self.row, can_assign=True)
        self.assertEqual(form.fields['illustrators'].label_from_instance(artist), f'Graphos (ID {artist.pk})')
        url = reverse('admin:illustrations_illustrator_change', args=[artist.pk])
        response = self.client.post(url, {
            '_edit_version': self.token(url), 'first_name': 'Przemek', 'last_name': 'Świszcz',
            'pseudonym': 'Graphos', 'email': 'updated@example.test', 'is_active': 'on'})
        self.assertEqual(response.status_code, 302)
        call_command('merge_illustrator', first_name='Przemek', last_name='Świszcz', pseudonym='Graphos',
                     apply=True, stdout=StringIO())
        self.assertEqual(Illustrator.objects.count(), count - 1)
        self.assertFalse(Illustrator.objects.filter(pk=duplicate.pk).exists())
        self.assertNotContains(self.client.get(reverse('illustrations:illustrator_list')), '(ID ')
