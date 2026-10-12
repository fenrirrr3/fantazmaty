"""Pomocniki testów metadanych tekstu: wspólny zestaw otagowanych tekstów, token formularza tagów
i recenzja źródłowa tekstu."""
from tempfile import TemporaryDirectory

from django.contrib.auth import get_user_model
from django.urls import reverse

from authors.models import Author
from fantazmaty.testing import create_member, create_superuser, form_value
from texts.models import Anthology, Review, Text
from workflow.models import WorkflowRoleAssignment

TAG_FORM = '//form[contains(@class,"text-tags-form")]/'


class TagFormMixin:
    """Wymaga atrybutu ``detail`` (adres strony tekstu z formularzem tagów)."""

    def tag_form_token(self):
        response = self.client.get(self.detail)
        self.assertEqual(response.status_code, 200)
        return form_value(response, form_xpath=TAG_FORM)

    def tag_form_token_for(self, user):
        """Token z formularza widzianego przez ``user``; przywraca wcześniejsze logowanie klienta."""
        from django.test import Client

        client = Client()
        client.force_login(user)
        response = client.get(self.detail)
        self.assertEqual(response.status_code, 200)
        return form_value(response, form_xpath=TAG_FORM)


class TaggedTextsFixture(TagFormMixin):
    """Trzy teksty (z dwoma autorami, z jednym, bez antologii) i konta wszystkich ról zespołu."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.admin = create_superuser("tags-admin")
        cls.member = create_member("tags-member", "Recenzent")
        cls.artist = create_member("tags-artist", "Ilustrator")
        cls.coordinator = create_member("tags-coordinator", "Koordynator redakcji")
        cls.outsider = get_user_model().objects.create_user(
            "tags-outsider", email="tags-outsider@example.test"
        )
        cls.book = Anthology.objects.create(title="Antologia A")
        cls.book2 = Anthology.objects.create(title="Antologia Z")
        cls.author = Author.objects.create(
            first_name="Anna", last_name="Żak", email="private-author@example.test"
        )
        cls.coauthor = Author.objects.create(
            first_name="Jan", last_name="Adamczyk", email="private-coauthor@example.test"
        )
        cls.text = Text.objects.create(
            title="Łąka", anthology=cls.book, length=100, tags="magia, podróż", genre="Fantasy"
        )
        cls.text.authors.add(cls.author, cls.coauthor)
        # Zwykły członek dostaje przydział: tagi i gatunek zmienia tylko osoba z przydziałem albo koordynator.
        WorkflowRoleAssignment.objects.create(text=cls.text, role="editor", assigned_to=cls.member)
        cls.other = Text.objects.create(title="Las", anthology=cls.book2, length=100)
        cls.other.authors.add(cls.author)
        cls.orphan = Text.objects.create(title="Zorza", length=100)
        cls.review = Review.objects.create(
            title="Zgłoszenie", anthology=cls.book, length=100, genre="Fantasy",
            email="secret-source@example.test", author_first_name="Ukryte",
            author_last_name="Zgłoszenie", copied_text=cls.text,
        )

    def setUp(self):
        super().setUp()
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        override = self.settings(ACTIVITY_SPOOL_DIR=tmp.name)
        override.enable()
        self.addCleanup(override.disable)
        self.client.force_login(self.member)
        self.list = reverse("core:tag_list")
        self.detail = reverse("core:assigned_text_detail", args=[self.text.pk])
        self.save = reverse("core:update_text_tags", args=[self.text.pk])


def source_review(text, genre="Fantasy"):
    """Recenzja, z której skopiowano tekst – ma własny gatunek, niezależny od gatunku tekstu."""
    return Review.objects.create(
        title=text.title,
        anthology=text.anthology,
        copied_text=text,
        length=100,
        genre=genre,
        author_first_name="Jan",
        author_last_name="Testowy",
    )
