"""Sample mailbox, safe preview, attachments and recruitment register."""
from copy import deepcopy

from django import forms
from django.contrib import messages as notifications
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Q, Value, CharField
from django.db.models.functions import Coalesce, Concat, NullIf, Trim
from django.http import FileResponse, JsonResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.mailbox_date import MailboxDateForm, DEFAULT_SENT_SINCE
from core.models import Recruitment, RecruitmentMailSource, RecruitmentRoleDecision
from core.permissions import can_copy_emails, recruitment_mailbox_required
from core.recruitment_roles import ROLE_CHOICES
from core.services.mailbox import MailboxError, read_headers
from core.services.mailbox_import import fetch_messages, mailbox_key, receipts
from core.services.mailbox_snapshots import imported_uids, recruitment_connection
from core.services.recruitment_samples import MAX_SAMPLES, attachment_archive, read_samples, store_samples
from core.views.mailbox_common import copied_emails, load_signed, preview_data, signed, stored_headers

SALT = 'recruitment-mailbox-v55'
SESSION_KEY = 'recruitment_sample_headers_v55'
MESSAGE_ACTIONS = ('preview', 'preview_html', 'download', 'add', 'bulk', 'set_notified')


class RecruitmentMailboxForm(forms.Form):
    roles = forms.MultipleChoiceField(label='Rola', choices=(('all', 'Wszystkie'), *ROLE_CHOICES),
        initial=['all'], required=False, widget=forms.CheckboxSelectMultiple,
        help_text='Zaznacz „Wszystkie” albo jedną lub kilka ról. Brak zaznaczenia oznacza Wszystkie.')
    show_downloaded = forms.BooleanField(label='Pokaż również zgłoszenia już pobrane lub dodane do bazy', required=False)

    def clean_roles(self):
        chosen = self.cleaned_data['roles']
        return [] if not chosen or 'all' in chosen else [key for key, _ in ROLE_CHOICES if key in chosen]


class RecruitmentBulkForm(forms.Form):
    decision = forms.ChoiceField(required=False, choices=(('', 'Bez zmiany'), ('yes', 'Przyjęty'), ('no', 'Odrzucony'), ('clear', 'Bez decyzji')))
    notified = forms.ChoiceField(required=False, choices=(('', 'Bez zmiany'), ('yes', 'Tak'), ('no', 'Nie')))

    def clean(self):
        data = super().clean()
        if not data.get('decision') and not data.get('notified'):
            raise forms.ValidationError('Wybierz zmianę decyzji lub pola Powiadomiony.')
        return data


def recruitment_register(request):
    # Wrapped by intake.recruitment_list's coordinator permission check.
    from core.pagination import paginate_items
    query = request.GET.get('q', '').strip()[:500]
    from core.selectors.recruitment import filter_role, role_choices
    choices = role_choices()
    role = request.GET.get('role', '')
    status = request.GET.get('status', '')
    role = role if role in dict(choices) else ''
    status = status if status in Recruitment.Status.values else ''
    records = Recruitment.objects.prefetch_related('role_decisions').annotate(_candidate_sort=Coalesce(
        NullIf(Trim(Concat('first_name', Value(' '), 'last_name')), Value('')),
        NullIf('applicant_name', Value('')), 'mail_sender', output_field=CharField()))
    if status and not role:
        records = records.filter(status=status)
    for term in query.split():
        records = records.filter(Q(first_name__plcontains=term) | Q(last_name__plcontains=term) |
            Q(applicant_name__plcontains=term) | Q(email__plcontains=term) | Q(mail_sender__plcontains=term) | Q(mail_subject__plcontains=term))
    if role:
        records = filter_role(records, role, status=status)
    page = paginate_items(request, records)
    return render(request, 'core/recruitment_register.html', {'items': page, 'page_obj': page, 'query': query,
        'role': role, 'status': status, 'role_choices': choices,
        'status_choices': [*RecruitmentRoleDecision.status.field.choices,
                           (Recruitment.Status.MIXED.value, Recruitment.Status.MIXED.label)]})


def _load(token, identity, kind):
    """Podpisany wybór albo kursor z bieżącymi filtrami tej strony."""
    return load_signed(token, SALT, {**identity, 'kind': kind}, 'Wybór wygasł. Pobierz nagłówki ponownie.',
                       'Zmieniono filtry lub skrzynkę. Pobierz nagłówki od początku.')


def _populate(context, config, identity, result):
    """Nagłówki na stronie: stan wiadomości (w bazie, pobrana) oraz podpisany wybór i kursory stron."""
    result = deepcopy(result)
    uids = [row['uid'] for row in result['rows']]
    known = {source.uid: source for source in RecruitmentMailSource.objects.filter(
        mailbox_key=identity['mailbox'], uid_validity=result['validity'], uid__in=uids).select_related('recruitment').prefetch_related('recruitment__role_decisions')}
    downloaded = set(receipts(config, result['validity']).filter(uid__in=uids).values_list('uid', flat=True))
    for row in result['rows']:
        source = known.get(row['uid'])
        row['downloaded'] = row['uid'] in downloaded
        row['record'] = source.recruitment if source else None
    context.update(result)
    context['selection'] = signed({**identity, 'kind': 'selection', 'validity': result['validity'], 'uids': uids}, SALT)
    for name in ('next_cursor', 'previous_cursor'):
        context[name] = signed({**identity, 'kind': 'cursor', 'cursor': result[name]}, SALT) if result[name] else ''


def _headers(request, context, config, identity, excluded):
    """Strona nagłówków (pierwsza albo wskazana kursorem) zapisana w sesji."""
    cursor = _load(request.POST['cursor'], identity, 'cursor')['cursor'] if request.POST.get('cursor') else None
    result = read_headers(config, cursor, excluded=excluded, recruitment_roles=identity['roles'],
                          sent_since=identity['sent_since'])
    request.session[SESSION_KEY] = {**identity, 'result': result, 'fetched_at': timezone.now().isoformat(),
        'date_filter': context['date_form'].snapshot() if request.method == 'POST'
        else {'date_filter_enabled': True, 'sent_since': identity['sent_since']}}
    if request.method == 'POST':
        return redirect(request.path)
    _populate(context, config, identity, result)
    return render(request, 'core/recruitment_mailbox.html', context)


def _selected_samples(request, action, context, config, selection):
    """Wybrane wiadomości pobrane ze skrzynki i sparsowane raz – po sprawdzeniu wyboru i formularza akcji."""
    try:
        chosen = [request.POST.get('uid') or request.POST.get('preview_uid')] if action in ('preview', 'preview_html') else request.POST.getlist('selected')
        selected = {int(value) for value in chosen}
    except (ValueError, TypeError):
        raise MailboxError('Nieprawidłowy wybór wiadomości.') from None
    if not 1 <= len(selected) <= MAX_SAMPLES or not selected.issubset(selection['uids']):
        raise MailboxError(f'Wybierz od 1 do {MAX_SAMPLES} wiadomości z pobranej listy.')
    context['selected'] = {value for value in selection['uids'] if str(value) in request.POST.getlist('selected')}
    if action == 'set_notified' and (len(selected) != 1 or request.POST.get('notified') not in ('yes', 'no')):
        raise MailboxError('Wybierz jedną wiadomość i status Tak lub Nie.')
    if action == 'bulk':
        context['bulk_form'] = RecruitmentBulkForm(request.POST)
        if not context['bulk_form'].is_valid():
            raise MailboxError('Wybierz poprawną zmianę decyzji lub pola Powiadomiony.')
    raw_messages = fetch_messages(config, selection['validity'], selected, raw_messages=True, max_messages=MAX_SAMPLES)
    if {row['uid'] for row in raw_messages} != selected:
        raise MailboxError('Nie pobrano wszystkich wybranych wiadomości. Niczego nie zapisano.')
    return read_samples(raw_messages)


def _preview(request, action, context, sample):
    """Treść wiadomości jako tekst: fragment strony dla skryptu albo cała strona (bez JS); bez zapisu."""
    preview = preview_data(sample['data'])
    if action == 'preview':
        return render(request, 'core/includes/recruitment_preview.html', {'preview': preview})
    context['preview'] = preview
    return render(request, 'core/recruitment_mailbox.html', context)


def _set_notified(request, record):
    """Flaga Powiadomiony jednej wiadomości (JSON); konflikt cofa cały zapis transakcji."""
    from core.views.recruitment_notified import save_notification, notification_response, NotificationConflict
    record = Recruitment.objects.select_for_update().get(pk=record.pk)
    try:
        save_notification(record, request.user, request.POST['notified'] == 'yes', '', new_mail=True)
    except NotificationConflict as error:
        transaction.set_rollback(True)
        return JsonResponse({'error': str(error)}, status=409)
    return JsonResponse(notification_response(record, request.user))


def _bulk_update(records, data):
    """Zmiana tylko wybranych pól (decyzja, Powiadomiony); notatki i inne dane zostają bez zmian."""
    from core.services.recruitment_decisions import set_all_decisions
    for record in Recruitment.objects.select_for_update().filter(pk__in=[record.pk for record in records]).order_by('pk'):
        fields = ['updated_at']
        if data['decision']:
            set_all_decisions(record, {'yes': 'accepted', 'no': 'rejected', 'clear': 'new'}[data['decision']])
        if data['notified']:
            record.notified = data['notified'] == 'yes'
            fields.append('notified')
        record.save(update_fields=fields)


def _save(request, action, context, config, validity, samples):
    """Zapis zgłoszeń z wybranych wiadomości (wszystkie albo żadne) i działanie przycisku; bez wysyłania e-maili."""
    archive, downloaded = None, set()
    try:
        if action == 'download':
            archive = attachment_archive(samples, downloaded_uids=downloaded)
        with transaction.atomic():
            records = store_samples(config, validity, samples, downloaded_uids=downloaded)
            if action == 'set_notified':
                return _set_notified(request, records[0])
            if action == 'bulk':
                _bulk_update(records, context['bulk_form'].cleaned_data)
        if archive is not None:
            response = FileResponse(archive, as_attachment=True, filename='probki-zalaczniki.zip', content_type='application/zip')
            archive = None  # ZIP należy już do odpowiedzi.
            response['X-Content-Type-Options'] = 'nosniff'
            return response
    finally:
        if archive is not None:
            archive.close()
    if action == 'download':
        notifications.success(request, 'Brak załączników w wybranych wiadomościach. Zgłoszenia dodano do bazy; nie utworzono archiwum.')
    else:
        notifications.success(request, f'Zapisano zgłoszenia w Rekrutacji: {len(samples)}.' if action == 'add' else f'Zapisano zmiany dla wiadomości: {len(samples)}. Nie wysyłano powiadomień e-mail.')
    return redirect(request.path)


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@recruitment_mailbox_required
def recruitment_mailbox(request):
    action = 'preview_html' if request.POST.get('preview_uid') else request.POST.get('action', 'headers')
    context = {'bulk_form': RecruitmentBulkForm(), 'max_samples': MAX_SAMPLES}
    try:
        config = recruitment_connection()
        key = mailbox_key(config)
        # Nocna migawka (domyślne filtry) zastępuje przy GET starsze nagłówki z sesji.
        saved, notice = stored_headers(request, SESSION_KEY, config, key, roles=[], show=False)
        context.update(notice)
        post = request.method == 'POST'
        form = RecruitmentMailboxForm(request.POST if post else None,
            initial={'roles': saved['roles'] or ['all'], 'show_downloaded': saved['show']} if saved else None)
        date_form = MailboxDateForm(request.POST if post else None,
            initial=saved.get('date_filter') if saved else None)
        context.update(form=form, date_form=date_form, mailbox=config)
        if post:
            if not form.is_valid():
                raise MailboxError('Popraw wybór ról.')
            if not date_form.is_valid():
                raise MailboxError('Popraw datę początkową.')
            roles, show = form.cleaned_data['roles'], form.cleaned_data['show_downloaded']
            sent_since = date_form.effective_date()
        else:
            roles, show = (saved['roles'], saved['show']) if saved else ([], False)
            sent_since = saved['sent_since'] if saved else DEFAULT_SENT_SINCE.isoformat()
        identity = {'user': request.user.pk, 'mailbox': key, 'roles': roles, 'show': show, 'sent_since': sent_since}
        if saved and saved['roles'] == roles and saved['show'] == show and saved['sent_since'] == sent_since:
            _populate(context, config, identity, saved['result'])
        if request.method == 'GET' and saved:
            return render(request, 'core/recruitment_mailbox.html', context)
        excluded = None if show else lambda validity: imported_uids(config, validity)
        if action == 'headers':
            return _headers(request, context, config, identity, excluded)
        selection = _load(request.POST.get('selection', ''), identity, 'selection')
        if action == 'copy_emails':
            if not can_copy_emails(request.user):
                raise PermissionDenied('Kopiowanie adresów jest dostępne dla koordynatorów i superusera.')
            return copied_emails(config, selection['validity'], excluded=excluded, recruitment_roles=roles,
                                 sent_since=sent_since)
        if action not in MESSAGE_ACTIONS:
            raise MailboxError('Nieznana operacja.')
        samples = _selected_samples(request, action, context, config, selection)
        if action in ('preview', 'preview_html'):
            return _preview(request, action, context, samples[0])
        return _save(request, action, context, config, selection['validity'], samples)
    except MailboxError as error:
        if action == 'set_notified':
            return JsonResponse({'error': str(error)}, status=400)
        if action == 'preview':
            return render(request, 'core/includes/recruitment_preview.html', {'preview_error': str(error)}, status=400)
        context.setdefault('form', RecruitmentMailboxForm())
        context['error'] = str(error)
        return render(request, 'core/recruitment_mailbox.html', context, status=400 if request.method == 'POST' else 200)
