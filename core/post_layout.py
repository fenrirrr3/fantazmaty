"""Permissions, forms and serialized transitions for post-layout proofreading."""
import uuid

from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import PostLayoutAssignment
from core.permissions import has_role, is_superuser, require_post_layout
from texts.models import Anthology


def can_manage(user):
    return is_superuser(user) or has_role(user, 'Koordynator korekty')


def eligible_proofreaders():
    return get_user_model().objects.filter(is_active=True, person_profile__is_active=True,
        person_profile__is_external=False, person_profile__roles__name__iexact='Korektor poskładowy').distinct().select_related('person_profile')


class ProofreaderField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return str(obj.person_profile)


class AssignmentForm(forms.Form):
    anthology = forms.ModelChoiceField(label='Antologia', queryset=Anthology.objects.none())
    proofreader = ProofreaderField(label='Korektor poskładowy', queryset=get_user_model().objects.none())
    page_from = forms.IntegerField(label='Strona od', min_value=1, max_value=2147483647)
    page_to = forms.IntegerField(label='Strona do', min_value=1, max_value=2147483647)
    token = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['anthology'].queryset = Anthology.objects.filter(is_novel=False).order_by('title', 'pk')
        self.fields['proofreader'].queryset = eligible_proofreaders().order_by('person_profile__last_name', 'person_profile__first_name', 'pk')
        self.initial['token'] = signing.dumps([user.pk, uuid.uuid4().hex], salt='post-layout-create')

    def clean_token(self):
        try:
            user_id, key = signing.loads(self.cleaned_data['token'], salt='post-layout-create', max_age=86400)
            if user_id != self.user.pk:
                raise ValueError()
            return uuid.UUID(key)
        except (signing.BadSignature, ValueError, TypeError):
            raise forms.ValidationError('Formularz wygasł. Odśwież stronę.')

    def clean(self):
        data = super().clean()
        if data.get('page_from') and data.get('page_to') and data['page_to'] < data['page_from']:
            self.add_error('page_to', 'Koniec zakresu nie może być mniejszy od początku.')
        return data


@transaction.atomic
def create_assignment(*, user, anthology, proofreader, page_from, page_to, token):
    require_post_layout(user)
    if not can_manage(user):
        raise PermissionDenied()
    anthology = Anthology.objects.select_for_update().get(pk=anthology.pk)
    from people.models import Person
    Person.objects.select_for_update().get(user_id=proofreader.pk)
    if anthology.is_novel or not eligible_proofreaders().filter(pk=proofreader.pk).exists():
        raise ValidationError('Antologia lub korektor nie są już dostępni. Odśwież stronę.')
    existing = PostLayoutAssignment.objects.filter(creation_key=token).first()
    if existing:
        if (existing.created_by_id, existing.anthology_id, existing.proofreader_id, existing.page_from, existing.page_to) != (
                user.pk, anthology.pk, proofreader.pk, page_from, page_to):
            raise ValidationError('Ten formularz został już zapisany z innymi danymi. Odśwież stronę.')
        return existing
    item = PostLayoutAssignment(anthology=anthology, proofreader=proofreader, page_from=page_from,
        page_to=page_to, created_by=user, creation_key=token)
    item.full_clean()
    item.save()
    return item


class StaleAssignment(ValidationError):
    pass


@transaction.atomic
def change_status(*, user, pk, status, version):
    require_post_layout(user)
    from django.shortcuts import get_object_or_404
    item = get_object_or_404(PostLayoutAssignment.objects.select_for_update(), pk=pk)
    if not can_manage(user) and item.proofreader_id != user.pk:
        raise PermissionDenied()
    if str(item.version) != str(version):
        raise StaleAssignment('Wpis zmienił się w innym oknie. Odśwież stronę.')
    if status == item.status:
        return item
    if status != item.next_status:
        raise ValidationError('Wybierz kolejny etap: Przydzielony → W trakcie → Zakończony.')
    today = timezone.localdate()
    if status == item.Status.IN_PROGRESS:
        item.assigned_end = item.work_start = today
    else:
        item.work_end = item.completed_on = today
    item.status = status
    item.version += 1
    item.full_clean()
    item.save()
    return item
