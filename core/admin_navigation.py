"""Task-oriented navigation without changing model registrations or permissions."""
from types import MethodType
from django.template.response import TemplateResponse
from django.urls import path, reverse

GROUPS = (
    ('publication', 'Teksty i antologie', ('texts.text', 'texts.anthology', 'core.anthologycorrection', 'illustrations.illustration', 'illustrations.coverproposal')),
    ('submissions', 'Zgłoszenia i recenzje', ('texts.review', 'texts.extract')),
    ('authors', 'Autorzy', ('authors.author', 'blacklist')),
    ('team', 'Zespół i konta', ('people.person', 'auth.user', 'people.vacation', 'core.recruitment')),
    ('settings', 'Ustawienia', ('core.mailboxconnection', 'people.role')),
    ('history', 'Historia i diagnostyka', ('core.useractivity', 'core.workflowevent')),
)
LABELS = {'texts.review': 'Zgłoszenia do recenzji', 'texts.reviewassignment': 'Oceny i przydziały recenzentów', 'texts.reviewers': 'Ogólne uwagi do zgłoszeń'}


def install(site):
    if getattr(site, '_cms_navigation_installed', False):
        return
    site._cms_navigation_installed = True
    original_app_list = site.get_app_list
    original_urls = site.get_urls

    def blacklist(request):
        models = {m['object_name'].lower(): m for app in original_app_list(request) for m in app['models']}
        entries = []
        for key, label in (('blacklistedauthor', 'Autorzy z bazy'), ('blacklistentry', 'Wpisy bez profilu autora')):
            if key in models:
                entries.append({**models[key], 'name': label})
        return TemplateResponse(request, 'admin/blacklist_index.html', {
            **site.each_context(request), 'title': 'Czarna lista', 'entries': entries,
        })

    def get_urls(self):
        return [path('czarna-lista/', self.admin_view(blacklist), name='blacklist_index')] + original_urls()

    def get_app_list(self, request, app_label=None):
        original = original_app_list(request, app_label)
        if app_label:
            return original
        models = {f"{app['app_label']}.{m['object_name'].lower()}": dict(m) for app in original for m in app['models']}
        for key, label in LABELS.items():
            if key in models:
                models[key]['name'] = label
        blacklist_models = [models.pop(key) for key in ('authors.blacklistedauthor', 'authors.blacklistentry') if key in models]
        if blacklist_models:
            models['blacklist'] = {'name': 'Czarna lista', 'object_name': 'BlacklistHub', 'admin_url': reverse('admin:blacklist_index'), 'view_only': True}
        result = []
        for key, name, members in GROUPS:
            entries = [models.pop(member) for member in members if member in models]
            if entries:
                result.append({'name': name, 'app_label': key, 'app_url': reverse('admin:index') + '#group-' + key, 'models': entries})
        if models:
            result.append({'name': 'Zaawansowane', 'app_label': 'advanced', 'app_url': reverse('admin:index') + '#group-advanced', 'models': list(models.values()), 'collapsed': True})
        return result

    site.get_urls = MethodType(get_urls, site)
    site.get_app_list = MethodType(get_app_list, site)
