"""Scalanie autorów (polecenie merge_bisiecka_author): podgląd, przeniesienie relacji, historii i przerwanie przy niejednoznaczności."""
import json
from io import StringIO
from django.core.management import call_command, CommandError
from django.test import TestCase
from authors.models import Author, AuthorNote
from people.models import Person
from texts.models import Anthology, Extract, ForeignAuthor, Review, Text


class AuthorMergeTests(TestCase):
    def setUp(self):
        self.target = Author.objects.create(
            first_name="Agata", last_name="Bisiecka", email="agata@example.test"
        )
        self.source = Author.objects.create(
            first_name="Wiktor",
            last_name="Orłowski",
            email="wiktor@example.test",
            has_contract=True,
            contact=False,
        )
        self.book = Anthology.objects.create(title="Antologia")
        self.text = Text.objects.create(title="Opowiadanie", anthology=self.book, length=100)
        self.text.authors.add(self.target, self.source)
        self.review = Review.objects.create(
            title="Zgłoszenie",
            anthology=self.book,
            author=self.source,
            length=100,
            status="accepted",
        )
        self.review.coauthors.add(self.target, self.source)
        AuthorNote.objects.create(author=self.source, content="Stara notatka")

    def run_merge(self, **kwargs):
        output = StringIO()
        call_command("merge_bisiecka_author", stdout=output, **kwargs)
        return json.loads(output.getvalue())

    def test_dry_run_apply_relations_history_and_idempotence(self):
        person = Person.objects.create(
            first_name="Agata", last_name="Bisiecka", author_profile=self.source
        )
        novel = Anthology.objects.create(title="Powieść", is_novel=True).novel
        novel.authors.add(self.source)
        original_date = self.review.created_at
        self.run_merge()
        self.assertTrue(Author.objects.filter(pk=self.source.pk).exists())
        self.target.refresh_from_db()
        self.assertEqual(self.target.pseudonym, "")
        result = self.run_merge(apply=True)
        self.target.refresh_from_db()
        person.refresh_from_db()
        self.review.refresh_from_db()
        self.assertEqual(self.target.pseudonym, "Wiktor Orłowski")
        self.assertEqual(self.target.email, "agata@example.test")
        self.assertFalse(self.target.contact)
        self.assertTrue(self.target.has_contract)
        self.assertEqual(person.author_profile_id, self.target.pk)
        self.assertEqual(self.review.author_id, self.target.pk)
        self.assertEqual(self.review.created_at, original_date)
        self.assertEqual(self.review.status, "accepted")
        self.assertFalse(self.review.coauthors.exists())
        self.assertEqual(list(self.text.authors.all()), [self.target])
        self.assertEqual(list(novel.authors.all()), [self.target])
        self.assertFalse(Author.objects.filter(pk=self.source.pk).exists())
        self.assertTrue(self.target.notes.filter(content="Stara notatka").exists())
        self.assertTrue(self.target.notes.filter(content__contains="wiktor@example.test").exists())
        self.assertTrue(result["warnings"])
        count = self.target.notes.count()
        self.run_merge(apply=True)
        self.assertEqual(self.target.notes.count(), count)

    def test_extracts_combine_without_loss_and_conflicting_decisions_rollback(self):
        first = Extract.objects.create(
            author=self.target,
            full_name="Agata",
            email=self.target.email,
            recruitment="Nabór",
            title="A",
            accepted_titles="A",
        )
        second = Extract.objects.create(
            author=self.source,
            full_name="Wiktor",
            email=self.source.email,
            recruitment="Nabór",
            title="A",
            rejected_titles="A",
        )
        with self.assertRaises(CommandError):
            self.run_merge(apply=True)
        self.target.refresh_from_db()
        self.assertEqual(self.target.pseudonym, "")
        self.assertEqual(Extract.objects.count(), 2)
        second.title = "B"
        second.rejected_titles = "B"
        second.save()
        self.run_merge(apply=True)
        first.refresh_from_db()
        self.assertEqual(Extract.objects.count(), 1)
        self.assertEqual(first.title, "A\nB")
        self.assertEqual(first.status, "mixed")

    def test_ambiguous_identity_and_two_person_profiles_abort(self):
        Person.objects.create(first_name="A", last_name="B", author_profile=self.source)
        Person.objects.create(first_name="C", last_name="D", author_profile=self.target)
        with self.assertRaises(CommandError):
            self.run_merge(apply=True)
        self.assertTrue(Author.objects.filter(pk=self.source.pk).exists())
        Author.objects.create(first_name="Agata", last_name="Bisiecka", email=None)
        with self.assertRaises(CommandError):
            self.run_merge(apply=True)

    def test_empty_email_is_transferred_and_foreign_legacy_link_retained(self):
        self.target.email = None
        self.target.save()
        foreign = ForeignAuthor.objects.create(
            first_name="Wiktor", last_name="Orłowski", legacy_author_id=self.source.pk
        )
        self.run_merge(apply=True)
        self.target.refresh_from_db()
        foreign.refresh_from_db()
        self.assertEqual(self.target.email, "wiktor@example.test")
        self.assertEqual(foreign.legacy_author_id, self.target.pk)
