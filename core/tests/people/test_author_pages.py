"""Strony autorów: filtry listy (umowa, kontakt, antologie, wyszukiwanie), profil autora i notatki superusera."""
from django.test import TestCase
from django.urls import reverse

from authors.models import Author, AuthorNote
from fantazmaty.testing import create_member, create_superuser
from texts.models import Anthology, Text


class AuthorPagesTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('authors-admin')
        cls.coordinator = create_member('authors-coord', 'Koordynator redakcji')
        cls.member = create_member('authors-member', 'Redaktor')
        cls.first_book = Anthology.objects.create(title='Pierwsza')
        cls.second_book = Anthology.objects.create(title='Druga')
        cls.signed = Author.objects.create(first_name='Anna', last_name='Umowna', email='signed@example.test',
                                           has_contract=True)
        cls.contact = Author.objects.create(first_name='Bartosz', last_name='Kontaktowy', pseudonym='Bart',
                                            email='contact@example.test', contact=True)
        cls.unpublished = Author.objects.create(first_name='Celina', last_name='Bez', email='none@example.test')
        for author, book in ((cls.signed, cls.first_book), (cls.contact, cls.second_book)):
            Text.objects.create(title=f'Tekst {author.last_name}', anthology=book, length=10).authors.add(author)

    def listed(self, **params):
        response = self.client.get(reverse('core:author_list'), params)
        self.assertEqual(response.status_code, 200)
        return [row['pk'] for row in response.context['authors']]

    def test_list_filters_combine_and_ignore_invalid_values(self):
        self.client.force_login(self.coordinator)
        both = [self.contact.pk, self.signed.pk]
        self.assertEqual(self.listed(), both)
        self.assertEqual(self.listed(accepted='0'), [self.unpublished.pk, self.contact.pk, self.signed.pk])
        self.assertEqual(self.listed(contract='yes'), [self.signed.pk])
        self.assertEqual(self.listed(contract='no', contact='yes'), [self.contact.pk])
        self.assertEqual(self.listed(contract='maybe', contact=' '), both)
        self.assertEqual(self.listed(anthology=[str(self.first_book.pk), 'abc', '0', '9' * 20]), [self.signed.pk])
        self.assertEqual(self.listed(q='anna umowna'), [self.signed.pk])
        self.assertEqual(self.listed(q='contact@'), [self.contact.pk])
        rows = self.client.get(reverse('core:author_list')).context['authors']
        self.assertEqual({row['display_name']: [a['title'] for a in row['participating_anthologies']]
                          for row in rows}, {'Anna Umowna': ['Pierwsza'], 'Bart': ['Druga']})
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse('core:author_list')).status_code, 403)

    def test_profile_shows_texts_and_note_history_but_only_superuser_adds_notes(self):
        AuthorNote.objects.create(author=self.signed, created_by=self.admin, content='Zachowana notatka')
        AuthorNote.objects.create(author=self.signed, content='Notatka bez autora')
        self.client.force_login(self.coordinator)
        page = self.client.get(reverse('core:author_detail', args=[self.signed.pk]))
        self.assertEqual([t['title'] for t in page.context['texts']], ['Tekst Umowna'])
        self.assertEqual([(n['content'], n['created_by'] is None) for n in page.context['author']['note_history']],
                         [('Notatka bez autora', True), ('Zachowana notatka', False)])
        self.assertFalse(page.context['can_manage_author_notes'])
        url = reverse('core:add_author_note', args=[self.signed.pk])
        self.assertEqual(self.client.post(url, {'content': 'Próba'}).status_code, 403)
        self.assertEqual(self.client.get(reverse('core:author_detail', args=[99999])).status_code, 404)

    def test_superuser_adds_note_and_empty_note_is_rejected(self):
        self.client.force_login(self.admin)
        url = reverse('core:add_author_note', args=[self.signed.pk])
        page = self.client.post(url, {'content': '   '})
        self.assertEqual(page.status_code, 400)
        self.assertIn('Wpisz treść', str(page.context['author_note_form'].errors))
        response = self.client.post(url, {'content': ' Nowa notatka '})
        self.assertRedirects(response, reverse('core:author_detail', args=[self.signed.pk]))
        note = AuthorNote.objects.get()
        self.assertEqual((note.content, note.created_by), ('Nowa notatka', self.admin))
        self.assertEqual(self.client.post(reverse('core:add_author_note', args=[99999]), {'content': 'X'}).status_code,
                         404)
