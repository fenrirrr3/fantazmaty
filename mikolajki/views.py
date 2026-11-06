"""Strona Mikołajek (/mikolajki): zapisy bez konta, potwierdzenie z e-maila i własne zgłoszenie po kodzie.

Strona działa tylko z włączonym modułem (Moduły opcjonalne → Mikołajki); wyłączona zwraca 404.
"""
import logging
from functools import wraps

from django.contrib import messages
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_http_methods

from . import services
from .forms import CodeForm, EntryDataForm, SignupForm
from .models import Edition, Entry

logger = logging.getLogger(__name__)
SESSION_KEY = 'mikolajki_entry'
MAIL_ERROR = ('Nie udało się wysłać wiadomości e-mail. Spróbuj ponownie za kilka minut – zgłoszenie zostanie '
              'zapisane po ponownym wysłaniu formularza.')


def module_page(view):
    """Strona modułu: 404 przy wyłączonym module, bez indeksowania i bez adresu strony w Referer poza nią.

    Polityka same-origin, nie no-referrer jak na stronach tylko do odczytu: przy no-referrer przeglądarka wysyła
    formularz z nagłówkiem „Origin: null” i ochrona CSRF odrzuca zgłoszenie."""
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not services.enabled():
            raise Http404('Mikołajki są wyłączone.')
        response = view(request, *args, **kwargs)
        response['X-Robots-Tag'] = 'noindex, nofollow'
        response['Referrer-Policy'] = 'same-origin'
        return response
    return never_cache(wrapper)


def _page(request, template, context, status=200):
    edition = context.get('edition') or Edition.current()
    return render(request, f'mikolajki/{template}.html', {'edition': edition, **context}, status=status)


def _too_many(request, wait):
    response = _page(request, 'message', {'title': 'Zbyt wiele prób',
                                          'message': 'Zbyt wiele prób z tego adresu. Spróbuj ponownie za kilka minut.'},
                     status=429)
    response['Retry-After'] = str(wait)
    return response


@module_page
@require_http_methods(['GET', 'POST'])
def secret_santa(request):
    edition = Edition.current()
    if edition is None:
        return _page(request, 'message', {'title': 'Mikołajki Fantazmatów',
                                          'message': 'Zapisy na kolejną edycję jeszcze się nie rozpoczęły.'})
    form = SignupForm(request.POST or None)
    if request.method == 'POST':
        if not edition.signups_open():
            messages.error(request, 'Zapisy są już zamknięte.')
            return redirect('mikolajki:secret_santa')
        wait = services.throttled(request, 'signup')
        if wait:
            return _too_many(request, wait)
        if form.is_valid():
            if form.cleaned_data['website']:
                # Bot: ta sama odpowiedź co przy prawdziwym zgłoszeniu, bez zapisu i wiadomości.
                messages.success(request, _sent_message(form.cleaned_data['email']))
                return redirect('mikolajki:secret_santa')
            try:
                _register(request, edition, form.cleaned_data)
            except Exception:  # noqa: BLE001 – serwer pocztowy niedostępny: komunikat zamiast błędu strony
                logger.exception('Mikołajki: nie udało się wysłać wiadomości po zgłoszeniu')
                form.add_error(None, MAIL_ERROR)
            else:
                messages.success(request, _sent_message(form.cleaned_data['email']))
                return redirect('mikolajki:secret_santa')
    return _page(request, 'signup', {'edition': edition, 'form': form, 'open': edition.signups_open(),
                                     'code_form': CodeForm()}, status=400 if form.errors else 200)


def _sent_message(email):
    return (f'Wysłaliśmy wiadomość na adres {email}. Kliknij w niej link potwierdzający – dopiero wtedy '
            'zgłoszenie bierze udział w losowaniu. W wiadomości jest też Twój kod uczestnika.')


def _register(request, edition, data):
    """Nowe zgłoszenie albo ponowne (niepotwierdzone lub wycofane – nowe dane i nowy link); przy potwierdzonym
    zgłoszeniu tylko przypomnienie kodu – dane się nie zmieniają, a odpowiedź strony jest taka sama."""
    values = {name: data[name] for name in ('full_name', 'shipping_address', 'phone', 'letter')}
    with transaction.atomic():
        entry = Entry.objects.select_for_update().filter(edition=edition, email=data['email']).first()
        if entry is not None and entry.active:
            reminder = True
        else:
            reminder = False
            if entry is None:
                entry = Entry(edition=edition, email=data['email'], code=services.new_code())
            for name, value in values.items():
                setattr(entry, name, value)
            entry.confirmed_at = entry.withdrawn_at = None
            entry.consent_at = timezone.now()
            entry.source = Entry.Source.FORM
            try:
                with transaction.atomic():
                    entry.save()
            except IntegrityError:  # równoległe zgłoszenie z tym samym adresem
                entry = Entry.objects.get(edition=edition, email=data['email'])
                reminder = entry.active
    (services.send_reminder if reminder else services.send_signup)(entry, request)


@module_page
@require_GET
def secret_santa_confirm(request, token):
    entry = services.entry_from_token(token)
    if entry is None or entry.edition_id != getattr(Edition.current(), 'pk', None):
        return _page(request, 'message', {'title': 'Link nieprawidłowy',
                                          'message': 'Ten link jest nieprawidłowy albo dotyczy wcześniejszej edycji.'},
                     status=404)
    if entry.withdrawn_at:
        message = 'To zgłoszenie zostało wycofane. Możesz zapisać się ponownie na stronie zapisów.'
    elif entry.confirmed_at:
        message = 'Zgłoszenie było już potwierdzone – bierzesz udział w losowaniu.'
    elif not entry.edition.signups_open():
        message = 'Zapisy są już zamknięte – zgłoszenia nie można potwierdzić.'
    else:
        Entry.objects.filter(pk=entry.pk, confirmed_at__isnull=True).update(confirmed_at=timezone.now())
        message = 'Dziękujemy! Zgłoszenie jest potwierdzone – bierzesz udział w losowaniu.'
    return _page(request, 'message', {'title': 'Potwierdzenie zgłoszenia', 'message': message, 'entry_link': True,
                                      'edition': entry.edition})


def _session_entry(request):
    """Zgłoszenie zalogowanego kodem uczestnika (najnowsza edycja, niewycofane) albo None."""
    pk = request.session.get(SESSION_KEY)
    edition = Edition.current()
    if not pk or edition is None:
        return None
    return Entry.objects.filter(pk=pk, edition=edition, withdrawn_at__isnull=True) \
        .select_related('edition', 'recipient').first()


@module_page
@require_http_methods(['GET', 'POST'])
def secret_santa_entry(request):
    edition = Edition.current()
    if edition is None:
        raise Http404('Brak edycji Mikołajek.')
    entry = _session_entry(request)
    action = request.POST.get('action', '')
    if entry is None:
        form = CodeForm(request.POST if action == 'login' else None)
        if action == 'login':
            wait = services.throttled(request, 'code')
            if wait:
                return _too_many(request, wait)
            if form.is_valid():
                found = services.find_entry(form.cleaned_data['email'], form.cleaned_data['code'])
                if found is not None:
                    request.session.cycle_key()
                    request.session[SESSION_KEY] = found.pk
                    return redirect('mikolajki:secret_santa_entry')
                form.add_error(None, 'Nie znaleźliśmy zgłoszenia z tym adresem e-mail i kodem.')
        return _page(request, 'login', {'edition': edition, 'form': form}, status=400 if form.errors else 200)
    data_form = EntryDataForm(instance=entry)
    if request.method == 'POST':
        response = _entry_action(request, entry, action)
        if response is not None:
            return response
        data_form = EntryDataForm(request.POST, instance=entry)
        data_form.is_valid()  # pokazanie błędów (zapis tylko w _entry_action)
    return _page(request, 'entry', {'edition': edition, 'entry': entry, 'form': data_form,
                                    'open': edition.signups_open()},
                 status=400 if request.method == 'POST' else 200)


def _entry_action(request, entry, action):
    """Działanie na własnym zgłoszeniu; None – formularz danych z błędami do pokazania."""
    open_ = entry.edition.signups_open()
    if action == 'logout':
        request.session.pop(SESSION_KEY, None)
        return redirect('mikolajki:secret_santa')
    if action == 'save' and open_:
        form = EntryDataForm(request.POST, instance=entry)
        if not form.is_valid():
            return None
        form.save()
        try:
            services.send_summary(entry, request, changed=True)
        except Exception:  # noqa: BLE001
            logger.exception('Mikołajki: nie udało się wysłać podsumowania po zmianie')
            messages.warning(request, 'Zmiany zapisane, ale nie udało się wysłać podsumowania e-mailem.')
        else:
            messages.success(request, 'Zmiany zapisane. Podsumowanie wysłaliśmy e-mailem.')
    elif action == 'resend' and open_ and not entry.confirmed_at:
        try:
            services.send_signup(entry, request)
        except Exception:  # noqa: BLE001
            logger.exception('Mikołajki: nie udało się ponownie wysłać linku')
            messages.error(request, MAIL_ERROR)
        else:
            messages.success(request, 'Wysłaliśmy ponownie wiadomość z linkiem potwierdzającym.')
    elif action == 'withdraw' and open_:
        Entry.objects.filter(pk=entry.pk).update(withdrawn_at=timezone.now())
        request.session.pop(SESSION_KEY, None)
        messages.success(request, 'Zgłoszenie zostało wycofane. Możesz zapisać się ponownie do końca zapisów.')
        return redirect('mikolajki:secret_santa')
    elif action == 'prepared' and entry.recipient_id:
        Entry.objects.filter(pk=entry.pk, package_prepared_at__isnull=True).update(package_prepared_at=timezone.now())
        messages.success(request, 'Dziękujemy! Organizator wyśle Ci e-mailem adres do wysyłki paczki.')
    else:
        messages.error(request, 'Ta operacja nie jest teraz dostępna.')
    return redirect('mikolajki:secret_santa_entry')
