"""Permissions, forms and serialized transitions for post-layout proofreading."""
import uuid

from django import forms
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from core.models import PostLayoutAssignment
from core.permissions import is_coordinator, require_post_layout
from texts.models import Anthology


def can_manage(user):
    return is_coordinator(user)


def eligible_proofreaders():
    return get_user_model().objects.filter(is_active=True, person_profile__is_active=True,
        person_profile__is_external=False, person_profile__roles__name__iexact='Korektor poskładowy').distinct().select_related('person_profile')


class ProofreaderField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        return str(getattr(obj, 'person_profile', None) or obj.get_full_name() or obj.username)


class AssignmentForm(forms.Form):
    anthology = forms.ModelChoiceField(label='Antologia', queryset=Anthology.objects.none())
    proofreader = ProofreaderField(label='Korektor poskładowy', queryset=get_user_model().objects.none())
    token = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['anthology'].queryset = Anthology.objects.filter(is_novel=False).exclude(status__in=('ready', 'abandoned')).order_by('title', 'pk')
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



@transaction.atomic
def create_assignment(*, user, anthology, proofreader, token, page_from=None, page_to=None):
    """Zakres stron jest opcjonalny – można go uzupełnić później w edycji wpisu."""
    require_post_layout(user)
    if not can_manage(user):
        raise PermissionDenied()
    anthology = Anthology.objects.select_for_update().get(pk=anthology.pk)
    from people.models import Person
    Person.objects.select_for_update().get(user_id=proofreader.pk)
    if anthology.is_novel or anthology.status in ('ready', 'abandoned') or not eligible_proofreaders().filter(pk=proofreader.pk).exists():
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


class AssignmentEditForm(forms.ModelForm):
    proofreader = ProofreaderField(label='Korektor poskładowy', queryset=get_user_model().objects.none())

    class Meta:
        model = PostLayoutAssignment
        fields = ('proofreader', 'page_from', 'page_to', 'assigned_start', 'work_start', 'completed_on')
        widgets = {field: forms.DateInput(attrs={'type': 'date'}, format='%Y-%m-%d')
            for field in ('assigned_start', 'work_start', 'completed_on')}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        eligible = eligible_proofreaders().values('pk')
        self.fields['proofreader'].queryset = get_user_model().objects.filter(
            Q(pk__in=eligible) | Q(pk=self.instance.proofreader_id)).select_related('person_profile')
        self.fields['work_start'].disabled = self.instance.status == 'assigned'
        self.fields['completed_on'].disabled = self.instance.status != 'completed'
        self.fields['work_start'].required = self.instance.status != 'assigned' and not self.instance.historical
        self.fields['completed_on'].required = self.instance.status == 'completed' and not self.instance.historical
        self.fields['assigned_start'].required = not self.instance.historical
        for name in ('page_from', 'page_to'):
            self.fields[name].required = False
            self.fields[name].help_text = 'Opcjonalne – podaj obie strony albo żadną.'
        self.fields['work_start'].help_text = 'To także data zakończenia etapu Przydzielony.'
        self.fields['completed_on'].help_text = 'To także data zakończenia etapu W trakcie.'

    def clean(self):
        data = super().clean()
        start, work, end = (data.get(f) for f in ('assigned_start', 'work_start', 'completed_on'))
        if start and work and work < start:
            self.add_error('work_start', 'Rozpoczęcie pracy nie może poprzedzać przydzielenia.')
        if start and end and end < start:
            self.add_error('completed_on', 'Zakończenie nie może poprzedzać przydzielenia.')
        if work and end and end < work:
            self.add_error('completed_on', 'Zakończenie nie może poprzedzać rozpoczęcia pracy.')
        first, last = data.get('page_from'), data.get('page_to')
        if (first is None) != (last is None):
            self.add_error('page_to' if first else 'page_from', 'Podaj obie strony zakresu albo żadną.')
        elif first and last and last < first:
            self.add_error('page_to', 'Koniec zakresu nie może być mniejszy od początku.')
        self.instance.assigned_end = work
        self.instance.work_end = end
        return data


@transaction.atomic
def edit_assignment(*, user, pk, version, data):
    from django.shortcuts import get_object_or_404
    if not can_manage(user):
        raise PermissionDenied()
    item = get_object_or_404(PostLayoutAssignment.objects.present().select_for_update(), pk=pk)
    if str(item.version) != str(version):
        raise StaleAssignment('Wpis zmienił się w innym oknie. Odśwież stronę.')
    form = AssignmentEditForm(data, instance=item)
    if form.is_valid():
        item = form.save(commit=False)
        item.version += 1
        item.full_clean()
        item.save()
    return form


def selection_token(user, item):
    return signing.dumps([user.pk, item.pk, item.version], salt='post-layout-bulk')


@transaction.atomic
def bulk_change(*, user, selected, operation, status=None, proofreader=None, confirm_delete=False):
    if not can_manage(user):
        raise PermissionDenied()
    if operation not in ('status', 'assign', 'delete', 'restore'):
        raise ValidationError('Wybierz operację zbiorczą.')
    if not selected or len(selected) > 500:
        raise ValidationError('Zaznacz od 1 do 500 wpisów.')
    versions = {}
    try:
        for token in selected:
            owner, pk, version = signing.loads(token, salt='post-layout-bulk', max_age=86400)
            if owner != user.pk:
                raise ValueError()
            versions[pk] = version
    except (signing.BadSignature, TypeError, ValueError):
        raise StaleAssignment('Zaznaczenie wygasło lub jest nieprawidłowe. Odśwież stronę.')
    items = list(PostLayoutAssignment.objects.select_for_update().filter(pk__in=versions).order_by('pk'))
    if len(items) != len(versions) or any(item.version != versions[item.pk] for item in items):
        raise StaleAssignment('Jeden z wpisów zmienił się lub został usunięty. Odśwież stronę; niczego nie zapisano.')
    if operation in ('delete', 'restore'):
        if operation == 'delete' and not confirm_delete:
            raise ValidationError('Potwierdź usunięcie zaznaczonych wpisów z listy.')
        now = timezone.now()
        for item in items:
            item.deleted_at, item.deleted_by = (now, user) if operation == 'delete' else (None, None)
            item.version += 1
            item.save(update_fields=['deleted_at', 'deleted_by', 'version'])
        return len(items)
    if any(item.deleted_at for item in items):
        raise ValidationError('Usunięte wpisy najpierw przywróć.')
    elif operation == 'assign':
        if not str(proofreader or '').isascii() or not str(proofreader or '').isdecimal():
            raise ValidationError('Wybierz korektora poskładowego.')
        person = eligible_proofreaders().filter(pk=int(proofreader)).first() if len(str(proofreader)) <= 18 else None
        if not person:
            raise ValidationError('Wybrana osoba nie jest aktywnym korektorem poskładowym.')
        for item in items:
            item.proofreader = person
            item.version += 1
            item.full_clean()
            item.save()
    else:
        if status not in PostLayoutAssignment.Status.values:
            raise ValidationError('Wybierz prawidłowy status.')
        for item in items:
            change_status(user=user, pk=item.pk, version=item.version, status=status)
    return len(items)


@transaction.atomic
def change_status(*, user, pk, status, version):
    require_post_layout(user)
    from django.shortcuts import get_object_or_404
    item = get_object_or_404(PostLayoutAssignment.objects.present().select_for_update(), pk=pk)
    if not can_manage(user) and item.proofreader_id != user.pk:
        raise PermissionDenied()
    if str(item.version) != str(version):
        raise StaleAssignment('Wpis zmienił się w innym oknie. Odśwież stronę.')
    if status == item.status:
        return item
    if status == item.previous_status:
        # Undo one step: the dates of the withdrawn step are cleared.
        if status == item.Status.ASSIGNED:
            item.assigned_end = item.work_start = None
        else:
            item.work_end = item.completed_on = None
    elif status == item.next_status:
        today = timezone.localdate()
        if status == item.Status.IN_PROGRESS:
            item.assigned_end = item.work_start = today
        else:
            item.work_end = item.completed_on = today
    elif item.historical:
        raise ValidationError('Historyczny wpis ze stopki zmienisz tylko w panelu admina.')
    else:
        raise ValidationError('Status zmienia się o jeden krok: Przydzielony ↔ W trakcie ↔ Zakończony.')
    item.status = status
    item.version += 1
    item.full_clean()
    item.save()
    return item
