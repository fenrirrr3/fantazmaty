from tempfile import TemporaryDirectory
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from authors.models import Author
from texts.models import Anthology, Text, TextTranslation, Review, ForeignAuthor, Translator
from illustrations.models import Illustration
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.tests import create_member
from core.translation_scope import ordinary
from core.selectors.texts import user_workflow_summary, available_stages_for_user
from core.edit_versions import version_of


class TranslationTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin=get_user_model().objects.create_superuser('tr-admin','tr-admin@example.test','test')
        cls.member=create_member('tr-member','Redaktor')
        cls.normal=Anthology.objects.create(title='Księga krajowa',has_illustrations=True)
        cls.book=Anthology.objects.create(title='TRANSLATED UNIQUE ANTHOLOGY',is_translated=True,has_illustrations=True)
        cls.author=ForeignAuthor.objects.create(first_name='Autor',last_name='Oryginału',email='author-private@example.test')
        cls.translator=Translator.objects.create(first_name='Anna',last_name='Żak',email='translator-private@example.test')
        cls.other=Translator.objects.create(first_name='Beata',last_name='Adamska',email='')
        cls.text=Text.objects.create(title='TRANSLATED UNIQUE STORY',length=500,anthology=cls.book)
        cls.text.translation.foreign_authors.add(cls.author)
        cls.text.translation.translators.add(cls.translator)
        cls.regular=Text.objects.create(title='Tekst zwykły',length=100,anthology=cls.normal)
        cls.domestic=Author.objects.create(first_name='Polski',last_name='Autor',email=None)
        cls.regular.authors.add(cls.domestic)
        cls.assignment=A.objects.create(text=cls.text,role='editor',assigned_to=cls.member)
        cls.stage=S.objects.create(text=cls.text,stage_type='editing',assignment=cls.assignment,started_at=timezone.localdate())
        cls.review=Review.objects.create(title='TRANSLATED UNIQUE REVIEW',anthology=cls.book,length=500,
            author_first_name='Autor',author_last_name='Oryginału',email='review-private@example.test')

    def setUp(self):
        tmp=TemporaryDirectory();self.addCleanup(tmp.cleanup)
        setting=self.settings(ACTIVITY_SPOOL_DIR=tmp.name);setting.enable();self.addCleanup(setting.disable)
        self.client.force_login(self.admin)

    def test_auto_creation_toggle_moving_and_history_retention(self):
        self.assertEqual(TextTranslation.objects.count(),1)
        self.assertFalse(Illustration.objects.filter(text=self.text).exists())
        self.normal.is_translated=True;self.normal.save()
        row=TextTranslation.objects.get(text=self.regular);row.translators.add(self.other)
        self.normal.save();self.assertEqual(TextTranslation.objects.filter(text=self.regular).count(),1)
        self.normal.is_translated=False;self.normal.save()
        self.assertEqual(list(row.translators.all()),[self.other])
        self.assertIn(self.regular,ordinary(Text.objects))
        self.regular.anthology=self.book;self.regular.save()
        self.assertEqual(TextTranslation.objects.get(text=self.regular).pk,row.pk)
        self.assertEqual(list(row.translators.all()),[self.other])

    def test_translation_list_same_columns_search_filter_sort_and_empty_anthology(self):
        blank=Anthology.objects.create(title='Puste tłumaczenie',is_translated=True)
        url=reverse('core:translation_list')
        for params in ({},{'q':'Żak'},{'translator':str(self.translator.pk)},{'anthology':str(self.book.pk)},
                       {'sort':'translators'},{'sort':'-length'},{'status':'editing'}):
            response=self.client.get(url,params)
            self.assertEqual(response.status_code,200)
            self.assertEqual([r['pk'] for r in response.context['texts']],[self.text.pk])
            self.assertContains(response,'Anna Żak')
            for heading in ['Antologia','Tytuł','Autorzy','Tłumacz','Długość','Etap pracy','Rozpoczęcie etapu','Ostatnia zmiana statusu']:
                self.assertContains(response,heading)
        response=self.client.get(url,{'anthology':blank.pk})
        self.assertContains(response,'Puste tłumaczenie')
        self.assertEqual(response.context['page_obj'].paginator.count,0)
        response=self.client.get(url,{'anthology':self.normal.pk})
        self.assertEqual(response.context['page_obj'].paginator.count,0)

    def test_ordinary_lists_search_and_filters_do_not_expose_translation_records(self):
        routes=['text_list','tag_list','review_list','workflow_list','anthology_list','home','global_search',
                'my_texts','anthology_corrections','data_integrity','author_list']
        for route in routes:
            with self.subTest(route=route):
                response=self.client.get(reverse('core:'+route),{'hide_ready':'0','q':'TRANSLATED UNIQUE'})
                self.assertEqual(response.status_code,200)
                # A search input may echo q; inspect complete title strings instead.
                self.assertNotContains(response,self.text.title)
                self.assertNotContains(response,self.book.title)
                self.assertNotContains(response,self.review.title)
        response=self.client.get(reverse('core:text_list'),{'anthology':self.book.pk,'hide_ready':'0'})
        self.assertEqual(response.context['page_obj'].paginator.count,0)
        self.assertNotContains(response,self.book.title)
        response=self.client.get(reverse('illustrations:illustration_list'))
        self.assertEqual(response.status_code,200);self.assertNotContains(response,self.book.title)

    def test_home_workload_and_profile_exclude_translations(self):
        summary=user_workflow_summary(self.member)
        self.assertEqual(summary['active_stage_count'],0)
        self.assertEqual(summary['reserved_assignment_count'],0)
        rows=available_stages_for_user(user=self.admin)
        self.assertNotIn(self.stage.pk,[r.pk for r in rows])
        response=self.client.get(reverse('core:person_detail',args=[self.member.person_profile.pk]))
        self.assertEqual(response.status_code,200);self.assertNotContains(response,self.book.title)

    def token(self):
        response=self.client.get(reverse('core:translation_detail',args=[self.text.pk]))
        self.assertEqual(response.status_code,200)
        return html.fromstring(response.content).xpath('//form[contains(@action,"tlumacze")]/input[@name="_edit_version"]/@value')[0]

    def test_translation_profiles_form_preserves_author_and_detects_conflict(self):
        endpoint=reverse('core:set_translators',args=[self.text.pk]);token=self.token()
        response=self.client.post(endpoint,{'foreign_authors':[self.author.pk], 'translators':[self.other.pk],'_edit_version':token})
        self.assertEqual(response.status_code,302)
        self.assertEqual(list(self.text.translation.translators.all()),[self.other])
        self.assertEqual(list(self.text.translation.foreign_authors.all()),[self.author])
        response=self.client.post(endpoint,{'translators':[self.translator.pk],'_edit_version':token})
        self.assertEqual(response.status_code,409)
        response=self.client.post(endpoint,{'translators':[999999],'_edit_version':self.token()})
        self.assertEqual(response.status_code,400)
        self.assertEqual(list(self.text.translation.translators.all()),[self.other])

    def test_member_sees_names_but_no_private_contacts_and_cannot_edit_translators(self):
        self.client.force_login(self.member)
        response=self.client.get(reverse('core:translation_list'))
        self.assertContains(response,'Anna Żak')
        self.assertNotContains(response,'translator-private@example.test')
        response=self.client.get(reverse('core:translation_detail',args=[self.text.pk]))
        self.assertContains(response,'Anna Żak');self.assertNotContains(response,'translator-private@example.test')
        response=self.client.post(reverse('core:set_translators',args=[self.text.pk]),{'translators':[self.other.pk]})
        self.assertEqual(response.status_code,403)
        self.client.logout()
        self.assertEqual(self.client.get(reverse('core:translation_list')).status_code,302)

    def test_regular_details_redirect_and_translation_route_rejects_normal_text(self):
        response=self.client.get(reverse('core:assigned_text_detail',args=[self.text.pk]))
        self.assertRedirects(response,reverse('core:translation_detail',args=[self.text.pk]))
        self.assertEqual(self.client.get(reverse('core:translation_detail',args=[self.regular.pk])).status_code,404)
        response=self.client.get(reverse('core:anthology_detail',args=[self.book.pk]))
        self.assertEqual(response.status_code,302)
        self.assertIn('/tlumaczenia/',response.url)

    def test_admin_flag_and_translator_inline_only_for_translated_text(self):
        response=self.client.get(reverse('admin:texts_anthology_change',args=[self.book.pk]))
        self.assertContains(response,'name="is_translated"')
        response=self.client.get(reverse('admin:texts_text_change',args=[self.text.pk]))
        self.assertEqual(response.status_code,200);self.assertContains(response,'translation-0-translators')
        response=self.client.get(reverse('admin:texts_text_change',args=[self.regular.pk]))
        self.assertEqual(response.status_code,200);self.assertNotContains(response,'translation-0-translators')

    def test_flag_off_restores_normal_visibility_without_losing_translators(self):
        self.book.is_translated=False;self.book.save()
        response=self.client.get(reverse('core:translation_list'))
        self.assertNotContains(response,self.book.title)
        response=self.client.get(reverse('core:text_list'))
        self.assertContains(response,self.text.title)
        self.assertEqual(list(TextTranslation.objects.get(text=self.text).translators.all()),[self.translator])

    def test_translator_delete_invalidates_text_edit_version(self):
        old=version_of(self.text);self.translator.delete()
        self.assertGreater(version_of(self.text),old)
