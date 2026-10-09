"""Badge palettes are rendered by the server for every workflow role and stage."""
from django.template import Context, Template
from django.test import SimpleTestCase

from core.palettes import STAGE_PALETTES, label_palette, role_palette, stage_palette
from workflow.catalog import workflow_role_choices
from workflow.models import WorkflowRoleAssignment, WorkflowStage


class PaletteTests(SimpleTestCase):
    def test_every_role_and_stage_has_a_palette(self):
        roles = [value for value, _ in WorkflowRoleAssignment.Role.choices]
        self.assertEqual([role for role in roles if not role_palette(role)], [])
        stages = [value for value, _ in WorkflowStage.StageType.choices]
        self.assertEqual([stage for stage in stages if stage not in STAGE_PALETTES], [])

    def test_team_role_names_follow_the_label_rules(self):
        cases = {'Koordynator redakcji': 'coordinator', 'Recenzent': 'reviewer', 'Redaktor': 'editing',
                 'Korektor': 'proofreading', 'Weryfikator': 'verification', 'Stylista': 'styling',
                 'Ilustrator': ''}
        for label, palette in cases.items():
            with self.subTest(label=label):
                self.assertEqual(label_palette(label), palette)

    def test_unknown_code_falls_back_to_the_label(self):
        self.assertEqual(role_palette('verifier_9', 'Weryfikator 9'), 'verification')
        self.assertEqual(stage_palette('unknown'), '')

    def test_filters_never_emit_an_empty_attribute(self):
        template = Template('{% load workspace_tags %}<b{{ code|role_palette_attr:label }}></b><i{{ "x"|label_palette_attr }}></i>')
        html = template.render(Context({'code': 'editor', 'label': 'Redaktor'}))
        self.assertEqual(html, '<b data-palette="editing"></b><i></i>')

    def test_role_choices_are_covered(self):
        self.assertEqual([role for role, _ in workflow_role_choices() if not role_palette(role)], [])
