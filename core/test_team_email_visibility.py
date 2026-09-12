from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from people.models import Person, Role


class TeamEmailVisibilityTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.member = User.objects.create_user(username="member")
        self.coordinator = User.objects.create_user(username="coordinator")
        self.admin = User.objects.create_user(username="admin", is_superuser=True)
        for user in (self.member, self.coordinator):
            person = Person.objects.create(
                first_name=user.username, last_name="Viewer",
                email=f"{user.username}@example.com", user=user,
            )
            if user == self.coordinator:
                person.roles.add(Role.objects.create(name="Koordynator redakcji"))
        self.target = Person.objects.create(
            first_name="Target", last_name="Person",
            email="private-contact@example.com",
            dropbox_email="private-dropbox@example.com",
        )

    def test_list_and_detail_visibility(self):
        for user, email_allowed, dropbox_allowed in (
            (self.member, False, False),
            (self.coordinator, True, False),
            (self.admin, True, True),
        ):
            self.client.force_login(user)
            for url in (
                reverse("core:people_list"),
                reverse("core:person_detail", args=[self.target.pk]),
            ):
                with self.subTest(user=user.username, url=url):
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                    html = response.content.decode()
                    self.assertEqual(self.target.email in html, email_allowed)
                    self.assertEqual(self.target.dropbox_email in html, dropbox_allowed)

    def test_search_cannot_match_hidden_email(self):
        for user, email_allowed, dropbox_allowed in (
            (self.member, False, False),
            (self.coordinator, True, False),
            (self.admin, True, True),
        ):
            self.client.force_login(user)
            for query, expected in (
                ("private-contact", email_allowed),
                ("private-dropbox", dropbox_allowed),
                ("Target", True),
            ):
                with self.subTest(user=user.username, query=query):
                    response = self.client.get(reverse("core:people_list"), {"query": query})
                    self.assertEqual(response.status_code, 200)
                    ids = [person.pk for person in response.context["people"]]
                    self.assertEqual(self.target.pk in ids, expected)

    def test_staff_alone_does_not_grant_email_access(self):
        self.member.is_staff = True
        self.member.save(update_fields=["is_staff"])
        self.client.force_login(self.member)
        response = self.client.get(reverse("core:people_list"))
        self.assertNotContains(response, self.target.email)
        self.assertNotContains(response, self.target.dropbox_email)

    def test_empty_table_column_count(self):
        for user, columns in ((self.member, 3), (self.coordinator, 4), (self.admin, 5)):
            self.client.force_login(user)
            response = self.client.get(reverse("core:people_list"), {"query": "no-match-xyz"})
            self.assertContains(response, f'colspan="{columns}"')
