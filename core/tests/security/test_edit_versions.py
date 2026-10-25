"""Polityka edycji widoków i tokeny wersji: wersja rośnie przy zapisie, operacjach zbiorczych, zmianach relacji
i usuwaniu powiązanych osób."""
from contextlib import contextmanager
from unittest.mock import patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import IntegrityError, connection
from django.db.models import QuerySet
from django.test import SimpleTestCase, TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import URLResolver, get_resolver

from authors.models import Author, AuthorNote, BlacklistEntry
from core.audio_description_models import AudioDescription
from core.edit_policy import policy_for
from core.edit_versions import PARENTS, TRACKED, VersionedQuerySet, batched_bumps, bump, version_of
from core.models import EditRevision, MailboxConnection
from people.models import Person
from texts.models import Anthology, Review, ReviewAssignment, Text, TextNote, TextTranslation, Translator
from workflow.models import WorkflowStage


def core_patterns():
    def walk(resolver, namespace=None):
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                yield from walk(pattern, pattern.namespace or namespace)
            else:
                yield namespace, pattern
    return {pattern.name: pattern for namespace, pattern in walk(get_resolver()) if namespace == 'core'}


class EditPolicyTests(SimpleTestCase):
    """Polityki edycji są deklarowane przy widokach (middleware nie trzyma list nazw adresów)."""

    # Formularze, których POST musi nieść aktualny token _edit_version.
    VERSIONED = {
        'update_text_tags', 'update_text_audiobook', 'cancel_workflow_repetition',
        'handoff_workflow_stage', 'link_text_review', 'set_translators',
        'update_coordinator_note', 'update_text_content_warnings', 'edit_text_note',
        'delete_text_note', 'update_text_file', 'toggle_text_take_lock', 'update_review_file',
        'update_review_content_warnings', 'update_author_notification', 'update_review_status',
        'assigned_review_detail',
    }
    # Widoki, które sprawdzają uprawnienia do obiektu przed założeniem blokady edycji.
    OBJECT_CHECKS = {
        'anthology_detail', 'add_text_note', 'update_text_content_warnings', 'edit_text_note',
        'delete_text_note', 'assigned_review_detail', 'update_review_content_warnings',
        'update_text_tags', 'update_text_audiobook',
        'edit_vacation', 'end_vacation', 'cancel_vacation',
    }

    def test_versioned_forms_require_token(self):
        patterns = core_patterns()
        for name in sorted(self.VERSIONED):
            with self.subTest(name=name):
                policy = getattr(patterns[name].callback, 'edit_policy', None)
                self.assertIsNotNone(policy)
                self.assertTrue(policy.require_version)

    def test_object_level_checks_are_declared(self):
        patterns = core_patterns()
        for name in sorted(self.OBJECT_CHECKS):
            with self.subTest(name=name):
                policy = getattr(patterns[name].callback, 'edit_policy', None)
                self.assertIsNotNone(policy)
                self.assertIsNotNone(policy.check)

    def test_policy_lookup_tolerates_missing_match(self):
        self.assertIsNone(policy_for(None))


class VersionedModelTests(TestCase):
    def test_every_tracked_model_uses_versioned_queryset(self):
        # Modele auth należą do Django; ich nieliczne bezpośrednie aktualizacje podbijają wersję jawnie.
        exempt = {'auth.user', 'auth.group'}
        missing = []
        for label in sorted((TRACKED | set(PARENTS)) - exempt):
            queryset_class = apps.get_model(label)._default_manager._queryset_class
            if not issubclass(queryset_class, VersionedQuerySet):
                missing.append(label)
        self.assertEqual(missing, [])

    def test_blacklist_and_mailbox_are_versioned(self):
        """Regresja: zapis wpisu czarnej listy i skrzynki pocztowej nie zmieniał tokenu wersji."""
        for obj in (
            BlacklistEntry.objects.create(name='Osoba'),
            MailboxConnection.objects.create(name='Teksty', host='example.test', username='teksty'),
        ):
            before = version_of(obj)
            obj.save()
            self.assertGreater(version_of(obj), before)


class BulkWriteVersionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Antologia')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)

    def stage(self):
        return WorkflowStage.objects.create(
            text=self.text, workflow_cycle=self.text.current_workflow_cycle,
            stage_type=WorkflowStage.StageType.READY_FOR_EDITING, iteration=1)

    def test_queryset_update_bumps_record_and_parent(self):
        stage = self.stage()
        text_before, stage_before = version_of(self.text), version_of(stage)
        WorkflowStage.objects.filter(pk=stage.pk).update(is_released=False)
        self.assertGreater(version_of(self.text), text_before)
        self.assertGreater(version_of(stage), stage_before)

    def test_related_manager_update_bumps_parent(self):
        self.stage()
        before = version_of(self.text)
        self.text.workflow_stages.update(is_released=False)
        self.assertGreater(version_of(self.text), before)

    def test_empty_update_does_not_bump(self):
        before = version_of(self.text)
        WorkflowStage.objects.filter(text=self.text).update(is_released=False)
        self.assertEqual(version_of(self.text), before)

    def test_bulk_create_bumps_parent(self):
        before = version_of(self.text)
        TextTranslation.objects.bulk_create([TextTranslation(text=self.text)])
        self.assertGreater(version_of(self.text), before)

    def test_batched_bumps_write_each_record_once(self):
        before = version_of(self.text)
        with CaptureQueriesContext(connection) as queries:
            with batched_bumps():
                for _ in range(5):
                    self.text.save(update_fields=['title'])
        bumps = [q for q in queries.captured_queries
                 if 'core_editrevision' in q['sql'] and q['sql'].startswith('UPDATE')]
        self.assertEqual(len(bumps), 1)
        self.assertEqual(version_of(self.text), before + 1)


class RelatedChangeVersionTests(TestCase):
    """Zmiany wykonywane „od drugiej strony” relacji też unieważniają formularz rodzica."""

    @classmethod
    def setUpTestData(cls):
        cls.book = Anthology.objects.create(title='Antologia')
        cls.text = Text.objects.create(title='Tekst', length=100, anthology=cls.book)
        cls.other_text = Text.objects.create(title='Drugi tekst', length=100, anthology=cls.book)
        cls.author = Author.objects.create(first_name='Jan', last_name='Autor', email='jan@example.test')
        cls.other_author = Author.objects.create(first_name='Ewa', last_name='Autorka', email='ewa@example.test')

    @contextmanager
    def assertBumped(self, *objects):
        """Po bloku każdy obiekt ma wyższą wersję niż przed nim."""
        before = [version_of(obj) for obj in objects]
        yield
        for obj, version in zip(objects, before):
            self.assertGreater(version_of(obj), version, obj)

    def test_moving_a_text_note_bumps_both_texts(self):
        note = TextNote.objects.create(text=self.text, content='Notatka')
        with self.assertBumped(self.text, self.other_text):
            note.text = self.other_text
            note.save()

    def test_moving_an_author_note_bumps_the_previous_author(self):
        note = AuthorNote.objects.create(author=self.author, content='Notatka')
        with self.assertBumped(self.author, self.other_author):
            note.author = self.other_author
            note.save()

    def test_reverse_clear_and_remove_bump_records_on_the_other_side(self):
        self.text.authors.add(self.author)
        with self.assertBumped(self.author, self.text):
            self.author.texts.clear()
        translator = Translator.objects.create(first_name='Tom', last_name='Tłumacz')
        translation = TextTranslation.objects.create(text=self.text)
        translation.translators.add(translator)
        with self.assertBumped(translator, translation, self.text):
            translator.translations.remove(translation)

    def test_reverse_clear_of_audio_description_bumps_its_anthology(self):
        person = Person.objects.create(first_name='Kontrola', last_name='AD', email='ad@example.test')
        description, _ = AudioDescription.objects.get_or_create(anthology=self.book)
        description.controllers.add(person)
        with self.assertBumped(description, self.book):
            person.controlled_audio_descriptions.clear()

    def test_deleting_identities_bumps_their_texts_and_reviews(self):
        self.text.authors.add(self.other_author)
        review = Review.objects.create(title='Zgłoszenie', anthology=self.book, author=self.author,
                                       author_first_name='Jan', author_last_name='Autor', length=100)
        review.coauthors.add(self.other_author)
        with self.assertBumped(self.text, review):
            self.other_author.delete()
        reviewer = get_user_model().objects.create_user('recenzent', 'recenzent@example.test')
        ReviewAssignment.objects.create(review=review, user=reviewer, position=1)
        TextNote.objects.create(text=self.other_text, author=reviewer, content='Notatka')
        with self.assertBumped(review, self.other_text):
            reviewer.delete()

    def test_bump_without_primary_key_is_ignored(self):
        bump('texts.text', None, 'default')
        self.assertFalse(EditRevision.objects.filter(model_label='texts.text', object_id=None).exists())

    def test_bulk_update_bumps_each_record_once(self):
        before = version_of(self.text), version_of(self.other_text)
        self.text.title = 'Nowy tytuł'
        self.other_text.title = 'Inny tytuł'
        Text.objects.bulk_update([self.text, self.other_text], ['title'])
        self.assertEqual((version_of(self.text), version_of(self.other_text)), (before[0] + 1, before[1] + 1))

    def test_concurrently_created_version_rows_fall_back_to_single_bumps(self):
        """Gdy inna transakcja utworzy wiersz wersji w trakcie partii, podbicie nie ginie."""
        with patch.object(QuerySet, 'bulk_create', side_effect=IntegrityError), batched_bumps():
            text = Text.objects.create(title='W partii', length=10, anthology=self.book)
        self.assertEqual(version_of(text), 1)

    def test_permission_granted_from_permission_side_bumps_account_version(self):
        user = get_user_model().objects.create_user('uprawnienia')
        before = version_of(user)
        Permission.objects.get(codename='view_user').user_set.add(user)
        self.assertGreater(version_of(user), before)

