"""Przypisanie osoby spoza spisu ilustratorów na stronie ilustracji: dopisanie do spisu przy zapisie,
data przypisania, odrzucenie osób już obecnych w spisie, konflikt wersji i brak pól dla osób bez
uprawnień koordynatora."""
from datetime import timedelta
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from fantazmaty.testing import create_member, create_superuser
from illustrations.editing import AssignmentForm, edit_token
from illustrations.models import Illustration, Illustrator
from people.models import Person
from texts.models import Anthology, Text


class NewIllustratorAssignmentTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('new-artist-admin')
        cls.artist = create_member('new-artist-member', 'Ilustrator')
        cls.contact = Illustrator.objects.create(first_name='Artysta', last_name='Kontakt',
                                                 email='kontakt@example.test')
        cls.hidden = Illustrator.objects.create(first_name='Ukryta', last_name='Osoba', pseudonym='Cień',
                                                is_active=False)
        cls.book = Anthology.objects.create(title='Antologia', has_illustrations=True)
        cls.text = Text.objects.create(title='Tekst', anthology=cls.book, length=20)
        cls.ill = Illustration.objects.get(text=cls.text)

    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        settings = self.settings(ACTIVITY_SPOOL_DIR=tmp.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.client.force_login(self.admin)
        self.url = reverse('illustrations:illustration_detail', args=[self.ill.pk])

    def post(self, version=None, **values):
        self.ill.refresh_from_db()
        return self.client.post(self.url, {'action': 'assignment', 'status': 'assigned',
                                           'version': version or edit_token(self.admin, self.ill), **values})

    def assert_rejected(self, response, field='new_illustrator_name'):
        self.assertEqual(response.status_code, 200)
        self.assertIn(field, response.context['forms']['assignment'].errors)
        self.assertEqual(Illustrator.objects.count(), 2)
        self.ill.refresh_from_db()
        self.assertEqual((self.ill.status, list(self.ill.illustrators.all())), ('unassigned', []))

    def test_new_person_is_added_to_directory_and_assigned(self):
        people, accounts = Person.objects.count(), get_user_model().objects.count()
        response = self.post(new_illustrator_name='  Anna   Maria Nowa ', new_illustrator_email='anna@example.test')
        self.assertEqual(response.status_code, 302)
        anna = Illustrator.objects.get(email='anna@example.test')
        self.assertEqual((anna.first_name, anna.last_name, anna.is_active), ('Anna Maria', 'Nowa', True))
        self.ill.refresh_from_db()
        self.assertEqual(list(self.ill.illustrators.all()), [anna])
        self.assertEqual(self.ill.assigned_at, timezone.localdate())
        self.assertEqual((Person.objects.count(), get_user_model().objects.count()), (people, accounts))
        self.assertContains(self.client.get(reverse('illustrations:illustration_list')), 'Anna Maria Nowa')
        # Osoba bez e-maila dołącza do już przypisanych; zmiana składu ustawia nową datę przypisania.
        Illustration.objects.filter(pk=self.ill.pk).update(assigned_at=timezone.localdate() - timedelta(days=9))
        response = self.post(illustrators=[anna.pk], new_illustrator_name='Graphos', status='delivered')
        self.assertEqual(response.status_code, 302)
        graphos = Illustrator.objects.get(first_name='Graphos')
        self.assertEqual((graphos.last_name, graphos.email), ('', None))
        self.ill.refresh_from_db()
        self.assertEqual(set(self.ill.illustrators.all()), {anna, graphos})
        self.assertEqual((self.ill.status, self.ill.assigned_at), ('delivered', timezone.localdate()))

    def test_person_already_in_directory_is_rejected(self):
        """E-mail (bez względu na wielkość liter), imię i nazwisko albo pseudonim – także nieaktywnego wpisu."""
        for values in ({'new_illustrator_name': 'Ktoś Inny', 'new_illustrator_email': 'KONTAKT@example.test'},
                       {'new_illustrator_name': ' artysta   KONTAKT '},
                       {'new_illustrator_name': 'Ukryta Osoba'},
                       {'new_illustrator_name': 'cień'}):
            with self.subTest(values=values):
                response = self.post(**values)
                self.assert_rejected(response)
                self.assertContains(response, 'Ta osoba jest już w spisie ilustratorów')

    def test_email_requires_name_and_valid_address(self):
        self.assert_rejected(self.post(new_illustrator_email='nowy@example.test'))
        self.assert_rejected(self.post(new_illustrator_name='Nowa Osoba', new_illustrator_email='zły-adres'),
                             'new_illustrator_email')

    def test_new_person_with_unassigned_status_is_not_saved(self):
        self.assert_rejected(self.post(new_illustrator_name='Nowa Osoba', status='unassigned'), 'status')

    def test_stale_version_neither_adds_person_nor_changes_assignment(self):
        old = edit_token(self.admin, self.ill)
        self.ill.set_artists([self.contact], status='assigned')
        response = self.post(version=old, new_illustrator_name='Spóźniona Osoba')
        self.assertEqual(response.status_code, 409)
        self.assertContains(response, 'Spóźniona Osoba', status_code=409)
        self.assertFalse(Illustrator.objects.filter(first_name='Spóźniona').exists())
        self.ill.refresh_from_db()
        self.assertEqual(list(self.ill.illustrators.all()), [self.contact])

    def test_without_coordinator_rights_there_are_no_assignment_fields(self):
        form = AssignmentForm(instance=self.ill)
        for name in ('illustrators', 'new_illustrator_name', 'new_illustrator_email'):
            self.assertNotIn(name, form.fields)
        self.assertIn('new_illustrator_name', AssignmentForm(instance=self.ill, can_assign=True).fields)
        self.client.force_login(self.artist)
        self.assertNotContains(self.client.get(self.url), 'name="new_illustrator_name"')
        response = self.client.post(self.url, {'action': 'assignment', 'status': 'assigned',
                                               'new_illustrator_name': 'Nieuprawniony Wpis'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Illustrator.objects.filter(first_name='Nieuprawniony').exists())

    def test_too_long_name_is_a_form_error(self):
        """Za długie imię lub nazwisko (ponad 100 znaków) to błąd formularza, nie błąd 500."""
        self.assert_rejected(self.post(new_illustrator_name='A' * 150 + ' Nowak'))
