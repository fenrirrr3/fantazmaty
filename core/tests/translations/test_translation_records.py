"""Rekordy tłumaczeń: przełączanie flagi antologii, przenoszenie autorów i unieważnianie wersji edycji."""
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from authors.models import Author
from core.edit_versions import version_of
from core.translation_scope import frontend_scope
from fantazmaty.testing import create_superuser
from illustrations.models import Illustration
from texts.models import Anthology, ForeignAuthor, Text, TextTranslation, Translator
from texts.novels import new_chapter


class TranslationFlagTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = create_superuser('tr-admin')
        cls.author = Author.objects.create(first_name='Jan', last_name='Próbny', email=None)
        cls.normal = Anthology.objects.create(title='Księga krajowa', has_illustrations=True)
        cls.regular = Text.objects.create(title='Tekst zwykły', length=100, anthology=cls.normal)
        cls.book = Anthology.objects.create(title='Przekłady', is_translated=True, has_illustrations=True)
        cls.foreign = ForeignAuthor.objects.create(first_name='Ursula', last_name='Zagraniczna')
        cls.translator = Translator.objects.create(first_name='Anna', last_name='Żak', language='angielski')
        cls.other = Translator.objects.create(first_name='Beata', last_name='Adamska')
        cls.text = Text.objects.create(title='Tekst zagraniczny', anthology=cls.book, length=250)
        cls.text.translation.foreign_authors.add(cls.foreign)
        cls.text.translation.translators.add(cls.translator)

    def test_auto_creation_toggle_moving_and_history_retention(self):
        self.assertEqual(TextTranslation.objects.count(), 1)
        self.assertFalse(Illustration.objects.filter(text=self.text).exists())
        self.normal.is_translated = True
        self.normal.save()
        row = TextTranslation.objects.get(text=self.regular)
        row.translators.add(self.other)
        self.normal.save()
        self.assertEqual(TextTranslation.objects.filter(text=self.regular).count(), 1)
        self.normal.is_translated = False
        self.normal.save()
        self.assertEqual(list(row.translators.all()), [self.other])
        self.assertIn(self.regular, frontend_scope(Text.objects))
        self.regular.anthology = self.book
        self.regular.save()
        self.assertEqual(TextTranslation.objects.get(text=self.regular).pk, row.pk)
        self.assertEqual(list(row.translators.all()), [self.other])

    def test_flagging_copies_author_and_roundtrip_restores_explicit_links(self):
        """Błąd: zwykła edycja antologii dodawała ponownie autora usuniętego po powrocie z tłumaczenia."""
        self.regular.authors.add(self.author)
        self.normal.is_translated = True
        self.normal.save()
        self.assertFalse(self.regular.authors.exists())
        profile = self.regular.translation.foreign_authors.get()
        self.assertEqual(profile.legacy_author_id, self.author.pk)
        self.assertEqual(profile.email, '')
        self.assertTrue(Author.objects.filter(pk=self.author.pk).exists())
        self.normal.save()
        self.assertEqual(self.regular.translation.foreign_authors.count(), 1)
        record_id = self.regular.translation.pk
        self.normal.is_translated = False
        self.normal.full_clean()
        self.normal.save(update_fields=['is_translated'])
        self.assertEqual(list(self.regular.authors.all()), [self.author])
        self.assertEqual(self.regular.translation.pk, record_id)
        self.assertEqual(self.regular.translation.foreign_authors.count(), 1)
        self.regular.authors.clear()
        self.normal.title = 'Nowy tytuł'
        self.normal.save()
        self.assertFalse(self.regular.authors.exists())

    def test_flag_off_maps_foreign_author_to_legacy_author_and_keeps_translators(self):
        ordinary = Author.objects.create(first_name=self.foreign.first_name, last_name=self.foreign.last_name,
                                         email=None)
        self.foreign.legacy_author_id = ordinary.pk
        self.foreign.save(update_fields=['legacy_author_id'])
        self.book.is_translated = False
        self.book.save()
        self.client.force_login(self.admin)
        self.assertNotContains(self.client.get(reverse('core:translation_list')), self.book.title)
        self.assertContains(self.client.get(reverse('core:text_list')), self.text.title)
        self.assertEqual(list(TextTranslation.objects.get(text=self.text).translators.all()), [self.translator])
        self.assertEqual(list(self.text.authors.all()), [ordinary])

    def test_foreign_author_without_mapping_blocks_reverse_conversion_without_partial_save(self):
        self.book.is_translated = False
        for action in (self.book.full_clean, self.book.save):
            with self.assertRaises(ValidationError):
                action()
        self.book.refresh_from_db()
        self.assertTrue(self.book.is_translated)
        self.assertEqual(self.text.translation.foreign_authors.get(), self.foreign)

    def test_novel_translation_rejected_before_authors_change(self):
        novel = Anthology.objects.create(title='Powieść', is_novel=True)
        novel.novel.authors.add(self.author)
        chapter = new_chapter(novel, novel.novel, chapter_number=1)
        novel.is_translated = True
        for action in (novel.full_clean, novel.save):
            with self.assertRaises(ValidationError):
                action()
        novel.refresh_from_db()
        self.assertFalse(novel.is_translated)
        self.assertEqual(chapter.authors.get(), self.author)
        with self.assertRaises(ValidationError):
            Anthology.objects.create(title='Niedozwolona', is_novel=True, is_translated=True)

    def test_profile_change_or_delete_invalidates_text_edit_version(self):
        old = version_of(self.text)
        self.translator.language = 'niemiecki'
        self.translator.save()
        changed = version_of(self.text)
        self.assertGreater(changed, old)
        self.translator.delete()
        self.assertGreater(version_of(self.text), changed)
