from tempfile import TemporaryDirectory
from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from lxml import html
from authors.models import Author
from texts.models import Anthology, Review, Text
from workflow.tests import create_member


class TagsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin=get_user_model().objects.create_superuser('tags-admin','tags-admin@example.test','test-only')
        cls.member=create_member('tags-member','Recenzent')
        cls.artist=create_member('tags-artist','Ilustrator')
        cls.coordinator=create_member('tags-coordinator','Koordynator redakcji')
        cls.outsider=get_user_model().objects.create_user('tags-outsider',email='tags-outsider@example.test')
        cls.book=Anthology.objects.create(title='Antologia A')
        cls.book2=Anthology.objects.create(title='Antologia Z')
        cls.author=Author.objects.create(first_name='Anna',last_name='Żak',email='private-author@example.test')
        cls.coauthor=Author.objects.create(first_name='Jan',last_name='Adamczyk',email='private-coauthor@example.test')
        cls.text=Text.objects.create(title='Łąka',anthology=cls.book,length=100,tags='magia, podróż')
        cls.text.authors.add(cls.author,cls.coauthor)
        cls.other=Text.objects.create(title='Las',anthology=cls.book2,length=100)
        cls.other.authors.add(cls.author)
        cls.orphan=Text.objects.create(title='Zorza',length=100)
        cls.review=Review.objects.create(title='Zgłoszenie',anthology=cls.book,length=100,genre='Fantasy',
            email='secret-source@example.test',author_first_name='Ukryte',author_last_name='Zgłoszenie',copied_text=cls.text)

    def setUp(self):
        tmp=TemporaryDirectory();self.addCleanup(tmp.cleanup)
        override=self.settings(ACTIVITY_SPOOL_DIR=tmp.name);override.enable();self.addCleanup(override.disable)
        self.client.force_login(self.member)
        self.list=reverse('core:tag_list')
        self.detail=reverse('core:assigned_text_detail',args=[self.text.pk])
        self.save=reverse('core:update_text_tags',args=[self.text.pk])

    def token(self):
        response=self.client.get(self.detail)
        self.assertEqual(response.status_code,200)
        return html.fromstring(response.content).xpath('//form[contains(@class,"text-tags-form")]/input[@name="_edit_version"]/@value')[0]

    def post(self,value):
        return self.client.post(self.save,{'tags':value,'_edit_version':self.token()})

    def ids(self,**params):
        response=self.client.get(self.list,params)
        self.assertEqual(response.status_code,200)
        return [row.pk for row in response.context['page_obj']]

    def test_list_metadata_is_live_and_private_contacts_are_not_exposed(self):
        response=self.client.get(self.list)
        for value in ('Antologia A','Łąka','Anna Żak','Jan Adamczyk','magia, podróż','Fantasy'):
            self.assertContains(response,value)
        for value in ('private-author@example.test','private-coauthor@example.test','secret-source@example.test','Ukryte Zgłoszenie'):
            self.assertNotContains(response,value)
        self.review.genre='Horror';self.review.save(update_fields=['genre'])
        self.assertContains(self.client.get(self.list),'Horror')
        self.assertNotContains(self.client.get(self.list),'Fantasy')

    def test_missing_genre_author_anthology_and_tags_render(self):
        response=self.client.get(self.list,{'anthology':'none'})
        for value in ('Zorza','Brak danych','Brak antologii','Brak tagów'):
            self.assertContains(response,value)

    def test_all_active_team_roles_can_edit_without_assignment(self):
        for user in (self.member,self.artist,self.coordinator,self.admin):
            self.client.force_login(user)
            self.assertEqual(self.post('tag '+str(user.pk)).status_code,302)
            self.text.refresh_from_db();self.assertEqual(self.text.tags,'tag '+str(user.pk))

    def test_guest_outsider_and_inactive_profile_cannot_read_or_save(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.list).status_code,302)
        self.assertEqual(self.client.post(self.save,{'tags':'bad'}).status_code,302)
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.get(self.list).status_code,403)
        self.assertEqual(self.client.post(self.save,{'tags':'bad'}).status_code,403)
        profile=self.member.person_profile;profile.is_active=False;profile.save()
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.list).status_code,403)
        self.assertEqual(self.client.post(self.save,{'tags':'bad'}).status_code,403)

    def test_tags_normalize_duplicates_whitespace_and_newlines(self):
        self.assertEqual(self.post('  Magia, magia\r\npodróż,  daleki   kosmos, PODRÓŻ,, ').status_code,302)
        self.text.refresh_from_db();self.assertEqual(self.text.tags,'Magia, podróż, daleki kosmos')
        self.assertContains(self.client.get(self.list),'Magia, podróż, daleki kosmos')

    def test_empty_tags_clear_the_field(self):
        self.assertEqual(self.post(' ,\n ').status_code,302)
        self.text.refresh_from_db();self.assertEqual(self.text.tags,'')

    def test_oversized_input_does_not_save_and_preserves_input(self):
        response=self.post('a'*5001)
        self.assertEqual(response.status_code,400)
        self.assertContains(response,'a'*5001,status_code=400)
        self.text.refresh_from_db();self.assertEqual(self.text.tags,'magia, podróż')

    def test_stale_or_missing_version_is_rejected_and_input_recoverable(self):
        old=self.token()
        self.post('Nowszy tag')
        response=self.client.post(self.save,{'tags':'Stary formularz','_edit_version':old})
        self.assertEqual(response.status_code,409)
        self.assertContains(response,'Stary formularz',status_code=409)
        self.text.refresh_from_db();self.assertEqual(self.text.tags,'Nowszy tag')
        self.assertEqual(self.client.post(self.save,{'tags':'Brak tokena'}).status_code,409)

    def test_csrf_get_and_nonexistent_text_are_handled(self):
        csrf=Client(enforce_csrf_checks=True);csrf.force_login(self.member)
        self.assertEqual(csrf.post(self.save,{'tags':'No'}).status_code,403)
        self.assertEqual(self.client.get(self.save).status_code,405)
        self.assertEqual(self.client.post(reverse('core:update_text_tags',args=[999999]),{'tags':'No'}).status_code,404)

    def test_tag_html_is_escaped_in_detail_and_list(self):
        self.post('<script>alert(1)</script>')
        for url in (self.list,self.detail):
            response=self.client.get(url)
            self.assertContains(response,'&lt;script&gt;alert(1)&lt;/script&gt;')
            self.assertNotContains(response,'<script>alert(1)</script>')

    def test_search_each_displayed_field_and_full_author_name(self):
        for query in ('Łąka','magia','podróż','Fantasy','Jan Adamczyk','Antologia A'):
            self.assertEqual(self.ids(q=query),[self.text.pk])

    def test_filters_combine_and_include_coauthors(self):
        self.assertEqual(self.ids(anthology=str(self.book.pk),author=str(self.coauthor.pk),genre='Fantasy',tag='mag',filled='yes'),[self.text.pk])
        self.assertEqual(self.ids(anthology=str(self.book.pk),genre='Horror'),[])
        self.assertEqual(self.ids(anthology='none'),[self.orphan.pk])
        self.assertEqual(set(self.ids(genre='__missing__',filled='no')),{self.other.pk,self.orphan.pk})
        for invalid in ('bad','9'*100,'-1','0'):
            self.assertEqual(self.ids(anthology=invalid),[])
            self.assertEqual(self.ids(author=invalid),[])

    def test_sort_all_columns_both_directions_without_duplicate_rows(self):
        self.other.tags='zebra';self.other.save(update_fields=['tags'])
        for key in ('title','anthology','author','tags','genre'):
            for prefix in ('','-'):
                ids=self.ids(sort=prefix+key)
                self.assertEqual(len(ids),3)
                self.assertEqual(set(ids),{self.text.pk,self.other.pk,self.orphan.pk})
        self.assertEqual(self.ids(sort='title'),[self.other.pk,self.text.pk,self.orphan.pk])
        self.assertEqual(self.ids(sort='author'),[self.text.pk,self.other.pk,self.orphan.pk])
        self.assertEqual(self.ids(sort='-tags')[0],self.other.pk)
        self.assertEqual(self.ids(sort='password'),self.ids(sort='title'))

    def test_sorting_is_before_pagination_and_filters_are_preserved(self):
        for n in range(28):Text.objects.create(title=f'Test {n:02}',anthology=self.book,length=1,tags='szukany')
        response=self.client.get(self.list,{'tag':'szukany','sort':'-title','page_size':25,'page':2})
        self.assertEqual(response.context['page_obj'].paginator.count,28)
        self.assertEqual([row.title for row in response.context['page_obj']],['Test 02','Test 01','Test 00'])
        self.assertIn('tag=szukany',response.context['page_obj'].previous_url)
        self.assertIn('sort=-title',response.context['page_obj'].previous_url)

    def test_only_tag_field_changes_and_workflow_is_untouched(self):
        before=(self.text.title,self.text.anthology_id,self.text.content_warnings,self.text.coordinator_note)
        stages=list(self.text.workflow_stages.values())
        response=self.client.post(self.save,{'tags':'nowy','title':'Nadpisany','coordinator_note':'Nadpisana','_edit_version':self.token()})
        self.assertEqual(response.status_code,302)
        self.text.refresh_from_db()
        self.assertEqual((self.text.title,self.text.anthology_id,self.text.content_warnings,self.text.coordinator_note),before)
        self.assertEqual(list(self.text.workflow_stages.values()),stages)

    def test_notes_and_tags_share_two_column_container_and_navigation_exists(self):
        response=self.client.get(self.detail)
        doc=html.fromstring(response.content)
        columns=doc.xpath('//div[@class="text-notes-tags"]/section/@aria-labelledby')
        self.assertEqual(columns,['text-notes-heading','text-tags-heading'])
        self.assertContains(response,'href="'+self.list+'"')
        self.assertContains(response,'core/tags.')

    def test_existing_rows_start_with_empty_tags_without_importing_genre(self):
        self.assertEqual(self.other.tags,'')
        self.assertEqual(self.orphan.tags,'')
        self.assertEqual(Text.objects.get(pk=self.text.pk).tags,'magia, podróż')

    def test_account_preview_cannot_write_tags(self):
        self.client.force_login(self.admin)
        from core.user_preview import SESSION_KEY
        session=self.client.session;session[SESSION_KEY]={'actor':self.admin.pk,'target':self.member.pk};session.save()
        self.assertEqual(self.client.post(self.save,{'tags':'Preview'}).status_code,403)
