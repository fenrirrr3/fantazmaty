"""Autorzy i pseudonimy: strony zespołu pokazują pseudonim, admin zachowuje dane prawne, wyszukiwanie i formularze."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from authors.models import Author
from core.intake_forms import ExtractForm, SingleReviewForm
from core.selectors.texts import _prepared_texts, _text_data
from fantazmaty.testing import create_member, create_superuser
from texts.admin import ReviewAdminForm
from texts.models import Anthology, ForeignAuthor, NovelProfile, Review, Text, TextTranslation
from workflow.models import WorkflowStage


class AuthorPseudonymTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('pseudo-admin')
        cls.member = create_member('pseudo-member', 'Redaktor')
        cls.author = Author.objects.create(first_name='Jawneimię', last_name='Jawnenazwisko',
                                           pseudonym='Mariusz Nowak', email='author@example.test')
        cls.book = Anthology.objects.create(title='Antologia testowa', has_illustrations=True)
        cls.story = Text.objects.create(title='Opowiadanie testowe', length=100, anthology=cls.book)
        cls.story.authors.add(cls.author)
        cls.review = Review.objects.create(
            title='Zgłoszenie testowe', anthology=cls.book, author=cls.author,
            author_first_name=cls.author.first_name, author_last_name=cls.author.last_name,
            author_pseudonym='Dawny pseudonim', genre='fantasy', length=100, email=cls.author.email)

    def setUp(self):
        self.client.force_login(self.admin)

    def test_pages_use_current_pseudonym_and_admin_retains_legal_name(self):
        routes = [('core:text_list', []), ('core:tag_list', []), ('core:audiobooks', []),
                  ('core:assigned_text_detail', [self.story.pk]), ('core:review_list', []),
                  ('core:assigned_review_detail', [self.review.pk]), ('core:author_list', []),
                  ('core:author_detail', [self.author.pk])]
        for name, args in routes:
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=args), {'hide_ready': '0'})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, 'Mariusz Nowak')
                self.assertNotContains(response, 'Jawneimię')
                self.assertNotContains(response, 'Jawnenazwisko')
        for model, pk in (('authors_author', self.author.pk), ('texts_review', self.review.pk)):
            response = self.client.get(reverse(f'admin:{model}_change', args=[pk]))
            self.assertContains(response, 'Jawneimię')
            self.assertContains(response, 'Jawnenazwisko')

    def test_search_suggestions_and_member_projection(self):
        response = self.client.get(reverse('core:author_suggestions'), {'q': 'Mariusz'})
        self.assertEqual(response.json()['results'][0]['label'], 'Mariusz Nowak')
        self.assertNotContains(response, 'Jawne')
        response = self.client.get(reverse('core:global_search'), {'q': 'Mariusz'})
        self.assertContains(response, 'Mariusz Nowak')
        self.assertNotContains(response, 'Jawneimię')
        texts = list(_prepared_texts(Text.objects.filter(pk=self.story.pk), False))
        with self.assertNumQueries(0):
            self.assertEqual(_text_data(texts[0], False)['authors_display'], 'Mariusz Nowak')
        self.client.force_login(self.member)
        for name in ('core:text_list', 'core:tag_list', 'core:audiobooks'):
            response = self.client.get(reverse(name))
            self.assertContains(response, 'Mariusz Nowak')
            self.assertNotContains(response, 'Jawneimię')

    def test_public_author_search_and_sort_use_pseudonym(self):
        """Chroni przed wyszukiwaniem i sortowaniem tekstów po nazwisku prawnym zamiast pseudonimu."""
        other = Author.objects.create(first_name='Zofia', last_name='Zetowska', pseudonym='Alfa Pseudonim',
                                      email='other@example.test')
        story = Text.objects.create(title='Drugie', anthology=self.book, length=100)
        story.authors.add(other)
        self.client.force_login(self.member)
        url = reverse('core:tag_list')
        self.assertEqual(list(self.client.get(url, {'q': 'Jawneimię Jawnenazwisko'}).context['page_obj']), [])
        self.assertEqual([x.pk for x in self.client.get(url, {'q': 'Mariusz'}).context['page_obj']],
                         [self.story.pk])
        for sort, expected in [('author', [story.pk, self.story.pk]), ('-author', [self.story.pk, story.pk])]:
            self.assertEqual([x.pk for x in self.client.get(url, {'sort': sort}).context['page_obj']], expected)
        from core.views.search import _search_authors, _search_texts

        self.assertEqual(_search_texts('Jawnenazwisko', include_authors=True), [])
        self.assertEqual(_search_authors('Jawnenazwisko', self.admin), [])

    def test_review_pseudonym_is_searchable(self):
        Review.objects.create(title='Unikalna historia pseudonimu', anthology=self.book, author_first_name='Jan',
                              author_last_name='Autor', email='autor@example.com', length=100, genre='fantasy',
                              author_pseudonym='Nietypowypseudonim', status='new')
        for url in ['core:review_list', 'core:global_search']:
            response = self.client.get(reverse(url), {'q': 'Nietypowypseudonim', 'query': 'Nietypowypseudonim',
                                                      'status': 'new'})
            self.assertContains(response, 'Unikalna historia pseudonimu')

    def test_empty_pseudonym_falls_back_and_review_coauthors_use_pseudonyms(self):
        self.assertEqual(str(self.author), 'Jawneimię Jawnenazwisko')
        other = Author.objects.create(first_name='Anna', last_name='Kowalska', email='second@example.test')
        self.review.coauthors.add(other)
        self.assertEqual(self.review.author_display_name, 'Mariusz Nowak, Anna Kowalska')
        self.author.pseudonym = ''
        self.author.save(update_fields=['pseudonym'])
        self.review.refresh_from_db()
        self.assertEqual(self.review.author_display_name, 'Jawneimię Jawnenazwisko, Anna Kowalska')
        self.review.author = None
        self.review.pk = None
        self.assertEqual(self.review.author_display_name, 'Dawny pseudonim')

    def test_submission_forms_store_pseudonym(self):
        for form_class in [SingleReviewForm, ReviewAdminForm]:
            with self.subTest(form=form_class.__name__):
                form = form_class(data={
                    'author_first_name': 'Adam', 'author_last_name': 'Nowy', 'author_pseudonym': 'Pióro',
                    'title': 'Nowy', 'email': 'adam@example.com', 'anthology': self.book.pk, 'length': 100,
                    'genre': 'fantasy', 'status': 'new'})
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.save(commit=False).author_pseudonym, 'Pióro')

    def test_extract_display_preserves_stored_name_and_submission_identity(self):
        from texts.models import Extract

        extract = Extract.objects.create(author=self.author, full_name=str(self.author), email=self.author.email,
                                         title='Ekstrakt testowy', recruitment='Nabór testowy',
                                         submission_dates='2026-10-05')
        for url in (reverse('core:extract_list'), reverse('core:extract_edit', args=[extract.pk])):
            response = self.client.get(url)
            self.assertContains(response, 'Mariusz Nowak')
            self.assertNotContains(response, str(self.author))
        form = ExtractForm({'author': self.author.pk, 'full_name': 'Mariusz Nowak', 'email': self.author.email,
                            'title': extract.title, 'recruitment': extract.recruitment,
                            'submission_dates': '2026-10-05'}, instance=extract)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        extract.refresh_from_db()
        self.assertEqual(extract.full_name, str(self.author))
        form = SingleReviewForm({'anthology': self.book.pk, 'author': self.author.pk,
                                 'author_first_name': 'Mariusz Nowak', 'author_last_name': '',
                                 'title': 'Nowe zgłoszenie', 'genre': 'fantasy', 'length': 100,
                                 'email': self.author.email})
        self.assertTrue(form.is_valid(), form.errors)
        review = form.save()
        self.assertEqual((review.author_first_name, review.author_last_name),
                         (self.author.first_name, self.author.last_name))

    def test_review_authorship_remains_hidden_for_regular_member(self):
        reviewer = create_member('pseudo-reviewer', 'Recenzent')
        self.client.force_login(reviewer)
        response = self.client.get(reverse('core:review_list'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Mariusz Nowak')
        self.assertNotContains(response, str(self.author))

    def test_foreign_author_profile_and_translations_use_pseudonym(self):
        book = Anthology.objects.create(title='Przekłady', is_translated=True)
        text = Text.objects.create(title='Przekład', anthology=book, length=100)
        person = ForeignAuthor.objects.create(first_name='ForeignLegal', last_name='Surname', pseudonym='ForeignPen')
        TextTranslation.objects.get(text=text).foreign_authors.add(person)
        for name, args in [('core:translation_list', []), ('core:translation_detail', [text.pk]),
                           ('core:translation_person_detail', ['author', person.pk])]:
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=args))
                self.assertContains(response, 'ForeignPen')
                self.assertNotContains(response, 'ForeignLegal')

    def test_tags_hide_current_withdrawals_and_their_filter_options(self):
        book = Anthology.objects.create(title='Wyłącznie wycofane')
        author = Author.objects.create(first_name='Autor', last_name='Wycofany', email='withdrawn@example.test')
        withdrawn = Text.objects.create(title='Wycofany tekst', anthology=book, length=100, genre='Tylko wycofane')
        withdrawn.authors.add(author)
        WorkflowStage.objects.create(text=withdrawn, stage_type='withdrawn', is_released=True, is_completed=False)
        WorkflowStage.objects.create(text=self.story, stage_type='withdrawn', is_released=True, is_completed=False,
                                     workflow_cycle=1, is_current=False)
        Text.objects.filter(pk=self.story.pk).update(current_workflow_cycle=2)
        WorkflowStage.objects.create(text=self.story, stage_type='ready', is_released=True, is_completed=False,
                                     workflow_cycle=2)
        response = self.client.get(reverse('core:tag_list'))
        self.assertContains(response, self.story.title)
        for hidden in (withdrawn.title, book.title, 'Autor Wycofany', 'Tylko wycofane'):
            self.assertNotContains(response, hidden)
        for params in ({'q': withdrawn.title}, {'author': author.pk}, {'anthology': book.pk}):
            response = self.client.get(reverse('core:tag_list'), params)
            self.assertEqual(list(response.context['page_obj']), [])

    def test_inactive_novel_profile_preserves_metadata_and_shows_recovery_link(self):
        """Chroni metadane powieści (autorzy, tagi) po zdjęciu i przywróceniu oznaczenia „powieść”."""
        from texts.catalog_admin import NovelAdminForm
        from texts.novels import edit_token, locked_book

        novel = Anthology.objects.create(title='Zmiana typu', is_novel=True)
        profile = NovelProfile.objects.get(anthology=novel)
        profile.authors.add(self.author)
        profile.tags = 'zachowany tag'
        profile.save()
        novel.is_novel = False
        novel.save()
        with self.assertRaises(ValidationError), locked_book(novel.pk, self.admin, 'old-token'):
            pass
        response = self.client.get(reverse('admin:texts_novelprofile_change', args=[profile.pk]))
        self.assertContains(response, 'Zachowany profil')
        self.assertNotContains(response, reverse('core:novel_detail', args=[novel.pk]))
        form = NovelAdminForm({'title': novel.title, 'authors': [self.author.pk],
                               'novel_token': edit_token(novel, self.admin), 'tags': profile.tags},
                              instance=profile)
        form.novel_user = self.admin
        self.assertFalse(form.is_valid())
        self.assertIn('Publikacja nie jest oznaczona jako powieść', str(form.non_field_errors()))
        novel.is_novel = True
        novel.save()
        profile.refresh_from_db()
        self.assertEqual(profile.tags, 'zachowany tag')
        self.assertEqual(list(profile.authors.all()), [self.author])
