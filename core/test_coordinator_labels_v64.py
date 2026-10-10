from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from core.permissions import is_coordinator
from people.models import Person, Role, Vacation


class CoordinatorLabelsTests(TestCase):
    def test_role_displays_do_not_invent_generic_role_and_access_is_preserved(self):
        admin = get_user_model().objects.create_superuser('labels-admin', 'labels@example.test', 'test')
        self.client.force_login(admin)
        for index, roles in enumerate((['Koordynator redakcji', 'Prawa ręka'], [])):
            user = get_user_model().objects.create_user(f'labels-{index}')
            person = Person.objects.create(user=user, first_name='Jan', last_name=f'Osoba {index}',
                legacy_coordinator_access=not roles)
            person.roles.add(*(Role.objects.get_or_create(name=name)[0] for name in roles))
            Vacation.objects.create(person=person, start_date=timezone.localdate(), until_revoked=True)
            self.assertTrue(is_coordinator(user))
            for route, args in [('people_list', []), ('person_detail', [person.pk]), ('active_vacations', [])]:
                page = self.client.get(reverse(f'core:{route}', args=args))
                self.assertEqual(page.status_code, 200)
                doc = html.fromstring(page.content)
                labels = [node.text_content().strip() for node in doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," role-badge ")]')]
                self.assertNotIn('Koordynator', labels)
                self.assertNotIn('Koordynator zespołu', doc.text_content())
                if roles:
                    self.assertIn('Koordynator redakcji', labels)
                    self.assertIn('Prawa ręka', labels)
                else:
                    self.assertContains(page, 'Brak przypisanych ról')
            self.assertTrue(is_coordinator(user))
        self.assertFalse(Role.objects.filter(name='Koordynator').exists())
