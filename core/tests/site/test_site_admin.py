"""Panel admina strony WWW: działy z podgrupami w jednym formularzu, pojedynczy rekord ustawień, grupa „Ustawienia”
w nawigacji i walidacja modeli (notka oczyszczona, ręczna podgrupa bez nazwisk)."""
from django.contrib import admin
from django.core.exceptions import ValidationError
from django.test import RequestFactory, TestCase
from django.urls import reverse

from core.site_models import SiteTeamGroup, SiteTeamSection, SiteTeamSettings
from fantazmaty.testing import create_member, create_superuser
from people import roles
from people.models import Role


class SiteTeamAdminTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('www-admin')
        cls.section = SiteTeamSection.objects.get(slug='audio')

    def setUp(self):
        self.client.force_login(self.admin)

    def test_navigation_group_and_screens(self):
        """Działy i ustawienia są w grupie „Ustawienia” pod polskimi adresami; lista, edycja z inline podgrup
        i formularz ustawień otwierają się superuserowi; koordynator nie ma dostępu."""
        request = RequestFactory().get(reverse('admin:index'))
        request.user = self.admin
        groups = {group['app_label']: group for group in admin.site.get_app_list(request)}
        names = {model['object_name'].lower(): model['name'] for model in groups['settings']['models']}
        self.assertEqual(names['siteteamsection'], 'Strona WWW – działy ekipy')
        self.assertEqual(names['siteteamsettings'], 'Strona WWW – ustawienia')
        change = reverse('admin:core_siteteamsection_change', args=[self.section.pk])
        self.assertEqual(change, f'/panel/organizacja/strona-www-dzialy/{self.section.pk}/edytuj/')
        page = self.client.get(change)
        self.assertEqual(page.status_code, 200)
        content = page.content.decode()
        for expected in ('name="groups-TOTAL_FORMS"', 'Lektorzy', 'Teodor Laskowski',
                         'id="id_coordinator_roles" multiple class="selectfilter"',
                         'id="id_groups-0-roles" multiple class="selectfilter"', reverse('core:site_www')):
            with self.subTest(expected=expected):
                self.assertIn(expected, content)
        listing = self.client.get(reverse('admin:core_siteteamsection_changelist'))
        self.assertContains(listing, 'Lektorzy (ręcznie)')
        settings_page = self.client.get(reverse('admin:core_siteteamsettings_change', args=[1]))
        self.assertContains(settings_page, 'Dawid Wiktorski')
        self.assertEqual(self.client.get(reverse('admin:core_siteteamsettings_add')).status_code, 403)
        self.client.force_login(create_member('www-coordinator', roles.COORDINATOR))
        for url in (change, reverse('admin:core_siteteamsection_changelist')):
            with self.subTest(url=url):
                self.assertIn(self.client.get(url).status_code, (302, 403))

    def test_change_form_saves_section_and_new_group(self):
        """Zapis działu z nową ręczną podgrupą przez formularz admina; notka zostaje oczyszczona."""
        data = {
            'title': 'Dział audio', 'slug': 'audio', 'position': '8', 'is_enabled': 'on', 'show_count': 'on',
            'in_navigation': 'on', 'navigation_label': 'Audio',
            'coordinator_roles': [Role.objects.get(name=roles.AUDIOBOOK_COORDINATOR).pk],
            'note': '<p>Notka <b>x</b> <a href="https://x.pl/">y</a></p>',
            'groups-TOTAL_FORMS': '1', 'groups-INITIAL_FORMS': '0', 'groups-MIN_NUM_FORMS': '0',
            'groups-MAX_NUM_FORMS': '1000',
            'groups-0-title': 'Nowi', 'groups-0-position': '9', 'groups-0-source': 'manual',
            'groups-0-manual_names': 'Anna Nowa\nJan Nowy', 'groups-0-section': str(self.section.pk),
        }
        change = reverse('admin:core_siteteamsection_change', args=[self.section.pk])
        data['_edit_version'] = self.client.get(change).context['request'].edit_version_token
        response = self.client.post(change, data)
        self.assertEqual(response.status_code, 302)
        self.section.refresh_from_db()
        self.assertEqual(self.section.note, 'Notka x <a href="https://x.pl/">y</a>')
        self.assertEqual(self.section.groups.count(), 4)
        self.assertEqual(self.section.groups.last().manual_list, ['Anna Nowa', 'Jan Nowy'])

    def test_model_validation(self):
        """Ręczna podgrupa bez nazwisk nie przechodzi walidacji; rekord ustawień jest zawsze jeden (pk=1)."""
        group = SiteTeamGroup(section=self.section, source=SiteTeamGroup.Source.MANUAL, manual_names=' \n ')
        with self.assertRaises(ValidationError) as caught:
            group.full_clean()
        self.assertIn('manual_names', caught.exception.message_dict)
        group.manual_names = 'Ktoś'
        group.full_clean()
        self.assertEqual(SiteTeamSettings.load().pk, 1)
        self.assertEqual(SiteTeamSettings.objects.count(), 1)
        self.assertEqual(self.section.anchor, 'ekipa-audio')
