"""Sample mailbox, safe preview, attachments and recruitment register."""
from copy import deepcopy

from django import forms
from django.contrib import messages as notifications
from django.contrib.auth.decorators import login_required
from django.core import signing
from django.db import transaction
from django.db.models import Q, Value, CharField
from django.db.models.functions import Coalesce, Concat, NullIf, Trim
from django.http import FileResponse, JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from core.mailbox_date import MailboxDateForm, DEFAULT_SENT_SINCE
from core.models import MailboxConnection, Recruitment, RecruitmentMailSource
from core.permissions import recruitment_mailbox_required, require_superuser
from core.recruitment_roles import ROLE_CHOICES
from core.services.mailbox import MailboxError, read_headers
from core.services.mailbox_import import fetch_messages, mailbox_key, receipts
from core.services.recruitment_samples import MAX_SAMPLES, parse_sample, store_samples, attachment_archive

SALT = 'recruitment-mailbox'
SESSION_KEY = 'recruitment_sample_headers'


class RecruitmentMailboxForm(forms.Form):
    roles = forms.MultipleChoiceField(label='Rola', choices=(('all', 'Wszystkie'), *ROLE_CHOICES),
        initial=['all'], required=False, widget=forms.SelectMultiple(attrs={'size': 4}), help_text='Wszystkie lub kilka wybranych ról. Pusty wybór oznacza Wszystkie.')
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


def recruitment_connection():
    boxes = list(MailboxConnection.objects.filter(purpose=MailboxConnection.Purpose.RECRUITMENT, is_active=True)[:2])
    if len(boxes) != 1:
        raise MailboxError('Ustaw w panelu administratora jedną aktywną skrzynkę z przeznaczeniem „Rekrutacja do zespołu”.')
    return boxes[0]


def imported_uids(config, validity):
    return set(receipts(config, validity).values_list('uid', flat=True)) | set(
        RecruitmentMailSource.objects.filter(mailbox_key=mailbox_key(config), uid_validity=validity).values_list('uid', flat=True))


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
        'status_choices': [('new', 'Bez decyzji'), ('accepted', 'Przyjęty'), ('rejected', 'Odrzucony'), ('mixed', 'Różne decyzje')]})


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@recruitment_mailbox_required
def recruitment_mailbox(request):
    action = 'preview_html' if request.POST.get('preview_uid') else request.POST.get('action', 'headers')
    context = {'bulk_form': RecruitmentBulkForm(), 'max_samples': MAX_SAMPLES}
    archive = None
    try:
        config = recruitment_connection()
        key = mailbox_key(config)
        saved = request.session.get(SESSION_KEY)
        if saved and ('sent_since' not in saved or saved.get('mailbox') != key or saved.get('user') != request.user.pk):
            saved = None
            request.session.pop(SESSION_KEY, None)
        form = RecruitmentMailboxForm(request.POST if request.method == 'POST' else None,
            initial={'roles': saved['roles'] or ['all'], 'show_downloaded': saved['show']} if saved else None)
        date_form = MailboxDateForm(request.POST if request.method == 'POST' else None,
            initial=saved.get('date_filter') if saved else None)
        context.update(form=form, date_form=date_form, mailbox=config)
        if request.method == 'POST':
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

        def signed(payload):
            return signing.dumps({**identity, **payload}, salt=SALT, compress=True)

        def load(value, kind):
            try:
                payload = signing.loads(value, salt=SALT, max_age=3600)
            except signing.BadSignature:
                raise MailboxError('Wybór wygasł. Pobierz nagłówki ponownie.') from None
            if not isinstance(payload, dict) or any(payload.get(k, '' if k == 'sent_since' else None) != v for k, v in identity.items()) or payload.get('kind') != kind:
                raise MailboxError('Zmieniono filtry lub skrzynkę. Pobierz nagłówki od początku.')
            return payload

        def populate(result):
            result = deepcopy(result)
            known = {source.uid: source for source in RecruitmentMailSource.objects.filter(
                mailbox_key=key, uid_validity=result['validity'], uid__in=[row['uid'] for row in result['rows']]).select_related('recruitment').prefetch_related('recruitment__role_decisions')}
            downloaded = set(receipts(config, result['validity']).values_list('uid', flat=True))
            for row in result['rows']:
                source = known.get(row['uid'])
                row['downloaded'] = row['uid'] in downloaded
                row['record'] = source.recruitment if source else None
            context.update(result)
            context['selection'] = signed({'kind': 'selection', 'validity': result['validity'], 'uids': [row['uid'] for row in result['rows']]})
            for name in ('next_cursor', 'previous_cursor'):
                context[name] = signed({'kind': 'cursor', 'cursor': result[name]}) if result[name] else ''

        if saved and saved['roles'] == roles and saved['show'] == show and saved['sent_since'] == sent_since:
            populate(saved['result'])
        excluded = None if show else lambda validity: imported_uids(config, validity)
        if request.method == 'GET' and saved:
            return render(request, 'core/recruitment_mailbox.html', context)
        if action == 'headers':
            cursor = load(request.POST['cursor'], 'cursor')['cursor'] if request.POST.get('cursor') else None
            result = read_headers(config, cursor, excluded=excluded, recruitment_roles=roles, sent_since=sent_since)
            request.session[SESSION_KEY] = {**identity, 'result': result, 'date_filter': date_form.snapshot() if request.method == 'POST' else {'date_filter_enabled': True, 'sent_since': sent_since}}
            populate(result)
            if request.method == 'POST':
                return redirect(request.path)
        else:
            selection = load(request.POST.get('selection', ''), 'selection')
            if action == 'copy_emails':
                require_superuser(request.user)
                from core.services.mailbox_email_copy import sender_emails
                return JsonResponse({'emails': sender_emails(config, selection['validity'], excluded=excluded, recruitment_roles=roles, sent_since=sent_since)})
            if action not in ('preview', 'preview_html', 'download', 'add', 'bulk'):
                raise MailboxError('Nieznana operacja.')
            try:
                chosen = [request.POST.get('uid') or request.POST.get('preview_uid')] if action in ('preview', 'preview_html') else request.POST.getlist('selected')
                selected = {int(value) for value in chosen}
            except (ValueError, TypeError):
                raise MailboxError('Nieprawidłowy wybór wiadomości.') from None
            if not 1 <= len(selected) <= MAX_SAMPLES or not selected.issubset(selection['uids']):
                raise MailboxError(f'Wybierz od 1 do {MAX_SAMPLES} wiadomości z pobranej listy.')
            context['selected'] = {value for value in selection['uids'] if str(value) in request.POST.getlist('selected')}
            if action == 'bulk':
                bulk = RecruitmentBulkForm(request.POST)
                context['bulk_form'] = bulk
                if not bulk.is_valid():
                    raise MailboxError('Wybierz poprawną zmianę decyzji lub pola Powiadomiony.')
            raw_messages = fetch_messages(config, selection['validity'], selected, raw_messages=True, max_messages=MAX_SAMPLES)
            if {row['uid'] for row in raw_messages} != selected:
                raise MailboxError('Nie pobrano wszystkich wybranych wiadomości. Niczego nie zapisano.')
            if action in ('preview', 'preview_html'):
                preview = parse_sample(raw_messages[0]['raw'])
                body = preview['mail_body']
                preview_data = {'subject': preview['mail_subject'], 'sender': preview['mail_sender'],
                    'body': body[:100000], 'truncated': len(body) > 100000}
                if action == 'preview':
                    return render(request, 'core/includes/recruitment_preview.html', {'preview': preview_data})
                context['preview'] = preview_data
                return render(request, 'core/recruitment_mailbox.html', context)
            if action == 'download':
                archive = attachment_archive(raw_messages)
            with transaction.atomic():
                records = store_samples(config, selection['validity'], raw_messages, downloaded=action == 'download')
                if action == 'bulk':
                    # Only explicitly chosen flags change; notes and other metadata remain intact.
                    records = Recruitment.objects.select_for_update().filter(pk__in=[record.pk for record in records]).order_by('pk')
                    for record in records:
                        fields = ['updated_at']
                        if bulk.cleaned_data['decision']:
                            from core.services.recruitment_decisions import set_all_decisions
                            set_all_decisions(record, {'yes': 'accepted', 'no': 'rejected', 'clear': 'new'}[bulk.cleaned_data['decision']])
                        if bulk.cleaned_data['notified']:
                            record.notified = bulk.cleaned_data['notified'] == 'yes'
                            fields.append('notified')
                        record.save(update_fields=fields)
            if action == 'download':
                response = FileResponse(archive, as_attachment=True, filename='probki-zalaczniki.zip', content_type='application/zip')
                archive = None
                response['X-Content-Type-Options'] = 'nosniff'
                return response
            notifications.success(request, f'Zapisano zgłoszenia w Rekrutacji: {len(selected)}.' if action == 'add' else f'Zapisano zmiany dla wiadomości: {len(selected)}. Nie wysyłano powiadomień e-mail.')
            return redirect(request.path)
    except MailboxError as error:
        if action == 'preview':
            return render(request, 'core/includes/recruitment_preview.html', {'preview_error': str(error)}, status=400)
        context.setdefault('form', RecruitmentMailboxForm())
        context['error'] = str(error)
        return render(request, 'core/recruitment_mailbox.html', context, status=400 if request.method == 'POST' else 200)
    finally:
        if archive is not None:
            archive.close()
    return render(request, 'core/recruitment_mailbox.html', context)
