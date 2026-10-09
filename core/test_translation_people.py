from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from lxml import html

from authors.models import Author
from texts.models import Anthology, Text, ForeignAuthor, Translator
from texts.admin import TextAdminForm
from core.edit_versions import version_of


class TranslationPeopleTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_superuser('profile-admin', 'admin@example.test', 'test')
        self.client.force_login(self.user)
        self.book = Anthology.objects.create(title='Przekłady', is_translated=True)
        self.author = ForeignAuthor.objects.create(first_name='Ursula', last_name='Zagraniczna')
        self.translator = Translator.objects.create(first_name='Jan', last_name='Tłumaczący', language='angielski')
        self.text = Text.objects.create(title='Tekst zagraniczny', anthology=self.book, length=250)
        self.text.translation.foreign_authors.add(self.author)
        self.text.translation.translators.add(self.translator)

    def test_admin_creates_profiles_without_email_and_keeps_domestic_model_separate(self):
        for model in (ForeignAuthor, Translator):
            url = reverse(f'admin:texts_{model._meta.model_name}_add')
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(response.context['adminform'].form.fields['email'].required)
            data = {'first_name':'Nowa', 'last_name':model.__name__, 'email':'', 'language':'francuski'}
            response = self.client.post(url, data)
            self.assertEqual(response.status_code,302)
            self.assertTrue(model.objects.filter(first_name='Nowa').exists())
        self.assertFalse(Author.objects.exists())
        form = TextAdminForm(data={'title':'Nowe tłumaczenie','anthology':self.book.pk,'length':15,
                                  'current_workflow_cycle':1})
        self.assertTrue(form.is_valid(), form.errors.as_json())
        text = form.save()
        self.assertFalse(text.authors.exists())
        self.assertIsNotNone(text.translation)

    def test_profile_links_filters_and_visibility(self):
        url=reverse('core:translation_list')
        for params in ({'q':'angielski'}, {'author':self.author.pk}, {'translator':self.translator.pk}):
            response=self.client.get(url,params)
            self.assertEqual(response.status_code,200)
            self.assertContains(response,self.text.title)
            self.assertContains(response,reverse('core:translation_person_detail',args=['author',self.author.pk]))
            self.assertContains(response,reverse('core:translation_person_detail',args=['translator',self.translator.pk]))
        response=self.client.get(reverse('core:translation_person_detail',args=['translator',self.translator.pk]))
        self.assertContains(response,'angielski')
        self.assertContains(response,self.text.title)
        response=self.client.get(reverse('core:author_list'))
        self.assertNotContains(response,'Zagraniczna')
        self.assertNotContains(response,'Tłumaczący')
        response=self.client.get(url)
        nav=html.fromstring(response.content).xpath('//a[contains(@class,"sidebar-link")]/text()')
        nav=[v.strip() for v in nav if v.strip()]
        self.assertEqual(nav.index('Tłumaczenia'),nav.index('Ekstrakty')+1)

    def test_filter_id_spaces_do_not_collide_and_sort_works(self):
        other=Translator.objects.create(first_name='Adam',last_name='Abacki')
        story=Text.objects.create(title='Drugi',anthology=self.book,length=100)
        story.translation.translators.add(other)
        for sort, expected in [('translators',[story.pk,self.text.pk]),('-translators',[self.text.pk,story.pk])]:
            response=self.client.get(reverse('core:translation_list'),{'sort':sort})
            self.assertEqual([t['pk'] for t in response.context['texts']],expected)
        response=self.client.get(reverse('core:translation_list'),{'translator':other.pk,'author':self.author.pk})
        self.assertEqual(response.context['page_obj'].paginator.count,0)

    def test_profile_change_invalidates_text_form_and_flagging_copies_existing_author(self):
        old=version_of(self.text)
        self.translator.language='niemiecki'
        self.translator.save()
        self.assertGreater(version_of(self.text), old)
        ordinary = Anthology.objects.create(title="Zmieniana")
        story = Text.objects.create(title="Już istnieje", anthology=ordinary, length=10)
        author = Author.objects.create(first_name="Dawny", last_name="Autor", email=None)
        story.authors.add(author)
        ordinary.is_translated = True
        ordinary.save()
        self.assertFalse(story.authors.exists())
        profile = story.translation.foreign_authors.get()
        self.assertEqual(profile.legacy_author_id, author.pk)
        self.assertEqual(profile.email, "")
        self.assertTrue(Author.objects.filter(pk=author.pk).exists())
        ordinary.save()
        self.assertEqual(story.translation.foreign_authors.count(), 1)
