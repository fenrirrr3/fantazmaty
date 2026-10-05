from tempfile import TemporaryDirectory
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from authors.models import Author
from texts.models import Anthology, Text, Review, ForeignAuthor, TextTranslation
from workflow.models import WorkflowStage
from workflow.tests import create_member
from core.selectors.texts import _prepared_texts, _text_data


class AuthorPseudonymTests(TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=tmp.name)
        override.enable()
        self.addCleanup(override.disable)
        self.admin = get_user_model().objects.create_superuser('pseudo-admin', 'admin@example.test', 'test')
        self.member = create_member('pseudo-member', 'Redaktor')
        self.author = Author.objects.create(first_name='Jawneimię', last_name='Jawnenazwisko', pseudonym='Mariusz Nowak', email='author@example.test')
        self.book = Anthology.objects.create(title='Antologia testowa', has_illustrations=True)
        self.story = Text.objects.create(title='Opowiadanie testowe', length=100, anthology=self.book)
        self.story.authors.add(self.author)
        self.review = Review.objects.create(title='Zgłoszenie testowe', anthology=self.book, author=self.author,
            author_first_name=self.author.first_name, author_last_name=self.author.last_name,
            author_pseudonym='Dawny pseudonim', genre='fantasy', length=100, email=self.author.email)
        self.client.force_login(self.admin)

    def test_pages_use_current_pseudonym_and_admin_retains_legal_name(self):
        routes = [('core:text_list', []), ('core:tag_list', []), ('core:audiobooks', []),
                  ('core:assigned_text_detail', [self.story.pk]), ('core:review_list', []),
                  ('core:assigned_review_detail', [self.review.pk]), ('core:author_list', []),
                  ('core:author_detail', [self.author.pk])]
        for name, args in routes:
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=args), {'hide_ready':'0'})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Mariusz Nowak')
                self.assertNotContains(response, 'Jawneimię')
                self.assertNotContains(response, 'Jawnenazwisko')
        for model in ('authors_author', 'texts_review'):
            pk = self.author.pk if model == 'authors_author' else self.review.pk
            response = self.client.get(reverse(f'admin:{model}_change', args=[pk]))
            self.assertContains(response, 'Jawneimię')
            self.assertContains(response, 'Jawnenazwisko')

    def test_search_suggestions_and_member_projection(self):
        response = self.client.get(reverse('core:author_suggestions'), {'q':'Mariusz'})
        self.assertEqual(response.json()['results'][0]['label'], 'Mariusz Nowak')
        self.assertNotContains(response, 'Jawne')
        response = self.client.get(reverse('core:global_search'), {'q':'Mariusz'})
        self.assertContains(response, 'Mariusz Nowak')
        self.assertNotContains(response, 'Jawneimię')
        texts = list(_prepared_texts(Text.objects.filter(pk=self.story.pk), False))
        with self.assertNumQueries(0):
            self.assertEqual(_text_data(texts[0], False)['authors_display'], 'Mariusz Nowak')
        self.client.force_login(self.member)
        for name in ('core:text_list','core:tag_list','core:audiobooks'):
            response=self.client.get(reverse(name))
            self.assertContains(response, 'Mariusz Nowak')
            self.assertNotContains(response, 'Jawneimię')

    def test_empty_pseudonym_falls_back_and_review_coauthors_use_pseudonyms(self):
        self.assertEqual(str(self.author), 'Jawneimię Jawnenazwisko')
        other=Author.objects.create(first_name='Anna',last_name='Kowalska',email='second@example.test')
        self.review.coauthors.add(other)
        self.assertEqual(self.review.author_display_name, 'Mariusz Nowak, Anna Kowalska')
        self.author.pseudonym=''
        self.author.save(update_fields=['pseudonym'])
        self.review.refresh_from_db()
        self.assertEqual(self.review.author_display_name,'Jawneimię Jawnenazwisko, Anna Kowalska')
        self.review.author=None
        self.review.pk=None
        self.assertEqual(self.review.author_display_name,'Dawny pseudonim')

    def test_extract_display_preserves_stored_name_and_submission_identity(self):
        from texts.models import Extract
        from core.intake_forms import ExtractForm, SingleReviewForm
        extract = Extract.objects.create(author=self.author, full_name=str(self.author), email=self.author.email,
            title='Ekstrakt testowy', recruitment='Nabór testowy', submission_dates='2026-10-05')
        for url in (reverse('core:extract_list'), reverse('core:extract_edit', args=[extract.pk])):
            response = self.client.get(url)
            self.assertContains(response, 'Mariusz Nowak')
            self.assertNotContains(response, str(self.author))
        form = ExtractForm({'author': self.author.pk, 'full_name':'Mariusz Nowak', 'email':self.author.email,
            'title':extract.title, 'recruitment':extract.recruitment, 'submission_dates':'2026-10-05'}, instance=extract)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        extract.refresh_from_db()
        self.assertEqual(extract.full_name, str(self.author))
        form = SingleReviewForm({'anthology':self.book.pk, 'author':self.author.pk,
            'author_first_name':'Mariusz Nowak', 'author_last_name':'', 'title':'Nowe zgłoszenie',
            'genre':'fantasy', 'length':100, 'email':self.author.email})
        self.assertTrue(form.is_valid(), form.errors)
        review = form.save()
        self.assertEqual((review.author_first_name, review.author_last_name), (self.author.first_name, self.author.last_name))

    def test_review_authorship_remains_hidden_for_regular_member(self):
        self.client.force_login(self.member)
        response = self.client.get(reverse('core:review_list'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Mariusz Nowak')
        self.assertNotContains(response, str(self.author))

    def test_foreign_author_profile_and_translations_use_pseudonym(self):
        book=Anthology.objects.create(title='Przekłady',is_translated=True)
        text=Text.objects.create(title='Przekład',anthology=book,length=100)
        person=ForeignAuthor.objects.create(first_name='ForeignLegal',last_name='Surname',pseudonym='ForeignPen')
        record=TextTranslation.objects.get(text=text)
        record.foreign_authors.add(person)
        for name,args in [('core:translation_list',[]),('core:translation_detail',[text.pk]),('core:translation_person_detail',['author',person.pk])]:
            with self.subTest(name=name):
                response=self.client.get(reverse(name,args=args))
                self.assertContains(response,'ForeignPen')
                self.assertNotContains(response,'ForeignLegal')

    def test_tags_hide_current_withdrawals_and_their_filter_options(self):
        book=Anthology.objects.create(title='Wyłącznie wycofane')
        author=Author.objects.create(first_name='Autor',last_name='Wycofany',email='withdrawn@example.test')
        withdrawn=Text.objects.create(title='Wycofany tekst',anthology=book,length=100,genre='Tylko wycofane')
        withdrawn.authors.add(author)
        WorkflowStage.objects.create(text=withdrawn,stage_type='withdrawn',is_released=True,is_completed=False)
        WorkflowStage.objects.create(text=self.story,stage_type='withdrawn',is_released=True,is_completed=False,workflow_cycle=1,is_current=False)
        Text.objects.filter(pk=self.story.pk).update(current_workflow_cycle=2)
        WorkflowStage.objects.create(text=self.story,stage_type='ready',is_released=True,is_completed=False,workflow_cycle=2)
        response=self.client.get(reverse('core:tag_list'))
        self.assertContains(response,self.story.title)
        for hidden in (withdrawn.title, book.title, 'Autor Wycofany', 'Tylko wycofane'):
            self.assertNotContains(response,hidden)
        for params in ({'q':withdrawn.title},{'author':author.pk},{'anthology':book.pk}):
            response=self.client.get(reverse('core:tag_list'),params)
            self.assertEqual(list(response.context['page_obj']),[])
