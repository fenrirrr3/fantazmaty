"""Przeniesienie przyjętego zgłoszenia do procesu wydawniczego: warunki, profil autora, umowy i telefon."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from authors.models import Author
from core.services.reviews import (
    copy_review_to_text,
    resolve_publication_authors,
    validate_review_publication,
)
from texts.models import Review, Text

from .support import ReviewFixtures

S = Review.Status


class PublicationTransferRuleTests(ReviewFixtures):
    def setUp(self):
        super().setUp()
        self.accepted = self.make_review(status=S.ACCEPTED, author_notified_at=timezone.localdate(),
                                         phone_number=' 600 100 200 ', author_pseudonym='Pióro')

    def copy(self, review=None, **options):
        return copy_review_to_text(user=self.admin, review_id=(review or self.accepted).pk, **options)

    def test_new_author_requires_contract_and_is_created_from_submission(self):
        with self.assertRaises(PermissionDenied):
            copy_review_to_text(user=self.coordinator, review_id=self.accepted.pk, contract_received=True)
        with self.assertRaisesMessage(ValidationError, 'Nieprawidłowe potwierdzenie'):
            self.copy(contract_received='yes')
        with self.assertRaisesMessage(ValidationError, 'potwierdź otrzymanie umowy'):
            self.copy()
        self.assertFalse(Author.objects.exists())
        text = self.copy(contract_received=True)
        author = Author.objects.get()
        self.assertEqual((author.pseudonym, author.phone_number, author.has_contract), ('Pióro', '600 100 200', True))
        self.assertEqual(list(text.authors.all()), [author])
        self.assertEqual(text.workflow_stages.get().stage_type, 'ready_for_editing')

    def test_existing_author_matched_by_email_and_name_gets_contract_and_phone(self):
        author = Author.objects.create(first_name='Autor', last_name='Test', email='AUTHOR@example.com',
                                       phone_number='111')
        with self.assertRaisesMessage(ValidationError, 'nie ma potwierdzonej umowy'):
            self.copy()
        self.copy(contract_received=True, update_author_phone=True)
        author.refresh_from_db()
        self.assertEqual((author.has_contract, author.phone_number), (True, '600 100 200'))
        self.accepted.refresh_from_db()
        self.assertEqual(self.accepted.author, author)
        linked = self.make_review(title='Powiązane', status=S.ACCEPTED, author=author, email='inny@example.com',
                                  author_notified_at=timezone.localdate())
        self.assertEqual(list(self.copy(linked).authors.all()), [author])

    def test_mismatched_or_missing_email_blocks_transfer(self):
        Author.objects.create(first_name='Inna', last_name='Osoba', email='author@example.com')
        with self.assertRaisesMessage(ValidationError, 'innych danych'):
            self.copy(contract_received=True)
        Review.objects.filter(pk=self.accepted.pk).update(email='')
        with self.assertRaisesMessage(ValidationError, 'uzupełnij adres e-mail'):
            self.copy(contract_received=True)
        self.assertFalse(Text.objects.exists())

    def test_coauthor_contracts_must_be_confirmed_explicitly(self):
        signed = Author.objects.create(first_name='Ma', last_name='Umowę', email='a@example.test', has_contract=True)
        unsigned = Author.objects.create(first_name='Bez', last_name='Umowy', email='b@example.test')
        self.accepted.coauthors.add(signed, unsigned)
        for ids, message in ((['x'], 'Nieprawidłowa lista'), ([unsigned.pk + 100], 'zmieniła się'),
                             ([], 'Bez Umowy')):
            with self.subTest(ids=ids), self.assertRaisesMessage(ValidationError, message):
                self.copy(contract_received=True, confirmed_coauthor_ids=ids)
        with self.assertRaisesMessage(ValidationError, 'zmiany telefonu'):
            resolve_publication_authors(self.accepted, update_author_phone='yes')
        author, coauthors = resolve_publication_authors(self.accepted, contract_received=True,
                                                        confirmed_coauthor_ids=[str(unsigned.pk)], persist=False)
        self.assertIsNone(author.pk)
        unsigned.refresh_from_db()
        self.assertFalse(unsigned.has_contract)
        text = self.copy(contract_received=True, confirmed_coauthor_ids=[str(unsigned.pk)])
        unsigned.refresh_from_db()
        self.assertTrue(unsigned.has_contract)
        self.assertEqual(text.authors.count(), 3)

    def test_publication_gates_and_contract_check(self):
        cases = [({'publication_detached': True}, 'odłączono'), ({'status': S.REJECTED}, 'wyłącznie przyjęty'),
                 ({'author_notified_at': None}, 'powiadomienie autora')]
        for values, message in cases:
            review = self.make_review(**{'status': S.ACCEPTED, 'author_notified_at': timezone.localdate(),
                                         **values})
            with self.subTest(values=values), self.assertRaisesMessage(ValidationError, message):
                validate_review_publication(review)
        with self.assertRaisesMessage(ValidationError, 'utwórz profil autora'):
            validate_review_publication(self.accepted, contracts=True)
        author = Author.objects.create(first_name='Autor', last_name='Test', email='author@example.com')
        with self.assertRaisesMessage(ValidationError, 'potwierdź umowy'):
            validate_review_publication(self.accepted, contracts=True)
        Author.objects.filter(pk=author.pk).update(has_contract=True)
        validate_review_publication(self.accepted, contracts=True)
