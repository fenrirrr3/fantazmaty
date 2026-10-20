"""Palety plakietek ról i etapów (także na stronach zespołu) oraz reguły arkuszy stylów: tabele,
plakietki, przyciski zewnętrzne i klasy odporne na blokery reklam."""
import re
from pathlib import Path

from django.conf import settings
from django.template import Context, Template
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from core.palettes import ROLE_PALETTES, STAGE_PALETTES, label_palette, role_palette, stage_palette
from fantazmaty.testing import create_member, create_superuser
from people import roles as team_roles
from people.models import Person, Role
from workflow.catalog import workflow_role_choices
from workflow.models import WorkflowRoleAssignment, WorkflowStage

STATIC = Path(settings.BASE_DIR) / 'core' / 'static' / 'core'


class PaletteTests(SimpleTestCase):
    def test_every_role_and_stage_has_a_palette(self):
        roles = [value for value, _ in WorkflowRoleAssignment.Role.choices]
        self.assertEqual([role for role in roles if not role_palette(role)], [])
        self.assertEqual([role for role, _ in workflow_role_choices() if not role_palette(role)], [])
        stages = [value for value, _ in WorkflowStage.StageType.choices]
        self.assertEqual([stage for stage in stages if stage not in STAGE_PALETTES], [])

    def test_team_role_names_follow_the_label_rules(self):
        cases = {'Koordynator redakcji': 'coordinator', 'Recenzent': 'reviewer', 'Redaktor': 'editing',
                 'Korektor': 'proofreading', 'Weryfikator': 'verification', 'Stylista': 'styling',
                 'Ilustrator': 'illustrator'}
        for label, palette in cases.items():
            with self.subTest(label=label):
                self.assertEqual(label_palette(label), palette)

    def test_unknown_code_falls_back_to_the_label(self):
        self.assertEqual(role_palette('verifier_9', 'Weryfikator 9'), 'verification')
        self.assertEqual(stage_palette('unknown'), '')

    def test_filters_never_emit_an_empty_attribute(self):
        template = Template('{% load workspace_tags %}<b{{ code|role_palette_attr:label }}></b>'
                            '<i{{ "x"|label_palette_attr }}></i>')
        rendered = template.render(Context({'code': 'editor', 'label': 'Redaktor'}))
        self.assertEqual(rendered, '<b data-palette="editing"></b><i></i>')


class TeamRolePaletteTests(TestCase):
    def test_new_role_palettes_are_distinct_and_shown_on_team_pages(self):
        roles = ['Prawa ręka', 'Ilustrator', 'Grafik', 'Lektor', 'Składacz', 'Dźwiękowiec']
        colors = [label_palette(role) for role in roles]
        self.assertEqual(len(set(colors)), 6)
        self.assertNotIn('', colors)
        existing = {label_palette(n) for n in ('Redaktor', 'Korektor', 'Weryfikator', 'Recenzent',
                                               'Koordynator korekty')}
        self.assertFalse(set(colors) & existing)
        member = create_member('v68reader', 'Korektor poskładowy')
        without_account = Person.objects.create(first_name='Jan', last_name='Bezkonta', is_active=False)
        for name in roles:
            role = Role.objects.get_or_create(name=name)[0]
            member.person_profile.roles.add(role)
            without_account.roles.add(role)
        self.client.force_login(create_superuser('v68admin'))
        for url in (reverse('core:people_list'),
                    reverse('core:person_detail', args=[member.person_profile.pk]),
                    reverse('core:person_detail', args=[without_account.pk])):
            page = self.client.get(url)
            for palette in colors:
                self.assertContains(page, f'data-palette="{palette}"')


    def test_social_media_role_has_a_unique_palette_in_both_themes(self):
        """Rola „Social Media” ma własną paletę: inną niż każda rola zespołu, rola workflow i etap,
        z tokenami w motywie jasnym i ciemnym; lista zespołu pokazuje ją przy osobie."""
        palette = label_palette(team_roles.SOCIAL_MEDIA)
        self.assertEqual(palette, 'social-media')
        others = {label_palette(name) for name in (*team_roles.STANDARD_ROLES, *team_roles.RETIRED_ROLES, 'Prawa ręka')
                  if name != team_roles.SOCIAL_MEDIA}
        self.assertNotIn(palette, others | set(ROLE_PALETTES.values()) | set(STAGE_PALETTES.values()))
        themes = (STATIC / 'themes.css').read_text(encoding='utf-8')
        self.assertIn('[data-palette="social-media"] { --palette-bg: var(--social-media-bg); '
                      '--palette-fg: var(--social-media-fg); }', themes)
        for theme in (themes.split(':root {', 1)[1], themes.split(':root[data-theme="dark"]', 1)[1]):
            block = theme.split('}', 1)[0]
            for token in ('--social-media-bg:', '--social-media-fg:'):
                self.assertIn(token, block)
        create_member('social-palette', team_roles.SOCIAL_MEDIA)
        self.client.force_login(create_superuser('social-palette-admin'))
        page = self.client.get(reverse('core:people_list'))
        self.assertContains(page, 'data-palette="social-media"')


    def test_emperor_role_has_a_distinct_readable_gothic_badge(self):
        """Rola honorowa „Imperator Ludzkości”: własna paleta (inna niż pozostałe role i etapy), złoty napis o kontraście
        ≥ 4,5:1 z oboma kolorami gradientu w motywie jasnym i ciemnym, krzyże ✠ po obu stronach dodawane CSS-em; etykieta
        z paletą na liście zespołu, w profilu osoby, w wyszukiwarce i na liście urlopów."""
        palette = label_palette(team_roles.EMPEROR)
        self.assertEqual(palette, 'emperor')
        others = {label_palette(name) for name in (*team_roles.STANDARD_ROLES, *team_roles.RETIRED_ROLES, 'Prawa ręka')
                  if name != team_roles.EMPEROR}
        self.assertNotIn(palette, others | set(ROLE_PALETTES.values()) | set(STAGE_PALETTES.values()))
        themes = (STATIC / 'themes.css').read_text(encoding='utf-8')
        self.assertIn('[data-palette="emperor"] { --palette-bg: var(--emperor-bg); --palette-fg: var(--emperor-fg); }',
                      themes)

        def luminance(color):
            channels = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
            r, g, b = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        for theme, block in (('jasny', themes.split(':root {', 1)[1]), ('ciemny', themes.split(':root[data-theme="dark"]', 1)[1])):
            tokens = dict(re.findall(r'--(emperor-[a-z-]+): (#[0-9a-f]{6})', block.split('}', 1)[0]))
            for background in ('emperor-bg', 'emperor-bg-end'):
                with self.subTest(theme=theme, background=background):
                    light, dark = sorted((luminance(tokens['emperor-fg']), luminance(tokens[background])), reverse=True)
                    self.assertGreaterEqual((light + 0.05) / (dark + 0.05), 4.5)
        components = (STATIC / 'components.css').read_text(encoding='utf-8')
        self.assertIn('linear-gradient(135deg, var(--emperor-bg), var(--emperor-bg-end))', components)
        self.assertIn('.role-badge[data-palette="emperor"]::before,\n.role-badge[data-palette="emperor"]::after {\n    content: "✠";',
                      components)
        member = create_member('emperor-badge', team_roles.EMPEROR)
        self.client.force_login(create_superuser('emperor-badge-admin'))
        for url in (reverse('core:people_list'), reverse('core:person_detail', args=[member.person_profile.pk]),
                    reverse('core:global_search') + '?q=emperor-badge'):
            with self.subTest(url=url):
                self.assertContains(self.client.get(url), f'<span class="role-badge" data-palette="emperor">{team_roles.EMPEROR}</span>',
                                    html=True)


class StylesheetTests(SimpleTestCase):
    def css(self, name):
        return (STATIC / name).read_text(encoding='utf-8')

    def test_tables_bleed_to_card_edges(self):
        css = self.css('components.css')
        self.assertIn('margin-inline: calc(-1 * var(--table-bleed, var(--card-padding)))', css)
        self.assertNotIn('.post-layout-table-wrap {', self.css('post-layout.css'))

    def test_person_state_and_role_badges_have_shared_spacing(self):
        """Odstępy etykiet przy nazwisku wynikają z reguł ogólnych, nie z poprawek pojedynczych stron."""
        css = self.css('components.css')
        for selector, declaration in (
                ('.person-name + :is(.stage-status, .status, .badge) {', 'margin-inline-start: var(--space-2);'),
                (':not(dt) + .roles-list {', 'margin-top: var(--space-1);')):
            with self.subTest(selector=selector):
                self.assertIn(declaration, css[css.index(selector):].split('}', 1)[0])
        head = self.css('pages.css').split('.search-result-head {', 1)[1].split('}', 1)[0]
        for declaration in ('display: flex;', 'flex-wrap: wrap;', 'justify-content: space-between;'):
            self.assertIn(declaration, head)

    def test_every_badge_has_a_border(self):
        css = self.css('components.css')
        self.assertIn('--badge-fg: var(--muted);\n    --badge-border: currentColor;', css)
        self.assertNotIn('--badge-border: transparent', css)

    def test_team_notes_use_theme_tokens_with_dark_variants(self):
        """Notatki zespołu: żółte tło z tokenów ostrzeżenia, ważne – czerwone; tokeny tła mają wariant ciemny."""
        css = self.css('components.css')
        rules = {
            '.text-notes-card .text-note {': ('var(--warning-light)', 'var(--warning-border)'),
            '.text-notes-card .text-note.is-important {': ('var(--danger-light)', 'var(--danger-border)'),
        }
        for selector, tokens in rules.items():
            with self.subTest(selector=selector):
                block = css[css.index(selector):].split('}', 1)[0]
                for token in tokens:
                    self.assertIn(token, block)
        dark = self.css('themes.css').split(':root[data-theme="dark"]', 1)[1].split('}', 1)[0]
        for token in ('--warning-light:', '--danger-light:', '--danger-border:'):
            self.assertIn(token, dark)

    def test_external_buttons_use_the_accent_colour(self):
        self.assertIn('.audiobook-public-button', self.css('components.css'))
        for theme in ('--accent:', '--accent-hover:'):
            self.assertGreaterEqual(self.css('themes.css').count(theme), 3)

    def test_no_ad_prefixed_classes(self):
        """Blokery reklam ukrywają elementy o klasach „ad-…” – audiodeskrypcja ich nie używa."""
        pattern = re.compile(r'(?<![\w-])ad-[a-z]')
        root = Path(settings.BASE_DIR) / 'core'
        for path in [*(root / 'templates').rglob('*.html'), *STATIC.glob('*.css')]:
            text = path.read_text(encoding='utf-8')
            if path.suffix == '.html':
                classes = ' '.join(re.findall(r'class="([^"]*)"', text))
            else:
                classes = ' '.join(re.findall(r'\.[\w-]+', text))
            self.assertIsNone(pattern.search(classes.replace('.', ' ')), path.name)
