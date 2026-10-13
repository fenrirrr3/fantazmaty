"""Odrzucony token CSRF – zrozumiały komunikat zamiast angielskiej strony 403 Django.

Najczęstszy przypadek to formularz logowania otwarty dawno albo przywrócony z pamięci
telefonu (karta z wczoraj, powrót „wstecz”): logowanie zmienia token, więc stara kopia
strony wysyła nieaktualny. Wtedy zalogowaną osobę od razu przenosimy dalej, a pozostałym
pokazujemy świeży formularz logowania. Dla innych formularzy – strona z instrukcją.
"""
from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render, resolve_url
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode


def _safe_next(request):
    target = request.POST.get('next') or request.GET.get('next') or ''
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                  require_https=request.is_secure()):
        return target
    return ''


def csrf_failure(request, reason=''):
    login_pages = {reverse('login'): resolve_url(settings.LOGIN_REDIRECT_URL),
                   reverse('admin:login'): reverse('admin:index')}
    if request.path in login_pages:
        target = _safe_next(request)
        if request.user.is_authenticated:
            return redirect(target or login_pages[request.path])
        messages.warning(request, 'Strona logowania była nieaktualna (np. otwarta dawno albo w innej karcie). '
                                  'Zaloguj się jeszcze raz.')
        return redirect(request.path + (f'?{urlencode({"next": target})}' if target else ''))
    referer = request.META.get('HTTP_REFERER', '')
    back = referer if url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()},
                                                      require_https=request.is_secure()) else ''
    return render(request, 'core/csrf_failure.html', {'back_url': back}, status=403)
