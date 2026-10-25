"""Strony urlopów: zgłaszanie, edycja, zakończenie i odwołanie, lista trwających urlopów oraz urlopy w panelu admina."""
from datetime import timedelta
from unittest.mock import patch

from django.contrib.messages import get_messages
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from lxml import html

from fantazmaty.testing import create_member, create_superuser, edit_token
from people.models import Person, Vacation


class VacationPageFixtures(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.member = create_member('urlop-member', 'Redaktor')
        cls.colleague = create_member('urlop-colleague', 'Korektor')
        cls.coordinator = create_member('urlop-coord', 'Koordynator redakcji')
        cls.today = timezone.localdate()

    def vacation(self, user, start_days=0, end_days=None, **values):
        """Urlop zaczynający się za podaną liczbę dni (do odwołania, gdy nie podano końca)."""
        if end_days is None:
            values.setdefault('until_revoked', True)
        else:
            values['end_date'] = timezone.now() + timedelta(days=end_days)
        return Vacation.objects.create(person=user.person_profile, start_date=self.today + timedelta(days=start_days),
                                       **values)

    def message(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)][-1]

    def day(self, days):
        return (self.today + timedelta(days=days)).isoformat()


class MyVacationsPageTests(VacationPageFixtures):
    url = reverse('core:my_vacations')

    def test_member_reports_own_vacation_and_cannot_choose_someone_else(self):
        self.client.force_login(self.member)
        page = self.client.get(self.url)
        self.assertEqual(page.context['vacation_people'], [])
        self.assertFalse(page.context['can_manage_team_vacations'])
        response = self.client.post(self.url, {'person': self.colleague.person_profile.pk,
                                               'start_date': self.day(2), 'until_revoked': 'on'})
        self.assertRedirects(response, self.url, fetch_redirect_response=False)
        self.assertEqual(self.message(response), 'Zgłoszono urlop.')
        vacation = Vacation.objects.get()
        self.assertEqual((vacation.person, vacation.start_date), (self.member.person_profile,
                                                                  self.today + timedelta(days=2)))

    def test_invalid_or_overlapping_vacation_is_returned_with_errors(self):
        self.client.force_login(self.member)
        page = self.client.post(self.url, {'start_date': self.day(3), 'end_date': self.day(1)})
        self.assertEqual(page.status_code, 400)
        self.assertTrue(page.context['form'].errors)
        self.vacation(self.member, 1, 5, until_revoked=False)
        page = self.client.post(self.url, {'start_date': self.day(2), 'end_date': self.day(4)})
        self.assertEqual(page.status_code, 400)
        self.assertIn('pokrywa się', str(page.context['form'].errors['start_date']))
        self.assertEqual(Vacation.objects.count(), 1)

    def test_coordinator_reports_vacation_for_selected_active_member(self):
        self.client.force_login(self.coordinator)
        page = self.client.get(self.url)
        self.assertTrue(page.context['can_manage_team_vacations'])
        self.assertIn(self.colleague.person_profile, page.context['vacation_people'])
        response = self.client.post(self.url, {'person': self.colleague.person_profile.pk,
                                               'start_date': self.day(0), 'until_revoked': 'on'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Vacation.objects.get().person, self.colleague.person_profile)
        former = Person.objects.create(first_name='Była', last_name='Osoba', is_active=False)
        for person in (former.pk, 'abc', '999999'):
            with self.subTest(person=person):
                self.assertEqual(self.client.post(self.url, {'person': person, 'start_date': self.day(1),
                                                             'until_revoked': 'on'}).status_code, 404)
        self.assertEqual(Vacation.objects.count(), 1)

    def test_superuser_without_profile_uses_first_member_or_returns_home(self):
        admin = create_superuser('urlop-admin')
        self.client.force_login(admin)
        page = self.client.get(self.url)
        self.assertEqual(page.context['person'], Person.objects.active().order_by('last_name', 'pk').first())
        self.assertEqual(list(page.context['vacations']), [])  # Historia urlopów dotyczy tylko własnego profilu.
        Person.objects.update(is_active=False)
        response = self.client.get(self.url)
        self.assertRedirects(response, reverse('core:home'), fetch_redirect_response=False)
        self.assertIn('nie jest połączone', self.message(response))

    def test_disabled_account_not_in_vacation_choices(self):
        self.colleague.is_active = False
        self.colleague.save(update_fields=['is_active'])
        self.client.force_login(self.coordinator)
        page = self.client.get(self.url)
        self.assertNotIn(self.colleague.person_profile.pk, [p.pk for p in page.context['vacation_people']])

    def test_my_vacations_show_entire_own_history_without_pagination(self):
        start = timezone.now() - timedelta(days=365)
        person = self.member.person_profile
        Vacation.objects.bulk_create([
            Vacation(person=person, start_date=(start + timedelta(days=i * 3)).date(),
                     end_date=start + timedelta(days=i * 3 + 1)) for i in range(60)
        ])
        Vacation.objects.create(person=self.colleague.person_profile, start_date=start.date(),
                                end_date=start + timedelta(days=1))
        self.client.force_login(self.member)
        response = self.client.get(self.url, {'page': 2, 'page_size': 25})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['vacations']), 60)
        self.assertTrue(all(v.person_id == person.pk for v in response.context['vacations']))
        doc = html.fromstring(response.content)
        self.assertEqual(len(doc.xpath('//*[@id="my-vacations-table"]/tbody/tr')), 60)
        self.assertEqual(doc.xpath('//*[@id="my-vacations-table"]/@data-pagination'), ['off'])
        self.assertFalse(doc.xpath('//*[contains(concat(" ",normalize-space(@class)," ")," cms-pagination ")]'))


class VacationChangePageTests(VacationPageFixtures):
    def edit_url(self, vacation):
        return reverse('core:edit_vacation', args=[vacation.pk])

    def test_member_edits_own_planned_vacation(self):
        vacation = self.vacation(self.member, 3)
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(self.edit_url(vacation)).status_code, 200)
        page = self.client.post(self.edit_url(vacation), {'start_date': self.day(5), 'end_date': self.day(4)})
        self.assertEqual(page.status_code, 400)
        self.assertEqual(page.context['vacation'].start_date, self.today + timedelta(days=3))
        response = self.client.post(self.edit_url(vacation), {'start_date': self.day(4), 'end_date': self.day(6)})
        self.assertRedirects(response, reverse('core:my_vacations'), fetch_redirect_response=False)
        self.assertEqual(self.message(response), 'Zapisano zmiany urlopu.')
        vacation.refresh_from_db()
        self.assertEqual((vacation.start_date, vacation.until_revoked), (self.today + timedelta(days=4), False))

    def test_edit_that_overlaps_another_vacation_is_rejected(self):
        self.vacation(self.member, 10, 12, until_revoked=False)
        vacation = self.vacation(self.member, 3, 5, until_revoked=False)
        self.client.force_login(self.member)
        page = self.client.post(self.edit_url(vacation), {'start_date': self.day(3), 'until_revoked': 'on'})
        self.assertEqual(page.status_code, 400)
        self.assertIn('pokrywa się', str(page.context['form'].errors))
        vacation.refresh_from_db()
        self.assertFalse(vacation.until_revoked)

    def test_service_errors_after_valid_form_are_shown_on_form(self):
        """Błąd usługi (np. zmiana stanu w międzyczasie) trafia do pola formularza albo do błędów ogólnych."""
        self.client.force_login(self.member)
        error = ValidationError({'start_date': 'Kolizja', 'person': 'Osoba poza formularzem'})
        with patch('core.views.vacations.create_vacation', side_effect=error):
            page = self.client.post(reverse('core:my_vacations'), {'start_date': self.day(1), 'until_revoked': 'on'})
        self.assertEqual(page.status_code, 400)
        self.assertEqual(page.context['form'].errors['start_date'], ['Kolizja'])
        self.assertEqual(page.context['form'].non_field_errors(), ['Osoba poza formularzem'])
        vacation = self.vacation(self.member, 3)
        with patch('core.views.vacations.update_vacation', side_effect=ValidationError('Urlop zmienił się')):
            page = self.client.post(self.edit_url(vacation), {'start_date': self.day(3), 'until_revoked': 'on'})
        self.assertEqual(page.status_code, 400)
        self.assertEqual(page.context['form'].non_field_errors(), ['Urlop zmienił się'])

    def test_only_owner_or_coordinator_reaches_active_members_vacation(self):
        vacation = self.vacation(self.member, 3)
        self.client.force_login(self.colleague)
        self.assertEqual(self.client.get(self.edit_url(vacation)).status_code, 404)
        for name in ('end_vacation', 'cancel_vacation'):
            self.assertEqual(self.client.post(reverse(f'core:{name}', args=[vacation.pk])).status_code, 404)
        self.client.force_login(self.coordinator)
        response = self.client.post(self.edit_url(vacation), {'start_date': self.day(3), 'end_date': self.day(4)})
        self.assertRedirects(response, reverse('core:active_vacations'), fetch_redirect_response=False)
        Person.objects.filter(pk=self.member.person_profile.pk).update(is_active=False)
        self.assertEqual(self.client.get(self.edit_url(vacation)).status_code, 404)

    def test_finished_vacation_cannot_be_edited(self):
        vacation = Vacation.objects.create(person=self.member.person_profile, start_date=self.today - timedelta(days=5),
                                           end_date=timezone.now() - timedelta(days=1))
        self.client.force_login(self.member)
        response = self.client.get(self.edit_url(vacation))
        self.assertRedirects(response, reverse('core:my_vacations'), fetch_redirect_response=False)
        self.assertEqual(self.message(response), 'Zakończonego urlopu nie można edytować.')

    def test_end_only_active_and_cancel_only_planned_vacation(self):
        planned = self.vacation(self.member, 3)
        current = self.vacation(self.member, 0, 2, until_revoked=False)
        self.client.force_login(self.member)
        end, cancel = (lambda v: reverse('core:end_vacation', args=[v.pk]),
                       lambda v: reverse('core:cancel_vacation', args=[v.pk]))
        self.assertIn('wyłącznie trwający urlop', self.message(self.client.post(end(planned))))
        self.assertTrue(self.message(self.client.post(cancel(current))))
        self.assertTrue(Vacation.objects.filter(pk=current.pk).exists())
        response = self.client.post(cancel(planned))
        self.assertEqual(self.message(response), 'Odwołano zaplanowany urlop.')
        self.assertFalse(Vacation.objects.filter(pk=planned.pk).exists())
        response = self.client.post(end(current), {'_edit_version': edit_token(self.member, current)})
        self.assertEqual(self.message(response), 'Zakończono urlop.')
        current.refresh_from_db()
        self.assertTrue(current.is_finished)

    def test_active_vacations_list_shows_only_current_leave_of_active_members(self):
        current = self.vacation(self.member, 0)
        self.vacation(self.colleague, 5)
        Vacation.objects.create(person=self.coordinator.person_profile, start_date=self.today - timedelta(days=3),
                                end_date=timezone.now() - timedelta(hours=1))
        former = Person.objects.create(first_name='Była', last_name='Osoba', is_active=False)
        Vacation.objects.create(person=former, start_date=self.today, until_revoked=True)
        self.client.force_login(self.colleague)
        page = self.client.get(reverse('core:active_vacations'))
        self.assertEqual(list(page.context['vacations']), [current])


class VacationAdminTests(VacationPageFixtures):
    def setUp(self):
        self.admin = create_superuser('urlop-superuser')
        self.client.force_login(self.admin)

    def test_admin_adds_vacation_for_active_member_and_person_page_shows_it(self):
        """Urlop dodany w panelu widać na ekranie osoby (pola tylko do odczytu wyliczane z urlopów)."""
        from django.utils import formats
        url = reverse('admin:people_vacation_add')
        former = Person.objects.create(first_name='Była', last_name='Osoba', is_active=False)
        data = {'start_date': self.day(0), 'until_revoked': 'on', '_save': 'Zapisz'}
        page = self.client.post(url, {**data, 'person': former.pk})
        self.assertEqual(page.status_code, 200)
        self.assertIn('nie jest już w zespole', str(page.context['adminform'].form.errors['person']))
        response = self.client.post(url, {**data, 'person': self.member.person_profile.pk})
        self.assertEqual(response.status_code, 302)
        page = self.client.get(reverse('admin:people_person_change', args=[self.member.person_profile.pk]))
        leave = html.fromstring(page.content.decode()).xpath('//fieldset[.//h2[text()="Urlop"]]')[0]
        values = [' '.join(node.text_content().split()) for node in leave.xpath('.//div[@class="readonly"]')]
        self.assertEqual(values[:2], [formats.localize(self.today), '-'])
        self.assertEqual(leave.xpath('.//div[@class="readonly"]/img/@alt'), ['True'])

    def test_only_planned_vacation_can_be_deleted_also_for_former_member(self):
        started = self.vacation(self.member, 0)
        planned = self.vacation(self.colleague, 10, 12, until_revoked=False)
        Person.objects.filter(pk=self.colleague.person_profile.pk).update(is_active=False)
        for vacation, status in ((started, 403), (planned, 302)):
            response = self.client.post(reverse('admin:people_vacation_delete', args=[vacation.pk]),
                                        {'post': 'yes', '_edit_version': edit_token(self.admin, vacation)})
            self.assertEqual(response.status_code, status)
        self.assertFalse(Vacation.objects.filter(pk=planned.pk).exists())
        self.assertTrue(Vacation.objects.filter(pk=started.pk).exists())
