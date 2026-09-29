from email.message import EmailMessage
from tempfile import TemporaryDirectory
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from texts.models import Review, Anthology, Text
from people.models import Person, Role
from core.models import NewsletterConsent
from core.forms import ReviewBulkImportForm
from core.services.reviews import import_reviews
from core.services.newsletters import record_consents
from core.services.review_import_parser import parse_review_records, encode_submission
from core.services.mailbox_import import parse_message
from core.selectors.people import role_names_context

class SubmissionConsentTests(TestCase):
    def setUp(self):
        self.tmp=TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        override=self.settings(ACTIVITY_SPOOL_DIR=self.tmp.name);override.enable();self.addCleanup(override.disable)
        self.admin=get_user_model().objects.create_superuser('admin','admin@example.com','password')
        self.coord=get_user_model().objects.create_user('coord','coord@example.com')
        self.person=Person.objects.create(user=self.coord,email=self.coord.email,first_name='Adam',last_name='Zeta')
        self.role=Role.objects.get_or_create(name='Koordynator recenzji')[0];self.person.roles.add(self.role)
        self.book=Anthology.objects.create(title='Nabór testowy')
        self.fields=['Jan Autor','Opowieść','fantasy','PRZEMOC', '1234','jan@example.com','123456789','premierach, naborach','Prywatne; zdanie.\nDrugi wiersz.']

    def test_parser_new_and_legacy_and_unknown_consent(self):
        rows, errors=parse_review_records(encode_submission(self.fields))
        self.assertEqual(errors,[]);row=rows[0]
        self.assertEqual(row['length'],1234);self.assertEqual(row['content_warnings'],'przemoc')
        self.assertEqual(row['author_message'],self.fields[8]);self.assertTrue(row['newsletter_premieres']);self.assertTrue(row['newsletter_recruitment'])
        legacy,errors=parse_review_records('Jan Autor;Stary;fantasy;1234;;jan@example.com;')
        self.assertFalse(legacy[0]['has_consent_fields']);self.assertFalse(errors)
        self.fields[7]='nieznane'
        self.assertTrue(parse_review_records(encode_submission(self.fields))[1])

    def test_bulk_preview_no_writes_then_commit(self):
        form=ReviewBulkImportForm(data={'anthology':self.book.pk,'records':encode_submission(self.fields)},user=self.admin)
        self.assertTrue(form.is_valid(),form.errors)
        self.assertFalse(NewsletterConsent.objects.exists());self.assertFalse(Review.objects.exists())
        import_reviews(user=self.admin,form=form)
        consent=NewsletterConsent.objects.get();self.assertTrue(consent.premieres and consent.recruitment)
        review=Review.objects.get();self.assertEqual(review.author_message,self.fields[8])
        self.assertEqual(review.content_warnings,'przemoc')

    def test_consents_merge_and_filter_superuser_only(self):
        record_consents('JAN@example.com',premieres=True)
        record_consents('jan@example.com',recruitment=True)
        record_consents('jan@example.com')
        record_consents('other@example.com')
        self.assertEqual(NewsletterConsent.objects.count(),2)
        url=reverse('core:newsletter_list')
        self.client.force_login(self.coord);self.assertEqual(self.client.get(url).status_code,403)
        self.client.force_login(self.admin)
        response=self.client.get(url,{'premieres':'yes','recruitment':'yes'})
        self.assertContains(response,'jan@example.com');self.assertNotContains(response,'other@example.com')

    def test_message_private_and_escaped(self):
        review=Review.objects.create(anthology=self.book,title='Test',author_message='<script>private-marker</script>',length=1234)
        url=reverse('core:assigned_review_detail',args=[review.pk])
        self.client.force_login(self.coord);response=self.client.get(url)
        self.assertNotContains(response,'private-marker');self.assertNotIn('author_message',response.context)
        self.client.force_login(self.admin);response=self.client.get(url)
        self.assertContains(response,'&lt;script&gt;private-marker&lt;/script&gt;')

    def test_mail_new_fields_and_subject(self):
        msg=EmailMessage();msg['Subject']='Nabór: „Nabór testowy” – Opowieść'
        msg.set_content(';'.join(self.fields)+'\n--- KONIEC WIADOMOŚCI AUTORA ---\nStopka techniczna')
        msg.add_attachment(b'not empty',maintype='application',subtype='vnd.openxmlformats-officedocument.wordprocessingml.document',filename='tekst.docx')
        parsed=parse_message(1,msg.as_bytes())
        self.assertEqual(parsed['anthology'],'Nabór testowy')
        rows,errors=parse_review_records(parsed['record']);self.assertFalse(errors)
        self.assertEqual(rows[0]['author_message'],self.fields[8])
        self.assertTrue(rows[0]['newsletter_recruitment'])

    def test_role_names_surname_order_and_detail_access(self):
        user=get_user_model().objects.create_user('other','other@example.com')
        person=Person.objects.create(user=user,email=user.email,first_name='Zofia',last_name='Alfa');person.roles.add(self.role)
        self.assertEqual(role_names_context({'role':str(self.role.pk)})['names'],['Zofia Alfa','Adam Zeta'])
        text=Text.objects.create(anthology=self.book,title='Tekst',length=1000)
        url=reverse('core:assigned_text_detail',args=[text.pk])
        self.client.force_login(self.admin);self.assertContains(self.client.get(url),'Osoby i wykonane prace przy tym tekście')
        self.assertNotContains(self.client.get(url),'Zofia Alfa, Adam Zeta')
        self.client.force_login(self.coord);self.assertNotContains(self.client.get(url),'Lista osób z zespołu według roli')

    def test_single_form_saves_new_fields(self):
        self.client.force_login(self.admin)
        url=reverse('core:review_create')
        page=self.client.get(url)
        self.assertContains(page,'newsletter_premieres')
        data={'author_first_name':'Jan','author_last_name':'Autor','email':'jan@example.com',
              'title':'Pojedynczy','genre':'fantasy','length':1234,'anthology':self.book.pk,
              'content_warnings':'PRZEMOC','author_message':'Tylko administrator',
              'newsletter_premieres':'on'}
        response=self.client.post(url,data)
        self.assertEqual(response.status_code,302, getattr(response,'context',None))
        consent=NewsletterConsent.objects.get();self.assertTrue(consent.premieres);self.assertFalse(consent.recruitment)
        self.assertEqual(Review.objects.get().author_message,'Tylko administrator')

    def test_invalid_batch_and_failed_save_do_not_leave_consents(self):
        from unittest.mock import patch
        form=ReviewBulkImportForm(data={'anthology':self.book.pk,'records':encode_submission(self.fields)+'\nNiepoprawne'},user=self.admin)
        self.assertFalse(form.is_valid());self.assertFalse(NewsletterConsent.objects.exists())
        form=ReviewBulkImportForm(data={'anthology':self.book.pk,'records':encode_submission(self.fields)},user=self.admin)
        with patch('core.services.newsletters.record_consents',side_effect=RuntimeError('failure')):
            with self.assertRaises(RuntimeError):import_reviews(user=self.admin,form=form)
        self.assertFalse(Review.objects.exists());self.assertFalse(NewsletterConsent.objects.exists())
