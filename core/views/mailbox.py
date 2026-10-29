"""Pobieranie zgłoszeń tekstów ze skrzynki: nagłówki, podgląd zgłoszeń, zapis i paczka ZIP (tylko superuser)."""
from copy import deepcopy
import re
from zipfile import BadZipFile

from lxml.etree import XMLSyntaxError
from django.contrib import messages as notifications
from django.core.exceptions import ValidationError
from django.db import transaction
from django.contrib.auth.decorators import login_required
from django.http import FileResponse
from django.shortcuts import render, redirect
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_http_methods
from core.mailbox_date import MailboxDateForm
from core.models import MailboxConnection, MailboxDownload
from core.permissions import superuser_required
from core.services.mailbox import read_headers, MailboxError
from core.services.mailbox_import import (
    default_mailbox, downloaded_uids, mailbox_key, receipts, fetch_messages, prepare_forms, package_messages,
    describe_import_error, filter_valid_messages, MAX_MESSAGES, BLACKLIST_CHOICES, DOWNLOAD_FILE, FUTURE_REJECTION,
    blacklisted_addresses, mark_blacklisted, sender_address,
)
from core.services.document_converter import ConversionError
from core.services.recruitment_samples import parse_sample
from core.services.reviews import import_reviews
from core.views.mailbox_common import copied_emails, load_signed, preview_data, signed, stored_headers, with_senders

HEADERS_SESSION_KEY = 'mailbox_headers_snapshot'
IMPORT_OPTIONS = ('show_downloaded', 'clean', 'rebuild', 'convert', 'subject_filter')
# Ciasteczko z tokenem formularza: skrypt strony chowa pasek postępu, gdy przeglądarka dostanie ZIP.
DOWNLOAD_COOKIE = 'mailbox_import_done'
EXPIRED = 'Podgląd wygasł lub ustawienia skrzynki się zmieniły. Pobierz nagłówki ponownie.'


def blacklist_choices(request):
    """Wybory przy wiadomościach od autorów z czarnej listy: {uid: 'reject' | 'download'}."""
    result = {}
    for name, value in request.POST.items():
        if name.startswith('blacklist_') and name[10:].isdigit() and value in (FUTURE_REJECTION, DOWNLOAD_FILE):
            result[int(name[10:])] = value
    return result


def load_token(token, user, key, salt):
    """Podpisany wybór, kursor albo podgląd tego użytkownika i tej skrzynki."""
    return load_signed(token, salt, {'user': user.pk, 'mailbox': key}, EXPIRED)


def restore_headers(request, config, key, context, saved):
    """The private session stores headers only, never bodies or attachments."""
    if not saved:
        return
    result = deepcopy(saved['result'])
    uids = [row['uid'] for row in result['rows']]
    already = set(receipts(config, result['validity']).filter(uid__in=uids).values_list('uid', flat=True))
    blacklisted = blacklisted_addresses(row.get('sender') for row in result['rows'])
    for row in result['rows']:
        row['downloaded'] = row['uid'] in already
        row['blacklisted'] = sender_address(row.get('sender')) in blacklisted
        row['blacklist_choice'] = context['blacklist_selected'].get(row['uid'], FUTURE_REJECTION)
    base = {'user': request.user.pk, 'mailbox': key, 'subject_filter': saved['subject_filter'],
            'sent_since': saved['sent_since'], 'show_downloaded': saved['show_downloaded']}
    for field in ('next_cursor', 'previous_cursor'):
        if result.get(field):
            result[field] = signed(dict(base, cursor=result[field]), 'mailbox-cursor')
    context['result'] = result
    # Renew signatures from trusted session data without refetching the mailbox.
    context['selection'] = signed(dict(base, validity=result['validity'], uids=uids), 'mailbox-selection')
    if request.method == 'GET':
        context.update({name: saved[name] for name in IMPORT_OPTIONS})
        context['date_form'] = MailboxDateForm(initial=saved['date_filter'])


def _same_filters(payload, context, sent_since, *, show=True):
    """Podpisany wybór lub kursor dotyczy bieżącego naboru i daty (z `show` – także pokazywania pobranych)."""
    return (payload.get('sent_since', '') == sent_since and payload.get('subject_filter', '') == context['subject_filter']
            and (not show or payload.get('show_downloaded', False) == context['show_downloaded']))


def _headers(request, action, config, key, context, sent_since):
    """Strona nagłówków zapisana w sesji; odświeżenie powtarza GET, a nie pobieranie ze skrzynki."""
    cursor = None
    if request.POST.get('cursor'):
        page = load_token(request.POST['cursor'], request.user, key, 'mailbox-cursor')
        if _same_filters(page, context, sent_since):
            cursor = page['cursor']
    result = read_headers(config, cursor, excluded=None if context['show_downloaded'] else downloaded_uids(config),
                          subject_filter=context['subject_filter'], sent_since=sent_since)
    request.session[HEADERS_SESSION_KEY] = dict(
        user=request.user.pk, mailbox=key, sent_since=sent_since, result=result,
        date_filter=context['date_form'].snapshot(), fetched_at=timezone.now().isoformat(),
        **{name: context[name] for name in IMPORT_OPTIONS})
    return redirect(request.path)


def _preview(request, action, config, key, context, sent_since):
    """Treść jednej wiadomości jak w rekrutacji – sam tekst (HTML bez znaczników i zasobów zdalnych):
    fragment strony dla skryptu albo cała strona (bez JS)."""
    selection = load_token(request.POST.get('selection', ''), request.user, key, 'mailbox-selection')
    try:
        uid = int(request.POST.get('uid') or request.POST.get('preview_uid'))
    except (TypeError, ValueError):
        raise MailboxError('Nieprawidłowy wybór wiadomości.') from None
    if uid not in selection['uids']:
        raise MailboxError('Wybierz wiadomość z pobranej listy.')
    raw = fetch_messages(config, selection['validity'], [uid], raw_messages=True, max_messages=1)
    if len(raw) != 1:
        raise MailboxError('Nie udało się pobrać wiadomości. Pobierz nagłówki ponownie.')
    preview = preview_data(parse_sample(raw[0]['raw']))
    if action == 'preview':
        return render(request, 'core/includes/recruitment_preview.html', {'preview': preview})
    context['preview'] = preview


def _copy_emails(request, action, config, key, context, sent_since):
    """Adresy nadawców wszystkich wiadomości z filtrami, z którymi pobrano listę."""
    selection = load_token(request.POST.get('selection', ''), request.user, key, 'mailbox-selection')
    if not _same_filters(selection, context, sent_since):
        raise MailboxError('Zmieniono filtry. Pobierz nagłówki ponownie.')
    return copied_emails(config, selection['validity'],
                         excluded=None if context['show_downloaded'] else downloaded_uids(config),
                         subject_filter=context['subject_filter'], sent_since=sent_since)


def _download(request, action, config, key, context, sent_since):
    """Podgląd zgłoszeń z wybranych wiadomości (download), a po zatwierdzeniu (confirm) zapis i paczka ZIP."""
    selection = load_token(request.POST.get('selection', ''), request.user, key, 'mailbox-selection')
    if not _same_filters(selection, context, sent_since, show=False):
        raise MailboxError('Zmieniono nabór lub datę. Najpierw pobierz nagłówki ponownie.')
    try:
        selected = sorted(set(int(value) for value in request.POST.getlist('uids')))
    except ValueError:
        raise MailboxError('Nieprawidłowy wybór wiadomości.') from None
    if not selected or not set(selected).issubset(selection['uids']):
        raise MailboxError('Wybierz wiadomości z pobranej listy.')
    validity = selection['validity']
    skipped_errors = []
    messages = fetch_messages(config, validity, selected, skipped_errors)
    digest = [[m['uid'], m['digest']] for m in messages]
    # Czarna lista sprawdzana przed przetwarzaniem: przyszłe odrzucenia tracą załączniki od razu.
    mark_blacklisted(messages, context['blacklist_selected'])
    decisions = {str(m['uid']): FUTURE_REJECTION if m['future_rejection'] else DOWNLOAD_FILE
                 for m in messages if m['blacklisted']}
    confirmation = None
    if action == 'confirm':
        confirmation = load_token(request.POST.get('preview', ''), request.user, key, 'mailbox-preview')
        if (confirmation['digest'] != digest or confirmation['clean'] != context['clean'] or confirmation['convert'] != context['convert']
                or confirmation.get('rebuild') != context['rebuild']
                or confirmation.get('choices', {}) != decisions):
            confirmation = None
            context['error'] = 'Wiadomości lub opcje się zmieniły. Sprawdź nowy podgląd i zatwierdź go ponownie.'
    tokens = confirmation['warnings'] if confirmation else {}
    approved = bool(confirmation and request.POST.get('approve') == 'on')
    messages = filter_valid_messages(messages, config, validity, request.user, skipped_errors)
    future = [message for message in messages if message['future_rejection']]
    accepted, rebuild_warnings = [], []
    to_package = [message for message in messages if not message['future_rejection']]
    archive = None
    try:
        if to_package:
            archive = package_messages(to_package, clean=context['clean'], convert=context['convert'],
                rebuild=context['rebuild'], allow_rebuild_omissions=request.POST.get('allow_rebuild_omissions') == 'on',
                skipped_errors=skipped_errors, accepted=accepted, rebuild_warnings=rebuild_warnings)
        kept = {message['uid'] for message in [*accepted, *future]}
        messages = [message for message in messages if message['uid'] in kept]
        selected = [message['uid'] for message in messages]
        digest = [[m['uid'], m['digest']] for m in messages]
        if confirmation and confirmation['digest'] != digest:
            approved = False
            tokens = {}
            context['error'] = 'Pominięto wiadomości z błędami. Sprawdź nowy podgląd i zatwierdź pozostałe zgłoszenia.'
        forms = prepare_forms(messages, config, validity, request.user, tokens, approved)
        hard_errors = [describe_import_error(e, form.mail_sources) for form in forms for field, errors in form.errors.items()
                       if field != 'confirm_submission_warnings' for e in errors]
        rows = (context.get('result') or {}).get('rows', [])
        context.update(preview_messages=messages, forms=forms, selection=request.POST['selection'],
                       selected=selected, hard_errors=hard_errors,
                       skipped_errors=[with_senders(error, rows) for error in skipped_errors])
        if rebuild_warnings:
            context['rebuild_warning'] = True
            context['rebuild_warning_text'] = ' '.join(rebuild_warnings)
        if not messages:
            context['error'] = 'Brak poprawnych zgłoszeń do zaimportowania. Niczego nie zapisano.'
        warnings = {str(form.data['anthology']): form.data.get('submission_warnings_token', '') for form in forms}
        context['future_rejections'] = sum(message['future_rejection'] for message in messages)
        context['blacklisted_messages'] = [message for message in messages if message['blacklisted']]
        context['preview_token'] = signed({
            'user': request.user.pk, 'mailbox': key, 'sent_since': sent_since, 'digest': digest, 'warnings': warnings,
            'clean': context['clean'], 'convert': context['convert'], 'rebuild': context['rebuild'],
            'choices': {uid: value for uid, value in decisions.items() if int(uid) in selected}}, 'mailbox-preview')
        if action == 'confirm' and approved and messages and not rebuild_warnings and all(form.is_valid() for form in forms):
            response = _save(request, config, key, validity, messages, tokens, context, archive)
            archive = None  # ZIP należy już do odpowiedzi, która zamknie go po wysłaniu.
            return response
        if action == 'confirm' and not approved and not context.get('error'):
            context['error'] = 'Sprawdź podgląd i zaznacz potwierdzenie.'
    finally:
        if archive is not None:
            archive.close()


def _save(request, config, key, validity, messages, tokens, context, archive):
    """Zapis zgłoszeń i znaczników pobrania (pliki są już gotowe), potem ZIP – albo powrót, gdy paczki nie ma."""
    with transaction.atomic():
        locked = MailboxConnection.objects.select_for_update().get(pk=config.pk)
        if not locked.is_active or locked.purpose != MailboxConnection.Purpose.SUBMISSIONS or mailbox_key(locked) != key:
            raise MailboxError('Ustawienia skrzynki się zmieniły. Pobierz nagłówki ponownie.')
        # Receipt uniqueness and this lock prevent duplicate imports
        # on retries, double-clicks and concurrent downloads.
        for form in prepare_forms(messages, config, validity, request.user, tokens, True):
            try:
                import_reviews(user=request.user, form=form)
            except ValidationError as error:
                raise MailboxError(' '.join(describe_import_error(detail, form.mail_sources) for detail in error.messages)) from None
        for message in messages:
            MailboxDownload.objects.update_or_create(mailbox_key=key,
                uid_validity=validity, uid=message['uid'], defaults={'fingerprint': message.get('fingerprint', ''), 'message_id': message.get('message_id', '')})
    rejected = context['future_rejections']
    if rejected:
        notifications.success(request, f'Dodano do przyszłych odrzuceń bez pobierania pliku: {rejected}.')
    if archive is None:
        # Same przyszłe odrzucenia: zapisane zgłoszenia, bez paczki ZIP.
        return redirect(request.path)
    response = FileResponse(archive, as_attachment=True, filename='zgloszenia.zip', content_type='application/zip')
    response['Cache-Control'] = 'private, no-store'
    response['X-Content-Type-Options'] = 'nosniff'
    token = request.POST.get('download_token', '')
    if re.fullmatch(r'[a-z0-9]{8,40}', token):
        response.set_cookie(DOWNLOAD_COOKIE, token, max_age=120, path=request.path, samesite='Strict',
                            secure=request.is_secure())
    return response


ACTIONS = {'headers': _headers, 'preview': _preview, 'preview_html': _preview, 'copy_emails': _copy_emails,
           'download': _download, 'confirm': _download}


@never_cache
@login_required
@require_http_methods(['GET', 'POST'])
@superuser_required
@sensitive_post_parameters()
def mailbox_headers(request):
    post = request.method == 'POST'
    action = 'preview_html' if request.POST.get('preview_uid') else request.POST.get('action', 'headers')
    date_form = MailboxDateForm(request.POST if post else None)
    context = {'date_form': date_form, 'result': None, 'max_messages': MAX_MESSAGES, 'show_downloaded': request.POST.get('show_downloaded') == 'on',
               **{name: request.POST.get(name) == 'on' if post else True for name in ('clean', 'rebuild', 'convert')},
               'blacklist_options': BLACKLIST_CHOICES, 'blacklist_selected': blacklist_choices(request)}
    try:
        config = default_mailbox()
        key = mailbox_key(config)
        context.update(mailbox=config, subject_choices=config.subject_choices())
        saved, notice = stored_headers(request, HEADERS_SESSION_KEY, config, key, show_downloaded=False, clean=True,
                                       rebuild=True, convert=True, subject_filter='')
        context.update(notice)
        restore_headers(request, config, key, context, saved)
        context['subject_filter'] = request.POST.get('subject_filter', '') if post else context.get('subject_filter', '')
        if context['subject_filter'] and context['subject_filter'] not in context['subject_choices']:
            raise MailboxError('Wybrany nabór nie jest już dostępny. Wybierz go ponownie.')
        if post:
            if not date_form.is_valid():
                raise MailboxError('Popraw datę początkową.')
            if action not in ACTIONS:
                raise MailboxError('Nieprawidłowa operacja.')
            response = ACTIONS[action](request, action, config, key, context, date_form.effective_date())
            if response is not None:
                return response
    except (MailboxError, ConversionError) as error:
        if action == 'preview':
            return render(request, 'core/includes/recruitment_preview.html', {'preview_error': str(error)}, status=400)
        context['error'] = with_senders(str(error), (context.get('result') or {}).get('rows', []))
    except (BadZipFile, XMLSyntaxError, ValueError, OSError):
        context['error'] = 'Nie udało się przetworzyć pliku. Sprawdź załączniki lub spróbuj pobrać same oryginały.'
    # Zwinięta sekcja „Dodaj ręcznie”; po błędzie formularza ręcznego przekazuje ją render_intake_page.
    from core.views.intake import manual_intake
    context['manual'] = getattr(request, 'manual_intake', None) or manual_intake(request)
    from core.cyclic_recruitment import settings_forms
    context['cyclic_forms'] = getattr(request, 'cyclic_forms', None) or settings_forms()
    context['cyclic_open'] = any(form.errors for form in context['cyclic_forms'])
    return render(request, 'core/review_bulk_import.html', context,
                  status=400 if context.get('error') else 200)


@never_cache
@login_required
@require_http_methods(['POST'])
@superuser_required
def cyclic_recruitments(request):
    """Zapis odstępów naborów cyklicznych (sekcja „Nabory cykliczne” na stronie „Dodaj do recenzji”)."""
    from copy import copy
    from django.http import QueryDict
    from core.cyclic_recruitment import settings_forms
    forms = settings_forms(request.POST)
    if not all(form.is_valid() for form in forms):
        # Strona jak przy GET, z rozwiniętą sekcją i błędami pól.
        page = copy(request)
        page.method, page.POST, page.cyclic_forms = 'GET', QueryDict(), forms
        notifications.error(request, 'Nie zapisano odstępów naborów cyklicznych – popraw zaznaczone pola.')
        response = mailbox_headers(page)
        response.status_code = 400
        return response
    with transaction.atomic():
        for form in forms:
            form.save()
    notifications.success(request, 'Zapisano odstępy naborów cyklicznych: ' + ', '.join(
        f'{form.instance.name} – {form.instance.interval_label}' for form in forms) + '.')
    return redirect('core:review_bulk_import')
