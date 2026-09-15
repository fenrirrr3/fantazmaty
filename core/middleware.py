"""Lock the edited aggregate and compare the version displayed in its HTML form."""
from contextlib import nullcontext
import hashlib
import json
import re
from html import escape
from django.core import signing
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.shortcuts import render
from django.urls import resolve, Resolver404


def aggregate(match):
    from texts.models import Review, Text, ReviewAssignment, Reviewers
    from workflow.models import WorkflowStage, WorkflowRoleAssignment
    from people.models import Vacation
    from core.models import AnthologyCorrection
    kwargs = match.kwargs
    admin_model = getattr(getattr(match.func, "model_admin", None), "model", None)
    object_id = kwargs.get("object_id", "")
    if match.namespace == "admin" and admin_model and str(object_id).isdecimal() and len(str(object_id)) < 19:
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
    for key, model in (("text_id", Text), ("review_id", Review), ("vacation_id", Vacation), ("correction_id", AnthologyCorrection)):
        if key in kwargs:
            return model, kwargs[key]
    return None, None


def fingerprint(obj):
    from texts.models import TextNote, ReviewAssignment
    from workflow.models import WorkflowStage, WorkflowRoleAssignment
    data = [obj.__class__.objects.filter(pk=obj.pk).values().first()]
    if obj._meta.label_lower == "texts.text":
        for model in (TextNote, WorkflowStage, WorkflowRoleAssignment):
            data.append(list(model.objects.filter(text_id=obj.pk).order_by("pk").values()))
    # Include every direct many-to-many relation (authors, coauthors, roles, groups).
    for field in obj._meta.many_to_many:
        data.append([field.name, list(getattr(obj, field.name).order_by('pk').values_list('pk', flat=True))])
    if obj._meta.label_lower == "texts.review":
        data.append(list(ReviewAssignment.objects.filter(review_id=obj.pk).order_by("pk").values()))
    return hashlib.sha256(json.dumps(data, sort_keys=True, cls=DjangoJSONEncoder).encode()).hexdigest()


class EditingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            match = resolve(request.path_info)
        except Resolver404:
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
        if request.method == "POST" and match.namespace == "core" and model and pk:
            from django.http import HttpResponseForbidden
            from core.permissions import is_coordinator, can_manage_vacation
            probe = model.objects.filter(pk=pk).first()
            name = match.url_name
            if probe and name in ('add_text_note', 'update_text_content_warnings'):
                allowed = is_coordinator(request.user) or probe.workflow_role_assignments.filter(workflow_cycle=probe.current_workflow_cycle, assigned_to=request.user).exists()
                if not allowed:
                    return HttpResponseForbidden('Brak dostępu do zmiany tego tekstu.')
            if probe and name in ('edit_text_note', 'delete_text_note'):
                from texts.models import TextNote
                note = TextNote.objects.filter(pk=match.kwargs.get('note_id'), text_id=pk).first()
                if note and not (note.author_id == request.user.pk or is_coordinator(request.user)):
                    return HttpResponseForbidden('Brak dostępu do tej notatki.')
            if probe and name in ('assigned_review_detail', 'update_review_content_warnings'):
                allowed = probe.assignments.filter(user=request.user).exists()
                if name == 'update_review_content_warnings':
                    allowed = allowed or is_coordinator(request.user)
                if not allowed:
                    return HttpResponseForbidden('Brak przydziału do tej recenzji.')
            if probe and model._meta.label_lower == 'people.vacation' and not can_manage_vacation(request.user, probe):
                return HttpResponseForbidden('Brak dostępu do tego urlopu.')
        writing = request.method == "POST"
        with transaction.atomic() if writing else nullcontext():
            if writing and model and model._meta.label_lower == 'people.person':
                from django.contrib.auth import get_user_model
                user_id = model.objects.filter(pk=pk).values_list('user_id', flat=True).first()
                get_user_model().objects.select_for_update().filter(pk=user_id).first()
            if writing and model and model._meta.label_lower == "people.vacation":
                from people.models import Person
                person_id = model.objects.filter(pk=pk).values_list("person_id", flat=True).first()
                Person.objects.select_for_update().filter(pk=person_id).first()
            objects = (model.objects.select_for_update() if writing else model.objects) if model else None
            obj = objects.filter(pk=pk).first() if model and pk else None
            if request.method == 'POST' and obj:
                if match.namespace == 'admin':
                    model_admin = getattr(match.func, 'model_admin', None)
                    if model_admin and str(match.kwargs.get('object_id', '')).isdecimal():
                        target = model_admin.model.objects.filter(pk=match.kwargs['object_id']).first()
                        permission = model_admin.has_delete_permission if match.url_name.endswith('_delete') else model_admin.has_change_permission
                        if not permission(request, target):
                            return self.get_response(request)
                else:
                    from core.permissions import is_coordinator
                    name = match.url_name
                    if name in ('set_text_authors', 'update_text_file') and not request.user.is_superuser:
                        return self.get_response(request)
                    if name in ('edit_text_note', 'delete_text_note'):
                        from texts.models import TextNote
                        note = TextNote.objects.filter(pk=match.kwargs.get('note_id'), text_id=pk).first()
                        if not note or not (note.author_id == request.user.pk or is_coordinator(request.user)):
                            return self.get_response(request)
                    if name in ('update_text_file', 'update_text_content_warnings', 'update_coordinator_note'):
                        allowed = is_coordinator(request.user)
                        if name != 'update_coordinator_note':
                            allowed = allowed or obj.workflow_role_assignments.filter(workflow_cycle=obj.current_workflow_cycle, assigned_to=request.user).exists()
                        if not allowed:
                            return self.get_response(request)
            current = fingerprint(obj) if obj else None
            key = f"{model._meta.label_lower}:{pk}" if obj else ""
            token = request.POST.get("_edit_version") if request.method == "POST" else None
            required = match.namespace == 'admin' or match.url_name in {
                'set_text_authors', 'update_coordinator_note', 'update_text_content_warnings',
                'edit_text_note', 'delete_text_note', 'update_text_file',
                'update_review_content_warnings', 'update_author_notification', 'update_review_status',
                'assigned_review_detail',
            }
            if request.method == 'POST' and obj and request.user.is_authenticated and request.user.is_active and (token or required):
                try:
                    expected = signing.loads(token or "", salt="cms-edit-version", max_age=86400)
                except signing.BadSignature:
                    expected = None
                if expected != [request.user.pk, key, current]:
                    return render(request, "core/edit_conflict.html", status=409)
            response = self.get_response(request)
            if obj and response.status_code in (200, 400) and "text/html" in response.get("Content-Type", "") and not response.streaming:
                version = signing.dumps([request.user.pk, key, current], salt="cms-edit-version")
                hidden = f'<input type="hidden" name="_edit_version" value="{escape(version, quote=True)}">'
                content = response.content.decode(response.charset)
                content = re.sub(r'(<form\b[^>]*method=["\']post["\'][^>]*>)', lambda m:m[0]+hidden, content, flags=re.I)
                response.content = content.encode(response.charset)
                if response.has_header("Content-Length"):
                    response["Content-Length"] = len(response.content)
            return response
