from tempfile import TemporaryDirectory
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import Client, TestCase
from django.urls import reverse
from lxml import html

from core.odkurzacz_forms import OdkurzaczForm
from illustrations.editing import AssignmentForm
from illustrations.models import Illustration
from people.models import Person, Role
from texts.models import Anthology, Text
from workflow.tests import create_member


class IllustratorDirectoryTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin=get_user_model().objects.create_superuser('admin-v6','admin-v6@example.test','test-only')
        cls.coordinator=create_member('koord-v6','Koordynator redakcji')
        cls.artist=create_member('artist-v6','Ilustrator')
        cls.editor=create_member('editor-v6','Redaktor')
        cls.role=Role.objects.get(name='Ilustrator')
        cls.person=cls.artist.person_profile
        cls.manual=Person.objects.create(first_name='Anna',last_name='Rysująca',email='anna-v6@example.test',
            illustrator_portfolio='https://example.org/portfolio',illustrator_preferences='Fantasy i krajobrazy',illustrator_covers=True)
        cls.manual.roles.add(cls.role)

    def setUp(self):
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup)
        override=self.settings(ACTIVITY_SPOOL_DIR=temporary.name);override.enable();self.addCleanup(override.disable)
        self.client.force_login(self.admin)
        self.list_url=reverse('illustrations:illustrator_list')
        self.add_url=reverse('illustrations:illustrator_add')
        self.edit_url=reverse('illustrations:illustrator_edit',args=[self.manual.pk])

    def token(self,url=None):
        response=self.client.get(url or self.edit_url)
        self.assertEqual(response.status_code,200)
        return html.fromstring(response.content).xpath('//input[@name="version"]/@value')[0]

    def data(self,**kwargs):
        return {'first_name':'Anna','last_name':'Rysująca','email':'anna-v6@example.test',
            'illustrator_portfolio':'https://example.org/portfolio',
            'illustrator_preferences':'Nowe preferencje','illustrator_covers':'on',**kwargs}

    def test_label_is_shortened(self):
        self.assertEqual(OdkurzaczForm().fields['normalize_formatting'].label,'Ujednolić formatowanie')
        response=self.client.get(reverse('core:programs'))
        self.assertContains(response,'Ujednolić formatowanie')
        self.assertNotContains(response,'Ujednolić formatowanie jak przy pobieraniu ze skrzynki')

    def test_directory_lists_accounts_and_manual_people_once_with_all_columns(self):
        before=Person.objects.count()
        response=self.client.get(self.list_url)
        for value in ('Imię i nazwisko','Adres e-mail','Portfolio','Preferencje','Okładki',
                      'Anna Rysująca','Fantasy i krajobrazy',self.person.email,'https://example.org/portfolio'):
            self.assertContains(response,value)
        self.assertNotContains(response,self.editor.person_profile.email)
        self.assertEqual(response.context['page_obj'].paginator.count,2)
        self.assertEqual(Person.objects.count(),before)

    def test_coordinator_and_superuser_can_read_add_and_edit(self):
        for user in (self.admin,self.coordinator):
            self.client.force_login(user)
            for url in (self.list_url,self.add_url,self.edit_url):
                self.assertEqual(self.client.get(url).status_code,200)

    def test_artist_editor_and_staff_cannot_access_or_post(self):
        self.editor.is_staff=True;self.editor.save()
        self.editor.user_permissions.add(*Permission.objects.filter(content_type__app_label='people',codename__in=['view_person','change_person','add_person']))
        for user in (self.artist,self.editor):
            self.client.force_login(user)
            for url in (self.list_url,self.add_url,self.edit_url):
                self.assertEqual(self.client.get(url).status_code,403)
            for url in (self.add_url,self.edit_url):
                self.assertEqual(self.client.post(url,self.data()).status_code,403)
        self.manual.refresh_from_db();self.assertEqual(self.manual.illustrator_preferences,'Fantasy i krajobrazy')

    def test_anonymous_and_inactive_coordinator_are_blocked(self):
        self.client.logout();self.assertEqual(self.client.get(self.list_url).status_code,302)
        Person.objects.filter(pk=self.coordinator.person_profile.pk).update(is_active=False)
        self.client.force_login(self.coordinator);self.assertEqual(self.client.get(self.list_url).status_code,403)

    def test_navigation_only_for_coordinators_directly_below_illustrations(self):
        for user in (self.admin,self.coordinator,self.artist,self.editor):
            self.client.force_login(user)
            response=self.client.get(reverse('core:programs'))
            links=html.fromstring(response.content).xpath('//nav[@aria-label="Główna nawigacja"]//a/@href')
            if user in (self.admin,self.coordinator):
                self.assertEqual(links.index(self.list_url),links.index(reverse('illustrations:illustration_list'))+1)
            else:self.assertNotIn(self.list_url,links)

    def test_manual_creation_does_not_create_account_and_can_be_assigned(self):
        accounts=get_user_model().objects.count()
        self.client.force_login(self.coordinator)
        response=self.client.post(self.add_url,self.data(first_name='Nowa',email='new-v6@example.test'))
        self.assertRedirects(response,self.list_url)
        person=Person.objects.get(email='new-v6@example.test')
        self.assertIsNone(person.user_id)
        self.assertFalse(person.is_coordinator)
        self.assertTrue(person.roles.filter(name='Ilustrator').exists())
        self.assertEqual(get_user_model().objects.count(),accounts)
        book=Anthology.objects.create(title='Nowa',has_illustrations=True)
        text=Text.objects.create(title='Tekst',anthology=book,length=100)
        form=AssignmentForm(instance=Illustration.objects.get(text=text),can_assign=True)
        self.assertIn(person,form.fields['illustrator'].queryset)

    def test_manual_edit_and_unchecked_cover_persist(self):
        response=self.client.post(self.edit_url,self.data(version=self.token(),illustrator_covers=''))
        self.assertRedirects(response,self.list_url)
        self.manual.refresh_from_db();self.assertFalse(self.manual.illustrator_covers)
        self.assertEqual(self.manual.illustrator_preferences,'Nowe preferencje')

    def test_account_identity_and_security_fields_cannot_be_overwritten(self):
        url=reverse('illustrations:illustrator_edit',args=[self.person.pk])
        before=(self.person.first_name,self.person.last_name,self.person.email,self.person.user_id)
        response=self.client.post(url,self.data(version=self.token(url),email='forged@example.test',
            user=self.admin.pk,is_coordinator='on',roles=Role.objects.get(name='Koordynator redakcji').pk))
        self.assertRedirects(response,self.list_url)
        self.person.refresh_from_db()
        self.assertEqual((self.person.first_name,self.person.last_name,self.person.email,self.person.user_id),before)
        self.assertFalse(self.person.is_coordinator)
        self.assertEqual(self.person.illustrator_preferences,'Nowe preferencje')

    def test_duplicate_email_case_insensitive_does_not_create_or_attach_account(self):
        count=Person.objects.count()
        response=self.client.post(self.add_url,self.data(email=self.person.email.upper()))
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Osoba z tym adresem e-mail już istnieje')
        self.assertEqual(Person.objects.count(),count)

    def test_invalid_portfolio_email_and_blank_name_are_rejected(self):
        count=Person.objects.count()
        for override in ({'illustrator_portfolio':'javascript:alert(1)'},{'illustrator_portfolio':'ftp://example.org/file'},
                         {'email':'nie-email'},{'first_name':' '}):
            response=self.client.post(self.add_url,self.data(email='unique-v6@example.test',**override) if 'email' not in override else self.data(**override))
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.context['form'].errors)
        self.assertEqual(Person.objects.count(),count)

    def test_conflict_with_another_save_preserves_data(self):
        token=self.token()
        self.manual.illustrator_preferences='Zmiana z panelu admina';self.manual.save()
        response=self.client.post(self.edit_url,self.data(version=token))
        self.assertEqual(response.status_code,409)
        self.assertContains(response,'Nowe preferencje',status_code=409)
        self.manual.refresh_from_db();self.assertEqual(self.manual.illustrator_preferences,'Zmiana z panelu admina')
        self.assertEqual(self.client.post(self.edit_url,self.data()).status_code,409)

    def test_non_illustrator_id_cannot_be_edited(self):
        url=reverse('illustrations:illustrator_edit',args=[self.editor.person_profile.pk])
        self.assertEqual(self.client.get(url).status_code,404)
        self.assertEqual(self.client.post(url,self.data()).status_code,404)

    def test_csrf_and_html_escaping(self):
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.admin)
        self.assertEqual(csrf.post(self.add_url,self.data()).status_code,403)
        self.client.post(self.edit_url,self.data(version=self.token(),illustrator_preferences='<script>alert(1)</script>'))
        self.assertContains(self.client.get(self.list_url),'&lt;script&gt;alert(1)&lt;/script&gt;')

    def test_search_and_inactive_people_do_not_erase_old_contacts(self):
        self.manual.is_active=False;self.manual.save()
        response=self.client.get(self.list_url,{'q':'krajobrazy'})
        self.assertContains(response,'Anna Rysująca');self.assertContains(response,'Poza zespołem')
        self.assertEqual(response.context['page_obj'].paginator.count,1)

    def test_admin_creation_and_edit_use_same_person(self):
        url=reverse('admin:people_person_add')
        page=self.client.get(url)
        self.assertNotContains(page,'name="illustrator_portfolio"')
        response=self.client.post(url,self.data(first_name='Panel',email='panel-v6@example.test',roles=[self.role.pk],is_active='on',_save='Zapisz'))
        self.assertEqual(response.status_code,302)
        person=Person.objects.get(email='panel-v6@example.test')
        edit_url=reverse('admin:people_person_change',args=[person.pk])
        edit_page=self.client.get(edit_url)
        self.assertContains(edit_page,'name="illustrator_portfolio"')
        token=html.fromstring(edit_page.content).xpath('//input[@name="_edit_version"]/@value')[0]
        response=self.client.post(edit_url,self.data(first_name='Panel',email='panel-v6@example.test',roles=[self.role.pk],is_active='on',_save='Zapisz',_edit_version=token))
        self.assertEqual(response.status_code,302)
        person.refresh_from_db()
        self.assertTrue(person.illustrator_covers)
        self.assertIsNone(person.user_id)
        self.assertContains(self.client.get(self.list_url),'Panel Rysująca')
        self.assertEqual(Person.objects.filter(email='panel-v6@example.test').count(),1)
