"""Lock the edited aggregate and compare the version displayed in its HTML form."""
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
    from texts.models import Review, Text
    from workflow.models import WorkflowStage
    from people.models import Vacation
    from core.models import AnthologyCorrection
    kwargs = match.kwargs
    admin_model = getattr(getattr(match.func, "model_admin", None), "model", None)
    object_id = kwargs.get("object_id", "")
    if match.namespace == "admin" and admin_model and str(object_id).isdecimal() and len(str(object_id)) < 19:
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
        model, pk = aggregate(match)
        with transaction.atomic():
            if model and model._meta.label_lower == "people.vacation":
                from people.models import Person
                person_id = model.objects.filter(pk=pk).values_list("person_id", flat=True).first()
                Person.objects.select_for_update().filter(pk=person_id).first()
            obj = model.objects.select_for_update().filter(pk=pk).first() if model and pk else None
            current = fingerprint(obj) if obj else None
            key = f"{model._meta.label_lower}:{pk}" if obj else ""
            token = request.POST.get("_edit_version") if request.method == "POST" else None
            if token and obj:
                try:
                    expected = signing.loads(token, salt="cms-edit-version", max_age=86400)
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
