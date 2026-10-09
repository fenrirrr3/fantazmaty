"""Edit policies live on the views; the middleware no longer keeps URL-name lists."""
from django.test import SimpleTestCase
from django.urls import get_resolver, URLResolver

from core.edit_policy import policy_for


def core_patterns():
    def walk(resolver, namespace=None):
        for pattern in resolver.url_patterns:
            if isinstance(pattern, URLResolver):
                yield from walk(pattern, pattern.namespace or namespace)
            else:
                yield namespace, pattern
    return {pattern.name: pattern for namespace, pattern in walk(get_resolver()) if namespace == 'core'}


class EditPolicyTests(SimpleTestCase):
    # Forms whose POST must carry a current _edit_version token.
    VERSIONED = {
        'update_text_tags', 'update_text_audiobook', 'cancel_workflow_repetition',
        'handoff_workflow_stage', 'link_text_review', 'set_text_authors', 'set_translators',
        'update_coordinator_note', 'update_text_content_warnings', 'edit_text_note',
        'delete_text_note', 'update_text_file', 'update_review_file',
        'update_review_content_warnings', 'update_author_notification', 'update_review_status',
        'assigned_review_detail',
    }
    # Views whose object-level permission is checked before the edit lock is taken.
    OBJECT_CHECKS = {
        'anthology_detail', 'add_text_note', 'update_text_content_warnings', 'edit_text_note',
        'delete_text_note', 'assigned_review_detail', 'update_review_content_warnings',
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
