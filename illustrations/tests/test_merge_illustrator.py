"""Komenda merge_illustrator: scalenie wpisu z pełnym imieniem i nazwiskiem z wpisem nazwanym
pseudonimem – podgląd bez zapisu, przeniesienie przypisań, idempotencja i przerwanie przy konflikcie."""
import json
from datetime import date
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from core.edit_versions import version_of
from illustrations.models import Illustration, Illustrator
from texts.models import Anthology, Text


class MergeIllustratorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.target = Illustrator.objects.create(first_name='Przemek', last_name='Świszcz',
                                                preferences='Fantasy', is_active=False)
        cls.source = Illustrator.objects.create(first_name='Graphos', email='graphos@example.test',
                                                portfolio='https://example.test/portfolio',
                                                preferences='Smoki', covers=True)
        cls.other = Illustrator.objects.create(first_name='Inna osoba')
        book = Anthology.objects.create(title='Antologia', has_illustrations=True)
        cls.rows = []
        for number, artists in enumerate(([cls.source, cls.other], [cls.target, cls.source])):
            text = Text.objects.create(title=f'Tekst {number}', anthology=book, length=10)
            row = Illustration.objects.get(text=text)
            row.assigned_at = date(2020, 2, 3)
            row.set_artists(artists, status='delivered', preserve_assignment_date=True)
            cls.rows.append(row)

    def merge(self, apply=False):
        out = StringIO()
        call_command('merge_illustrator', first_name='Przemek', last_name='Świszcz', pseudonym='Graphos',
                     apply=apply, stdout=out)
        return json.loads(out.getvalue())

    def test_dry_run_merge_shared_assignments_and_idempotence(self):
        before = list(Illustration.objects.order_by('pk').values())
        revisions = [version_of(row) for row in self.rows]
        self.merge()
        self.target.refresh_from_db()
        self.source.refresh_from_db()
        self.assertEqual(self.target.pseudonym, "")
        self.assertIsNone(self.target.email)
        self.assertEqual(self.source.email, "graphos@example.test")
        self.assertEqual([version_of(row) for row in self.rows], revisions)
        report = self.merge(apply=True)
        self.target.refresh_from_db()
        self.assertFalse(Illustrator.objects.filter(pk=self.source.pk).exists())
        self.assertEqual(self.target.display_name, "Graphos")
        self.assertEqual(self.target.email, "graphos@example.test")
        self.assertEqual(self.target.preferences, "Fantasy\n\nSmoki")
        self.assertTrue(self.target.covers)
        self.assertFalse(self.target.is_active)
        self.assertEqual(
            set(self.rows[0].illustrators.values_list("pk", flat=True)),
            {self.target.pk, self.other.pk},
        )
        self.assertEqual(
            list(self.rows[1].illustrators.values_list("pk", flat=True)), [self.target.pk]
        )
        self.assertEqual(list(Illustration.objects.order_by("pk").values()), before)
        self.assertTrue(all(version_of(row) > old for row, old in zip(self.rows, revisions)))
        self.assertEqual(report["merged_id"], self.source.pk)
        repeat = self.merge(apply=True)
        self.assertIsNone(repeat["merged_id"])
        self.assertEqual(repeat["before"][0], repeat["after"])

    def test_conflicting_contact_and_ambiguous_name_abort_without_changes(self):
        self.target.email = "different@example.test"
        self.target.save()
        with self.assertRaisesMessage(CommandError, "email"):
            self.merge(apply=True)
        self.target.refresh_from_db()
        self.assertEqual(self.target.pseudonym, "")
        self.assertTrue(self.rows[0].illustrators.filter(pk=self.source.pk).exists())
        self.target.email = None
        self.target.save()
        Illustrator.objects.create(first_name="Graphos")
        with self.assertRaisesMessage(CommandError, "kilka rekordów"):
            self.merge(apply=True)
        self.assertTrue(Illustrator.objects.filter(pk=self.source.pk).exists())
