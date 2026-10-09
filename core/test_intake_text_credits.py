from core.services.review_import_parser import encode_submission
from core.services.reviews import import_reviews
from core.forms import ReviewBulkImportForm
from core.supervision import text_credit_groups
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from texts.models import Anthology, Text, Review, ReviewAssignment
from people.models import Person
from workflow.completed_import import import_completed_workflow

class TextCreditTests(TestCase):
    def test_only_this_text_completed_work_and_reviews(self):
        book=Anthology.objects.create(title='Antologia',cover_author='Inna osoba')
        a=Text.objects.create(title='Pierwszy',anthology=book,length=100)
        b=Text.objects.create(title='Drugi',anthology=book,length=100)
        for text,name in ((a,'Ala'),(b,'Ewa')):
            u=get_user_model().objects.create_superuser(name,name+'@example.com','password')
            Person.objects.create(user=u,email=u.email,first_name=name,last_name='Test')
            import_completed_workflow(text_id=text.pk,next_stage='ready',stages=[{'stage_type':'editing','assigned_to':u.email},{'stage_type':'styling','assigned_to':u.email}])
            review=Review.objects.create(title=text.title,anthology=book,copied_text=text,status='accepted',length=100)
            ReviewAssignment.objects.create(review=review,user=u,position=1,opinion='yes')
        groups={row['label']:row['names'] for row in text_credit_groups(a)}
        self.assertEqual(groups['Redakcja'],['Ala Test'])
        self.assertEqual(groups['Kontrola przed składem'],['Ala Test'])
        self.assertEqual(groups['Recenzje'],['Ala Test'])
        self.assertEqual(groups['Ilustracja'],[])

    def test_old_and_new_formats_save_correct_warning_and_consents(self):
        book = Anthology.objects.create(title="Nabór")
        user = get_user_model().objects.create_superuser("admin", "admin@example.com", "password")
        records = (
            "Ala Autor;Stary;fantasy;37930;ala@example.com;123456789;Nabór;premierach, naborach\n"
            + encode_submission(
                [
                    "Ewa Autor",
                    "Nowy",
                    "fantasy",
                    "PRZEMOC",
                    "1234",
                    "ewa@example.com",
                    "",
                    "naborach",
                    "",
                ]
            )
        )
        form = ReviewBulkImportForm({"anthology": book.pk, "records": records}, user=user)
        self.assertTrue(form.is_valid(), form.errors)
        import_reviews(user=user, form=form)
        self.assertEqual(Review.objects.get(title="Stary").content_warnings, "")
        self.assertEqual(Review.objects.get(title="Stary").length, 37930)
        self.assertEqual(Review.objects.get(title="Nowy").content_warnings, "przemoc")
        self.client.force_login(user)
        response = self.client.post(
            reverse("core:review_bulk_submit"),
            {
                "anthology": book.pk,
                "records": "Jan Inny;Trzeci;fantasy;99;j@example.com;;Nabór;premierach",
            },
        )
        self.assertContains(response, "intake-stack", status_code=response.status_code)
        self.assertNotContains(response, "intake-columns", status_code=response.status_code)
        self.assertNotContains(response, "Ostrzeżenia: –", status_code=response.status_code)
