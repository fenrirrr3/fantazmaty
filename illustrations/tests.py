from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from authors.models import Author
from people.models import Person, Role
from texts.models import Anthology, Review, Text

from .forms import CoverProposalForm
from .models import CoverProposal, Illustration
from .services import sync_required_illustrations


User = get_user_model()


class IllustrationTestDataMixin:
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()

        cls.member = User.objects.create_user(username="czlonek")
        cls.member_person = Person.objects.create(
            first_name="Jan",
            last_name="Zespolowy",
            email="czlonek@example.com",
            user=cls.member,
        )

        cls.coordinator = User.objects.create_user(username="koordynator")
        cls.coordinator_person = Person.objects.create(
            first_name="Ewa",
            last_name="Koordynujaca",
            email="koordynator@example.com",
            user=cls.coordinator,
            is_coordinator=True,
        )

        cls.superuser = User.objects.create_superuser(
            username="superadmin",
            email="superadmin@example.com",
            password="Testowe-haslo-123!",
        )

        cls.author = Author.objects.create(
            first_name="PoufneImieAutora",
            last_name="PoufneNazwiskoAutora",
            pseudonym="PoufnyPseudonimAutora",
            email="poufny.autor@example.com",
        )

        cls.anthology = Anthology.objects.create(
            title="Antologia A",
            status=Anthology.Status.UNPUBLISHED,
            has_illustrations=True,
        )
        cls.other_anthology = Anthology.objects.create(
            title="Antologia B",
            status=Anthology.Status.UNPUBLISHED,
            has_illustrations=True,
        )

        cls.text = Text.objects.create(
            title="Zimowe opowiadanie",
            length=25000,
            anthology=cls.anthology,
        )
        cls.text.authors.add(cls.author)

        cls.other_text = Text.objects.create(
            title="Zorza",
            length=20000,
            anthology=cls.other_anthology,
        )

        cls.illustration = Illustration.objects.get(text=cls.text)
        cls.other_illustration = Illustration.objects.get(
            text=cls.other_text
        )

        cls.review = Review.objects.create(
            author=cls.author,
            author_first_name=cls.author.first_name,
            author_last_name=cls.author.last_name,
            email=cls.author.email,
            title=cls.text.title,
            genre="fantasy",
            length=cls.text.length,
            anthology=cls.anthology,
            status=Review.Status.ACCEPTED,
            copied_text=cls.text,
        )

        cls.proposal = CoverProposal.objects.create(
            illustration_author="Autor ilustracji testowej",
            illustration_url="https://example.com/ilustracja",
            submitted_by=cls.member,
        )

    def illustration_url(self):
        return reverse("illustrations:illustration_list")

    def proposals_url(self):
        return reverse("illustrations:cover_proposal_list")

    def status_url(self, proposal=None):
        proposal = proposal or self.proposal
        return reverse(
            "illustrations:update_cover_proposal_status",
            args=[proposal.pk],
        )


class IllustrationModelTests(IllustrationTestDataMixin, TestCase):
    def test_qualified_text_automatically_receives_illustration(self):
        self.assertEqual(
            Illustration.objects.filter(text=self.text).count(),
            1,
        )
        self.assertEqual(
            self.illustration.status,
            Illustration.Status.UNASSIGNED,
        )

    def test_repeated_text_save_does_not_duplicate_illustration(self):
        self.text.save()
        self.text.save()

        self.assertEqual(
            Illustration.objects.filter(text=self.text).count(),
            1,
        )

    def test_enabling_illustrations_creates_missing_records(self):
        anthology = Anthology.objects.create(
            title="Antologia bez ilustracji",
            status=Anthology.Status.UNPUBLISHED,
            has_illustrations=False,
        )
        text = Text.objects.create(
            title="Tekst oczekujący",
            length=10000,
            anthology=anthology,
        )

        self.assertFalse(
            Illustration.objects.filter(text=text).exists()
        )

        anthology.has_illustrations = True
        anthology.save(update_fields=["has_illustrations"])

        self.assertTrue(
            Illustration.objects.filter(text=text).exists()
        )

    def test_explicit_synchronization_restores_missing_records_once(self):
        self.illustration.delete()

        created = sync_required_illustrations(anthology=self.anthology)
        repeated = sync_required_illustrations(anthology=self.anthology)

        self.assertEqual(created, 1)
        self.assertEqual(repeated, 0)
        self.assertEqual(
            Illustration.objects.filter(text=self.text).count(),
            1,
        )

    def test_nonqualifying_text_does_not_receive_illustration(self):
        for status, has_illustrations in (
            (Anthology.Status.PUBLISHED, True),
            (Anthology.Status.IN_PREPARATION, True),
            (Anthology.Status.UNPUBLISHED, False),
        ):
            with self.subTest(status=status, enabled=has_illustrations):
                anthology = Anthology.objects.create(
                    title=f"Antologia {status} {has_illustrations}",
                    status=status,
                    has_illustrations=has_illustrations,
                )
                text = Text.objects.create(
                    title="Tekst bez wymaganej ilustracji",
                    length=10000,
                    anthology=anthology,
                )

                self.assertFalse(
                    Illustration.objects.filter(text=text).exists()
                )

    def test_assigned_status_requires_illustrator(self):
        self.illustration.status = Illustration.Status.ASSIGNED

        with self.assertRaises(ValidationError) as error:
            self.illustration.full_clean()

        self.assertIn("illustrator", error.exception.message_dict)

    def test_selected_illustrator_requires_assigned_status(self):
        self.illustration.illustrator = self.member_person

        with self.assertRaises(ValidationError) as error:
            self.illustration.full_clean()

        self.assertIn("status", error.exception.message_dict)

    def test_assignment_sets_date_and_unassignment_clears_it(self):
        self.illustration.illustrator = self.member_person
        self.illustration.status = Illustration.Status.ASSIGNED
        self.illustration.save()

        self.assertEqual(
            self.illustration.assigned_at,
            timezone.localdate(),
        )

        self.illustration.illustrator = None
        self.illustration.status = Illustration.Status.UNASSIGNED
        self.illustration.save()
        self.illustration.refresh_from_db()

        self.assertIsNone(self.illustration.assigned_at)


@override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": (
                "django.contrib.staticfiles.storage.StaticFilesStorage"
            ),
        },
    },
)
class IllustrationViewTests(IllustrationTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        role, _ = Role.objects.get_or_create(name="Ilustrator")
        self.member_person.roles.add(role)
        self.client.force_login(self.member)

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()

        response = self.client.get(self.illustration_url())

        self.assertEqual(response.status_code, 302)
        self.assertIn("next=", response.url)

    def test_user_without_team_profile_is_denied(self):
        outsider = User.objects.create_user(username="spoza_zespolu")
        self.client.force_login(outsider)

        response = self.client.get(self.illustration_url())

        self.assertEqual(response.status_code, 403)

    def test_former_member_is_denied(self):
        Person.objects.filter(pk=self.member_person.pk).update(
            is_active=False
        )

        response = self.client.get(self.illustration_url())

        self.assertEqual(response.status_code, 403)

    def test_get_does_not_recreate_missing_illustration(self):
        self.illustration.delete()

        response = self.client.get(self.illustration_url())

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            Illustration.objects.filter(text=self.text).exists()
        )

    def test_list_rejects_post(self):
        response = self.client.post(self.illustration_url())

        self.assertEqual(response.status_code, 405)

    def test_author_identity_is_hidden_from_member_and_coordinator(self):
        for user in (self.member, self.coordinator):
            with self.subTest(user=user.username):
                self.client.force_login(user)

                response = self.client.get(self.illustration_url())

                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["can_view_authors"])

                for value in (
                    self.author.first_name,
                    self.author.last_name,
                    self.author.pseudonym,
                    self.author.email,
                ):
                    self.assertNotContains(response, value)

                for illustration in response.context["illustrations"]:
                    self.assertEqual(
                        list(illustration.text.authors.all()),
                        [],
                    )

    def test_superuser_can_view_author_identity(self):
        self.client.force_login(self.superuser)

        response = self.client.get(self.illustration_url())

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["can_view_authors"])
        self.assertContains(response, self.author.last_name)

    def test_response_disables_caching(self):
        response = self.client.get(self.illustration_url())

        self.assertIn("no-store", response["Cache-Control"])
        self.assertIn("private", response["Cache-Control"])

    def test_anthology_filter_limits_results(self):
        response = self.client.get(
            self.illustration_url(),
            {"anthology": self.anthology.pk},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item.pk for item in response.context["illustrations"]],
            [self.illustration.pk],
        )

    def test_status_filter_limits_results(self):
        self.other_illustration.illustrator = self.member_person
        self.other_illustration.status = Illustration.Status.DELIVERED
        self.other_illustration.save()

        response = self.client.get(
            self.illustration_url(),
            {"status": Illustration.Status.DELIVERED},
        )

        self.assertEqual(
            [item.pk for item in response.context["illustrations"]],
            [self.other_illustration.pk],
        )

    def test_published_anthology_is_excluded_without_deleting_record(self):
        self.anthology.status = Anthology.Status.PUBLISHED
        self.anthology.save(update_fields=["status"])

        response = self.client.get(self.illustration_url())

        self.assertNotIn(
            self.illustration.pk,
            [item.pk for item in response.context["illustrations"]],
        )
        self.assertTrue(
            Illustration.objects.filter(pk=self.illustration.pk).exists()
        )

    def test_invalid_filters_fall_back_safely(self):
        response = self.client.get(
            self.illustration_url(),
            {
                "anthology": "9" * 100,
                "status": "nieistniejacy",
                "sort": "text__authors__email",
                "page_size": "1000000",
                "page": "nie-liczba",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["selected_anthology_id"])
        self.assertEqual(response.context["selected_status"], "")
        self.assertEqual(response.context["selected_sort"], "anthology")
        self.assertEqual(
            response.context["page_obj"].selected_page_size,
            25,
        )

    def test_sorting_is_applied_before_pagination(self):
        Text.objects.bulk_create(
            [
                Text(
                    title=f"AAA {number:02d}",
                    length=10000,
                    anthology=self.anthology,
                )
                for number in range(30)
            ]
        )
        sync_required_illustrations(anthology=self.anthology)

        response = self.client.get(
            self.illustration_url(),
            {"sort": "title", "page_size": "25", "page": "1"},
        )

        self.assertEqual(
            [item.text.title for item in response.context["illustrations"]],
            [f"AAA {number:02d}" for number in range(25)],
        )

        response = self.client.get(
            self.illustration_url(),
            {"sort": "title", "page_size": "25", "page": "2"},
        )

        self.assertEqual(
            [item.text.title for item in response.context["illustrations"]],
            [
                "AAA 25",
                "AAA 26",
                "AAA 27",
                "AAA 28",
                "AAA 29",
                "Zimowe opowiadanie",
                "Zorza",
            ],
        )


class CoverProposalFormTests(TestCase):
    def test_invalid_url_is_rejected(self):
        form = CoverProposalForm(
            data={
                "illustration_author": "Autor ilustracji",
                "illustration_url": "javascript:alert(1)",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("illustration_url", form.errors)

    def test_missing_author_is_rejected(self):
        form = CoverProposalForm(
            data={
                "illustration_author": "",
                "illustration_url": "https://example.com/ilustracja",
            }
        )

        self.assertFalse(form.is_valid())
        self.assertIn("illustration_author", form.errors)


@override_settings(
    SECURE_SSL_REDIRECT=False,
    STORAGES={
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
        },
        "staticfiles": {
            "BACKEND": (
                "django.contrib.staticfiles.storage.StaticFilesStorage"
            ),
        },
    },
)
class CoverProposalViewTests(IllustrationTestDataMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.member)

    def test_member_can_submit_proposal(self):
        response = self.client.post(
            self.proposals_url(),
            {
                "illustration_author": "Nowy autor ilustracji",
                "illustration_url": "https://example.com/nowa",
            },
        )

        self.assertRedirects(
            response,
            self.proposals_url(),
            fetch_redirect_response=False,
        )

        proposal = CoverProposal.objects.get(
            illustration_url="https://example.com/nowa"
        )

        self.assertEqual(proposal.submitted_by_id, self.member.pk)
        self.assertEqual(proposal.status, CoverProposal.Status.PENDING)
        self.assertIsNone(proposal.status_changed_at)

    def test_submission_cannot_forge_status_or_submitter(self):
        response = self.client.post(
            self.proposals_url(),
            {
                "illustration_author": "Próba podmiany",
                "illustration_url": "https://example.com/podmiana",
                "submitted_by": self.superuser.pk,
                "status": CoverProposal.Status.APPROVED,
            },
        )

        self.assertEqual(response.status_code, 302)

        proposal = CoverProposal.objects.get(
            illustration_url="https://example.com/podmiana"
        )

        self.assertEqual(proposal.submitted_by_id, self.member.pk)
        self.assertEqual(proposal.status, CoverProposal.Status.PENDING)

    def test_invalid_submission_does_not_create_record(self):
        initial_count = CoverProposal.objects.count()

        response = self.client.post(
            self.proposals_url(),
            {
                "illustration_author": "Autor",
                "illustration_url": "niepoprawny-url",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "illustration_url",
            response.context["form"].errors,
        )
        self.assertEqual(
            CoverProposal.objects.count(),
            initial_count,
        )

    def test_former_member_cannot_submit(self):
        Person.objects.filter(pk=self.member_person.pk).update(
            is_active=False
        )
        initial_count = CoverProposal.objects.count()

        response = self.client.post(
            self.proposals_url(),
            {
                "illustration_author": "Autor",
                "illustration_url": "https://example.com/zablokowana",
            },
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            CoverProposal.objects.count(),
            initial_count,
        )

    def test_regular_member_cannot_change_status_by_direct_post(self):
        response = self.client.post(
            self.status_url(),
            {"status": CoverProposal.Status.APPROVED},
        )

        self.assertEqual(response.status_code, 403)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, CoverProposal.Status.PENDING)
        self.assertIsNone(self.proposal.status_changed_at)

    def test_coordinator_profile_flag_grants_status_management(self):
        self.assertFalse(
            self.coordinator.groups.filter(name="Koordynator").exists()
        )
        self.client.force_login(self.coordinator)

        response = self.client.post(
            self.status_url(),
            {"status": CoverProposal.Status.APPROVED},
        )

        self.assertEqual(response.status_code, 302)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, CoverProposal.Status.APPROVED)
        self.assertIsNotNone(self.proposal.status_changed_at)

    def test_coordinator_role_grants_status_management(self):
        role, _ = Role.objects.get_or_create(name="Koordynator")
        self.member_person.roles.add(role)

        response = self.client.post(
            self.status_url(),
            {"status": CoverProposal.Status.INQUIRY_SENT},
        )

        self.assertEqual(response.status_code, 302)

        self.proposal.refresh_from_db()
        self.assertEqual(
            self.proposal.status,
            CoverProposal.Status.INQUIRY_SENT,
        )

    def test_old_coordinator_group_does_not_restore_former_members_access(self):
        group, _ = Group.objects.get_or_create(name="Koordynator")
        self.member.groups.add(group)
        Person.objects.filter(pk=self.member_person.pk).update(
            is_active=False
        )

        response = self.client.post(
            self.status_url(),
            {"status": CoverProposal.Status.APPROVED},
        )

        self.assertEqual(response.status_code, 403)

    def test_superuser_without_person_profile_can_manage_status(self):
        self.client.force_login(self.superuser)

        response = self.client.post(
            self.status_url(),
            {"status": CoverProposal.Status.REJECTED},
        )

        self.assertEqual(response.status_code, 302)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, CoverProposal.Status.REJECTED)

    def test_status_change_requires_post(self):
        self.client.force_login(self.coordinator)

        response = self.client.get(self.status_url())

        self.assertEqual(response.status_code, 405)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, CoverProposal.Status.PENDING)

    def test_invalid_status_is_not_saved(self):
        self.client.force_login(self.coordinator)

        response = self.client.post(
            self.status_url(),
            {"status": "nieistniejacy-status"},
        )

        self.assertEqual(response.status_code, 302)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, CoverProposal.Status.PENDING)
        self.assertIsNone(self.proposal.status_changed_at)

    def test_repeated_status_does_not_change_timestamp(self):
        earlier = timezone.now() - timedelta(days=1)
        CoverProposal.objects.filter(pk=self.proposal.pk).update(
            status=CoverProposal.Status.APPROVED,
            status_changed_at=earlier,
        )
        self.client.force_login(self.coordinator)

        response = self.client.post(
            self.status_url(),
            {"status": CoverProposal.Status.APPROVED},
        )

        self.assertEqual(response.status_code, 302)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status_changed_at, earlier)

    def test_missing_proposal_returns_404(self):
        self.client.force_login(self.coordinator)
        missing_id = self.proposal.pk + 10000

        response = self.client.post(
            reverse(
                "illustrations:update_cover_proposal_status",
                args=[missing_id],
            ),
            {"status": CoverProposal.Status.APPROVED},
        )

        self.assertEqual(response.status_code, 404)

    def test_status_post_requires_csrf_token(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.coordinator)

        response = client.post(
            self.status_url(),
            {"status": CoverProposal.Status.APPROVED},
        )

        self.assertEqual(response.status_code, 403)

        self.proposal.refresh_from_db()
        self.assertEqual(self.proposal.status, CoverProposal.Status.PENDING)

    def test_management_controls_follow_permissions(self):
        for user, expected in (
            (self.member, False),
            (self.coordinator, True),
            (self.superuser, True),
        ):
            with self.subTest(user=user.username):
                self.client.force_login(user)

                response = self.client.get(self.proposals_url())

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    response.context["can_manage_proposals"],
                    expected,
                )

    def test_deleting_submitter_preserves_proposal(self):
        self.member.delete()
        self.proposal.refresh_from_db()

        self.assertIsNone(self.proposal.submitted_by_id)
        self.assertEqual(
            self.proposal.illustration_url,
            "https://example.com/ilustracja",
        )