"""Seed z migracji core 0060: działy strony „Ekipa” odtwarzające obecną stronę są w świeżej bazie."""
from django.test import TestCase

from core.site_models import SiteTeamGroup, SiteTeamSection, SiteTeamSettings
from people import roles


class SiteTeamSeedTests(TestCase):
    def test_sections_order_roles_and_manual_audio_names(self):
        """Osiem działów w kolejności strony, role grup i koordynacji, notka Ilustracji, ręczne nazwiska audio,
        etykieta „Audio” w nawigacji i rekord ustawień z domyślnymi osobami."""
        sections = {item.slug: item for item in SiteTeamSection.objects.prefetch_related('groups__roles', 'coordinator_roles')}
        self.assertEqual(list(sections), ['redakcja', 'korekta', 'korekta-poskladowa', 'weryfikacja', 'recenzje',
                                          'ilustracje', 'grafika-sklad', 'audio'])
        expected = {
            'redakcja': ('Redakcja', True, [roles.EDITING_COORDINATOR], [('', 'roles', [roles.EDITOR])]),
            'korekta': ('Korekta', True, [roles.PROOFREADING_COORDINATOR], [('', 'roles', [roles.PROOFREADER])]),
            'korekta-poskladowa': ('Korekta poskładowa', True, [], [('', 'roles', [roles.POST_LAYOUT_PROOFREADER])]),
            'weryfikacja': ('Weryfikacja', True, [roles.VERIFICATION_COORDINATOR], [('', 'roles', [roles.VERIFIER])]),
            'recenzje': ('Recenzje', True, [roles.REVIEW_COORDINATOR], [('', 'roles', [roles.REVIEWER])]),
            'ilustracje': ('Ilustracje', False, [roles.ILLUSTRATION_COORDINATOR], []),
            'grafika-sklad': ('Grafika i skład', False, [],
                              [('Grafika', 'roles', [roles.DESIGNER]), ('Skład', 'roles', [roles.TYPESETTER])]),
            'audio': ('Dział audio', False, [roles.AUDIOBOOK_COORDINATOR], [
                ('Lektorzy', 'manual', ['Aleksandra Czyżewska', 'Izabella Dobrowolska', 'Sebastian Filipowicz',
                                        'Rafał Flejter', 'Jakub Kloch', 'Paweł Roicki', 'M. Stella Sit',
                                        'Karolina Stachacz-Bartosik', 'Ela Zińczuk']),
                ('Dźwiękowcy', 'manual', ['Teodor Laskowski']),
                ('Korektorzy', 'manual', ['Jadwiga Glaza', 'Anna Koral', 'Agnieszka Nowak', 'Agnieszka Rzepecka',
                                          'Aneta Stawiszyńska-Marciniak', 'Katarzyna Twarduś'])]),
        }
        for slug, (title, show_count, coordinators, groups) in expected.items():
            with self.subTest(slug=slug):
                item = sections[slug]
                self.assertEqual((item.title, item.show_count, item.is_enabled, item.in_navigation),
                                 (title, show_count, True, True))
                self.assertEqual([role.name for role in item.coordinator_roles.all()], coordinators)
                self.assertEqual([(group.title, group.source,
                                   group.manual_list if group.source == SiteTeamGroup.Source.MANUAL
                                   else [role.name for role in group.roles.all()])
                                  for group in item.groups.all()], groups)
        self.assertIn('<a href="https://fantazmaty.pl/czytaj/antologie/">antologii</a>', sections['ilustracje'].note)
        self.assertEqual(sections['audio'].navigation_label, 'Audio')
        settings = SiteTeamSettings.objects.get(pk=1)
        self.assertEqual((settings.lead_editor, settings.site_person), ('Dawid Wiktorski', 'Dawid Wiktorski'))
