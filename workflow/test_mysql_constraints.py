"""Regresje ograniczenia weryfikatorów; uruchamiane również na MySQL."""
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from texts.models import Text
from .models import WorkflowRoleAssignment as Assignment


class VerifierConstraintTests(TestCase):
    def setUp(self):
        self.text = Text.objects.create(title="Test ograniczenia", length=100)
        self.user = get_user_model().objects.create_user(username="verifier_constraint")

    def create(self, role, **kwargs):
        return Assignment.objects.create(
            text=self.text, role=role, assigned_to=self.user, **kwargs
        )

    def test_bulk_insert_cannot_assign_both_verifications(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment.objects.bulk_create([
                Assignment(text=self.text, role=role, assigned_to=self.user)
                for role in (Assignment.Role.VERIFIER_1, Assignment.Role.VERIFIER_2)
            ])

    def test_direct_update_cannot_bypass_constraint(self):
        self.create(Assignment.Role.VERIFIER_1)
        other = self.create(Assignment.Role.EDITOR)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Assignment.objects.filter(pk=other.pk).update(role=Assignment.Role.VERIFIER_2)

    def test_other_roles_cycles_and_texts_are_allowed(self):
        self.create(Assignment.Role.VERIFIER_1)
        self.create(Assignment.Role.EDITOR)
        self.create(Assignment.Role.PROOFREADER_1)
        self.create(Assignment.Role.VERIFIER_2, workflow_cycle=2)
        second = Text.objects.create(title="Inny tekst", length=100)
        Assignment.objects.create(text=second, role=Assignment.Role.VERIFIER_2, assigned_to=self.user)

    def test_unassigned_roles_and_deleted_user_are_allowed(self):
        self.create(Assignment.Role.VERIFIER_1)
        Assignment.objects.create(text=self.text, role=Assignment.Role.VERIFIER_2)
        self.user.delete()
        self.assertEqual(Assignment.objects.filter(text=self.text, assigned_to__isnull=True).count(), 2)
