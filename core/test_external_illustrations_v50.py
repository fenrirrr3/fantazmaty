from django.contrib.auth import get_user_model
from django.test import TestCase, Client
from django.urls import reverse
from lxml import html
from core.models import Recruitment, RecruitmentMailSource
from core.views.recruitment_delete import deletion_token
from illustrations.models import Illustration, Illustrator, PublicIllustrationSettings
from illustrations.public import link_version
from people.models import Person, Role
from texts.models import Anthology, Text
from workflow.models import WorkflowStage
from pathlib import Path
import shutil
import subprocess


class ExternalIllustrationsTests(TestCase):
    def test_dialog_open_cancel_and_fallback(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for the dialog test')
        result = subprocess.run([node, str(Path(__file__).parent / 'js_tests/dialogs.cjs')], capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_superuser('external-admin', 'admin@example.test', 'test')
        cls.coordinator = get_user_model().objects.create_user('external-coordinator')
        cls.artist = get_user_model().objects.create_user('external-artist')
        for user, role in ((cls.coordinator, 'Koordynator'), (cls.artist, 'Ilustrator')):
            profile = Person.objects.create(user=user, first_name='Osoba', last_name=role)
            profile.roles.add(Role.objects.get_or_create(name=role)[0])
        cls.book = Anthology.objects.create(title='Antologia publiczna', has_illustrations=True)
        cls.text = Text.objects.create(title='Opowiadanie publiczne', anthology=cls.book, length=100, tags='kosmos, przyjaźń', genre='science fiction', file_url='https://example.test/private-folder')
        cls.illustration, _ = Illustration.objects.get_or_create(text=cls.text)
        Illustration.objects.filter(pk=cls.illustration.pk).update(story_url='https://example.test/private-story', illustrated_excerpt='TAJNY FRAGMENT', coordinator_notes='TAJNE UWAGI')
        cls.url = reverse('illustrations:external_illustrations')
        cls.internal = reverse('illustrations:illustration_list')

    def test_anonymous_projection_is_read_only_without_private_data(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'themes.')
        for value in ('Antologia publiczna', 'Opowiadanie publiczne', 'kosmos', 'science fiction', 'Dostępne'):
            self.assertContains(response, value)
        for value in ('TAJNY FRAGMENT', 'TAJNE UWAGI', 'private-story', 'private-folder', 'admin@example.test', 'site-sidebar'):
            self.assertNotContains(response, value)
        self.assertFalse(PublicIllustrationSettings.objects.exists())
        self.assertEqual(self.client.post(self.url, {'status':'assigned'}).status_code,405)
        for url in (self.internal, reverse('illustrations:illustration_detail', args=[self.illustration.pk]), reverse('core:recruitment_list'), reverse('core:home')):
            self.assertEqual(self.client.get(url).status_code,302)
        self.client.force_login(self.admin)
        # Even a logged-in admin receives the same public column projection.
        self.assertNotContains(self.client.get(self.url),'TAJNE UWAGI')
        self.assertNotContains(self.client.get(reverse('core:home')),self.url)

    def test_status_uses_people_not_stored_status_and_does_not_reveal_names(self):
        artist = Illustrator.objects.create(first_name='PRYWATNY ILUSTRATOR', email='private-artist@example.test')
        self.illustration.illustrators.add(artist)
        response = self.client.get(self.url, {'status':'Przypisane'})
        self.assertContains(response, self.text.title)
        self.assertNotContains(response, 'PRYWATNY ILUSTRATOR')
        self.assertNotContains(response, 'private-artist@example.test')
        self.illustration.illustrators.clear()
        Illustration.objects.filter(pk=self.illustration.pk).update(manual_illustrator_name='RĘCZNY ILUSTRATOR', manual_illustrator_email='manual@example.test')
        self.assertContains(self.client.get(self.url, {'status':'Przypisane'}), self.text.title)
        self.assertNotContains(self.client.get(self.url),'RĘCZNY ILUSTRATOR')

    def test_scope_filters_sorting_pagination_and_escaping(self):
        second = Text.objects.create(title='<script>nie wykonuj</script>', anthology=self.book, length=1, tags='smoki', genre='fantasy')
        Illustration.objects.get_or_create(text=second)
        result = self.client.get(self.url, {'q':'smoki','sort':'-title'})
        self.assertContains(result,'&lt;script&gt;')
        self.assertNotContains(result,'<script>nie wykonuj')
        self.assertNotContains(result,self.text.title)
        for sort in ('anthology','title','tags','genre','status','-status','coordinator_notes'):
            response = self.client.get(self.url, {'sort':sort,'page_size':'500'})
            self.assertEqual(response.status_code,200)
            self.assertNotContains(response,'TAJNE UWAGI')
        # Historical released catalogue fixture; transition rules are tested elsewhere.
        Anthology.objects.filter(pk=self.book.pk).update(status=Anthology.Status.READY)
        self.assertNotContains(self.client.get(self.url),self.text.title)
        self.assertContains(self.client.get(self.url, {'hide_published':'0'}),self.text.title)
        for flag in ('is_translated','is_novel'):
            Anthology.objects.filter(pk=self.book.pk).update(**{flag:True})
            self.assertNotContains(self.client.get(self.url, {'hide_published':'0'}),self.text.title)
            Anthology.objects.filter(pk=self.book.pk).update(**{flag:False})
        WorkflowStage.objects.filter(text=self.text).update(is_current=False)
        WorkflowStage.objects.create(text=self.text, stage_type=WorkflowStage.StageType.WITHDRAWN, is_current=True, is_released=True)
        self.assertNotContains(self.client.get(self.url, {'hide_published':'0'}),self.text.title)

    def test_shared_link_validation_permissions_conflict_and_layout(self):
        self.assertEqual(self.client.post(self.internal, {'drive_url':'https://drive.google.com/x'}).status_code,302)
        self.client.force_login(self.artist)
        self.assertEqual(self.client.post(self.internal, {'drive_url':'https://drive.google.com/x'}).status_code,403)
        self.assertNotContains(self.client.get(self.internal),'data-dialog-open="public-illustration-link"')
        self.client.force_login(self.coordinator)
        page = self.client.get(self.internal)
        doc = html.fromstring(page.content)
        self.assertTrue(doc.xpath('//div[@class="illustration-public-actions"]/a'))
        public_link = doc.xpath('//a[@href="%s"]' % self.url)[0]
        self.assertEqual(public_link.get("target"), "_blank")
        self.assertEqual(set(public_link.get("rel").split()), {"noopener", "noreferrer"})
        self.assertTrue(doc.xpath('//div[@class="illustration-filter-bottom"]/label/input[@name="hide_published"]'))
        token = link_version(self.coordinator,'')
        for url in ('javascript:alert(1)','https://drive.google.com.evil.test/x','http://drive.google.com/x','https://drive.google.com:wrong/x'):
            self.assertEqual(self.client.post(self.internal, {'drive_url':url,'version':token}).status_code,400)
        self.assertFalse(PublicIllustrationSettings.objects.exists())
        link = 'https://drive.google.com/drive/folders/example?usp=sharing'
        self.assertEqual(self.client.post(self.internal, {'drive_url':link,'version':token}).status_code,302)
        self.assertContains(self.client.get(self.url),link)
        self.assertEqual(self.client.post(self.internal, {'drive_url':'','version':token}).status_code,409)
        self.assertEqual(PublicIllustrationSettings.objects.get().drive_url,link)
        self.assertEqual(self.client.post(self.internal, {'drive_url':'','version':link_version(self.coordinator,link)}).status_code,302)
        self.assertNotContains(self.client.get(self.url),'Otwórz GDrive')
        csrf = Client(enforce_csrf_checks=True);csrf.force_login(self.coordinator)
        self.assertEqual(csrf.post(self.internal, {'drive_url':link,'version':token}).status_code,403)


class RecruitmentDeletionTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser('delete-admin','admin@example.test','test')
        self.coordinator = get_user_model().objects.create_user('delete-coordinator')
        p = Person.objects.create(user=self.coordinator, first_name='Anna', last_name='Koordynator')
        p.roles.add(Role.objects.get_or_create(name='Koordynator')[0])
        self.record = Recruitment.objects.create(first_name='Jan', last_name='Kandydat', mail_roles=['editors','proofreaders'])
        RecruitmentMailSource.objects.create(recruitment=self.record, mailbox_key='key', uid_validity=1, uid=2)
        self.url = reverse('core:recruitment_delete', args=[self.record.pk])
        self.detail = reverse('core:recruitment_detail', args=[self.record.pk])

    def test_only_superuser_sees_button_and_can_delete(self):
        self.assertEqual(self.client.post(self.url).status_code,302)
        self.client.force_login(self.coordinator)
        self.assertNotContains(self.client.get(self.detail),'Usuń zgłoszenie')
        for method in ('get','post'):
            self.assertEqual(getattr(self.client,method)(self.url).status_code,403)
        self.client.force_login(self.admin)
        page = self.client.get(self.detail)
        self.assertContains(page,'cms-danger-button');self.assertContains(page,'Odśwież dane')
        self.assertEqual(self.client.get(self.url).status_code,200)
        self.assertTrue(Recruitment.objects.filter(pk=self.record.pk).exists())
        other = Recruitment.objects.create(first_name='Inna osoba')
        result = self.client.post(self.url, {'version':deletion_token(self.admin,self.record)})
        self.assertRedirects(result,reverse('core:recruitment_list'))
        self.assertFalse(Recruitment.objects.filter(pk=self.record.pk).exists())
        self.assertFalse(RecruitmentMailSource.objects.exists())
        self.assertFalse(self.record.role_decisions.exists())
        self.assertTrue(Recruitment.objects.filter(pk=other.pk).exists())

    def test_csrf_and_newer_decision_prevent_deletion(self):
        self.client.force_login(self.admin)
        csrf = Client(enforce_csrf_checks=True);csrf.force_login(self.admin)
        self.assertEqual(csrf.post(self.url).status_code,403)
        token = deletion_token(self.admin,self.record)
        decision = self.record.role_decisions.first();decision.decision_reason='Nowe uzasadnienie';decision.save()
        self.assertEqual(self.client.post(self.url, {'version':token}).status_code,409)
        self.assertEqual(self.client.post(self.url, {'version':'forged'}).status_code,409)
        self.assertTrue(Recruitment.objects.filter(pk=self.record.pk).exists())
