"""Import zgłoszeń: ostrzeżenie, które pojawi się między walidacją formularza a zapisem, wraca do potwierdzenia."""
from django.core.exceptions import ValidationError

from authors.models import Author
from core.forms import ReviewBulkImportForm
from core.services.reviews import import_reviews
from texts.models import Review

from .support import ReviewFixtures

RECORD = 'Ala Autor;Nowa opowieść;fantasy;3000;ala@example.com;123456789;Nabór;premierach'


class ReviewImportRefreshTests(ReviewFixtures):
    def test_warning_found_during_import_refreshes_form_for_new_confirmation(self):
        form = ReviewBulkImportForm({'anthology': self.book.pk, 'records': RECORD}, user=self.admin)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertFalse(form['submission_warnings_token'].value())
        # Autor trafia na czarną listę po sprawdzeniu formularza, a przed zapisem.
        Author.objects.create(first_name='Ala', last_name='Autor', email='ala@example.com', is_blacklisted=True)
        with self.assertRaisesMessage(ValidationError, 'Wykryto ostrzeżenia'):
            import_reviews(user=self.admin, form=form)
        self.assertFalse(Review.objects.filter(title='Nowa opowieść').exists())
        # Formularz widoku dostaje nowe ostrzeżenia i token, a zachowuje wpisane rekordy.
        self.assertTrue(any('czarnej liście' in warning for warning in form.import_warnings))
        token = form['submission_warnings_token'].value()
        self.assertTrue(token)
        self.assertEqual(form['records'].value(), RECORD)
        self.assertEqual(form.warning_payload['anthology_id'], self.book.pk)
        confirmed = ReviewBulkImportForm({**form.data, 'confirm_submission_warnings': 'on'}, user=self.admin)
        self.assertTrue(confirmed.is_valid(), confirmed.errors)
        self.assertEqual(import_reviews(user=self.admin, form=confirmed), 1)
        self.assertTrue(Review.objects.get(title='Nowa opowieść').is_hidden)
