"""Lock the edited aggregate and compare the version displayed in its HTML form."""
from contextlib import nullcontext
import re
from django.core import signing
from django.db import transaction
from django.shortcuts import render
from django.conf import settings
from django.urls import resolve

from core.edit_policy import policy_for
from core.request_match import resolve_request


def aggregate(match):
    from texts.models import Review, Text, TextNote, ReviewAssignment, Reviewers, Anthology, TextTranslation
    from workflow.models import WorkflowStage, WorkflowRoleAssignment
    from people.models import Vacation
    from authors.models import Author, AuthorNote
    from core.models import AnthologyCorrection, Audiobook
    kwargs = match.kwargs
    admin_model = getattr(getattr(match.func, "model_admin", None), "model", None)
    object_id = kwargs.get("object_id", "")
    if match.namespace == "admin" and admin_model and str(object_id).isdecimal() and len(str(object_id)) < 19:
        if admin_model is Audiobook:
            return Text, Audiobook.objects.filter(pk=int(object_id)).values_list('text_id', flat=True).first()
        if admin_model is TextTranslation:
            return Text, TextTranslation.objects.filter(pk=int(object_id)).values_list('text_id', flat=True).first()
        if admin_model is TextNote:
            text_id = TextNote.objects.filter(pk=int(object_id)).values_list('text_id', flat=True).first()
            return Text, text_id
        if admin_model is AuthorNote:
            author_id = AuthorNote.objects.filter(pk=int(object_id)).values_list('author_id', flat=True).first()
            return Author, author_id
        if admin_model in (ReviewAssignment, Reviewers):
            review_id = admin_model.objects.filter(pk=int(object_id)).values_list('review_id', flat=True).first()
            return Review, review_id
        if admin_model in (WorkflowStage, WorkflowRoleAssignment):
            text_id = admin_model.objects.filter(pk=int(object_id)).values_list('text_id', flat=True).first()
            return Text, text_id
        return admin_model, int(object_id)
    if "stage_id" in kwargs:
        text_id = WorkflowStage.objects.filter(pk=kwargs["stage_id"]).values_list("text_id", flat=True).first()
        return Text, text_id
    for key, model in (("anthology_id", Anthology), ("text_id", Text), ("review_id", Review), ("vacation_id", Vacation), ("correction_id", AnthologyCorrection)):
        if key in kwargs:
            return model, kwargs[key]
    return None, None


RECOVERABLE_FIELDS = frozenset({
    'tags',
    'notes', 'content', 'general_notes', 'content_warnings', 'coordinator_note',
    'opinion', 'fragment', 'problem', 'suggestion', 'file_url',
    'author_first_name', 'author_last_name', 'email', 'phone_number',
    'first_name', 'last_name', 'pseudonym', 'title', 'length', 'genre',
    'start_date', 'end_date', 'started_at', 'ended_at', 'reason',
    'unofficial_notes', 'cover_notes', 'previous_data',
})


def recover_submitted_values(data):
    """Allow known content fields, including formsets; never echo secrets/tokens."""
    result = []
    for name in data:
        field = name
        match = re.fullmatch(r'(?:[\w]+-)*[0-9]+-([\w]+)', name)
        if match:
            field = match[1]
        if field in RECOVERABLE_FIELDS:
            result.extend((name, value) for value in data.getlist(name))
    return result


def fingerprint(obj):
    # Kept as a small public helper for existing integrations/tests.
    from core.edit_versions import version_of
    return version_of(obj)


def conflict_values(obj, request):
    """A conflict page must respect the same field access as the normal page."""
    from core.permissions import can_manage_review_files, can_manage_reviews
    from texts.models import Review, ReviewAssignment
    if isinstance(obj, Review):
        fields = ['title', 'content_warnings']
        if can_manage_reviews(request.user):
            fields.append('coordinator_note')
        if can_manage_review_files(request.user):
            fields.append('file_url')
        values = [(name, str(getattr(obj, name, '') or '')) for name in fields]
        match = request.resolver_match or resolve(request.path_info)
        if match.url_name == 'assigned_review_detail':
            own = ReviewAssignment.objects.filter(review=obj, user=request.user).first()
            if own:
                values.extend([('opinion', own.get_opinion_display()), ('notes', own.notes)])
        return values
    return [(name, str(getattr(obj, name) or '')) for name in
            ('content_warnings', 'coordinator_note', 'file_url', 'title') if hasattr(obj, name)]


class EditingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        match = resolve_request(request)
        if match is None:
            return self.get_response(request)
        if match.namespace not in ("core", "illustrations", "admin"):
            return self.get_response(request)
        from core.permissions import is_team_member
        if not request.user.is_authenticated or not request.user.is_active:
            return self.get_response(request)
        if match.namespace != 'admin' and not is_team_member(request.user):
            return self.get_response(request)
        if request.method == 'POST':
            from django.middleware.csrf import CsrfViewMiddleware
            rejection = CsrfViewMiddleware(self.get_response).process_view(request, match.func, match.args, match.kwargs)
            if rejection is not None:
                return rejection
        if match.namespace == "admin":
            from django.contrib import admin
            if not admin.site.has_permission(request):
                return self.get_response(request)
        check = getattr(match.func, "permission_check", None)
        if check:
            from django.core.exceptions import PermissionDenied
            try:
                check(request.user)
            except PermissionDenied:
                return self.get_response(request)
        model, pk = aggregate(match)
        policy = policy_for(match)
        if request.method == "POST" and match.namespace != "admin" and model and pk and policy:
            probe = model.objects.filter(pk=pk).first()
            if probe and not policy.allows(request, probe, match.kwargs):
                # The view repeats the same check and answers with its own 403.
                return self.get_response(request)
        writing = request.method == "POST"
        with transaction.atomic() if writing else nullcontext():
            # A standalone note edit (including a move) takes the same parent
            # locks as the Text inline, in PK order and before locking the note.
            if writing and match.namespace == 'admin':
                from texts.models import Anthology, NovelProfile, Text, TextNote
                model_admin = getattr(match.func, 'model_admin', None)
                if getattr(model_admin, 'model', None) is NovelProfile and pk:
                    book_id = NovelProfile.objects.filter(pk=pk).values_list('anthology_id', flat=True).first()
                    list(Text.objects.select_for_update().filter(anthology_id=book_id).order_by('pk'))
                    list(Anthology.objects.select_for_update().filter(pk=book_id))
                if getattr(model_admin, 'model', None) is TextNote:
                    parent_ids = {pk} if model is Text and pk else set()
                    raw_target = request.POST.get('text', '')
                    if raw_target.isascii() and raw_target.isdecimal() and len(raw_target) < 19:
                        parent_ids.add(int(raw_target))
                    list(Text.objects.select_for_update().filter(pk__in=parent_ids).order_by('pk'))
                    if model is Text and aggregate(match) != (model, pk):
                        return render(request, 'core/edit_conflict.html', {
                            'submitted_values': recover_submitted_values(request.POST),
                        }, status=409)
            if writing and model and model._meta.label_lower == 'people.person':
                from django.contrib.auth import get_user_model
                user_id = model.objects.filter(pk=pk).values_list('user_id', flat=True).first()
                get_user_model().objects.select_for_update().filter(pk=user_id).first()
            if writing and model and model._meta.label_lower == "people.vacation":
                from people.models import Person
                person_id = model.objects.filter(pk=pk).values_list("person_id", flat=True).first()
                Person.objects.select_for_update().filter(pk=person_id).first()
            objects = (model.objects.select_for_update() if writing else model.objects) if model else None
            if model and model._meta.label_lower == 'texts.review' and match.namespace != 'admin':
                # A conflict must never expose an object that the normal view hides.
                objects = objects.accessible_to(request.user)
            obj = objects.filter(pk=pk).first() if model and pk else None
            if request.method == 'POST' and obj:
                if match.namespace == 'admin':
                    model_admin = getattr(match.func, 'model_admin', None)
                    if model_admin and str(match.kwargs.get('object_id', '')).isdecimal():
                        target = model_admin.model.objects.filter(pk=match.kwargs['object_id']).first()
                        permission = model_admin.has_delete_permission if match.url_name.endswith('_delete') else model_admin.has_change_permission
                        if not permission(request, target):
                            return self.get_response(request)
            current = fingerprint(obj) if obj else None
            key = f"{model._meta.label_lower}:{pk}" if obj else ""
            token = request.POST.get("_edit_version") if request.method == "POST" else None
            required = match.namespace == 'admin' or bool(policy and policy.require_version)
            if request.method == 'POST' and obj and request.user.is_authenticated and request.user.is_active and (token or required):
                try:
                    expected = signing.loads(token or "", salt="cms-edit-version", max_age=settings.SESSION_COOKIE_AGE)
                except signing.BadSignature:
                    expected = None
                if expected != [request.user.pk, key, current]:
                    return render(request, "core/edit_conflict.html", {
                        "submitted_values": recover_submitted_values(request.POST),
                        "current_values": conflict_values(obj, request),
                        "retry_url": request.path,
                    }, status=409)
            if obj:
                request.edit_version_token = signing.dumps([request.user.pk, key, current], salt="cms-edit-version")
            return self.get_response(request)
