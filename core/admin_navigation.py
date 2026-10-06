"""Task-oriented navigation without changing model registrations or permissions."""
from django.template.response import TemplateResponse
from django.urls import reverse
from urllib.parse import urlencode

GROUPS = (
    ('submissions', 'Zgłoszenia i recenzje', ('texts.review', 'texts.extract')),
    ('publication', 'Teksty i antologie', ('texts.text', 'texts.anthology', 'texts.anthologytask', 'core.anthologycorrection')),
    ('novels', 'Powieści i słownik', ('novels', 'texts.novelprofile', 'texts.vocabularyterm')),
    ('translations', 'Tłumaczenia', ('texts.texttranslation', 'texts.foreignauthor', 'texts.translator')),
    ('authors', 'Autorzy', ('authors.author', 'blacklist')),
    ('team', 'Zespół i konta', ('people.person', 'auth.user', 'people.vacation', 'core.recruitment')),
    ('art', 'Ilustracje i okładki', ('illustrations.illustration', 'illustrations.illustrator', 'illustrators_active', 'illustrators_inactive', 'illustrations.coverproposal')),
    ('audio', 'Audiobooki i audiodeskrypcje', ('audiobooks_queue', 'audiobooks_blacklist', 'audio_description_tasks')),
    ('history', 'Historia i diagnostyka', ('workflow.workflowstage', 'workflow.workflowroleassignment', 'core.useractivity', 'core.workflowevent')),
    ('settings', 'Ustawienia', ('core.mailboxconnection', 'people.role', 'auth.group')),
)
DETAIL_MODELS = ('texts.textnote', 'authors.authornote', 'texts.reviewassignment', 'texts.reviewers',
                 'workflow.workflowrepetition', 'workflow.workflowhandoff')
LABELS = {
    'texts.review': 'Zgłoszenia do recenzji', 'people.person': 'Osoby w zespole',
    'auth.user': 'Konta użytkowników', 'texts.anthologytask': 'Zadania antologii',
    'workflow.workflowstage': 'Etapy pracy', 'workflow.workflowroleassignment': 'Przydziały wykonawców',
    'auth.group': 'Grupy uprawnień', 'texts.reviewassignment': 'Oceny i przydziały recenzentów',
    'texts.reviewers': 'Ogólne uwagi do zgłoszeń',
}



def blacklist_index(site, request):
    from django.contrib.admin import AdminSite
    models = {m['object_name'].lower(): m for app in AdminSite.get_app_list(site, request) for m in app['models']}
    entries = []
    for key, label in (('blacklistedauthor', 'Autorzy z bazy'), ('blacklistentry', 'Wpisy bez profilu autora')):
        if key in models: entries.append({**models[key], 'name': label})
    return TemplateResponse(request, 'admin/blacklist_index.html', {
        **site.each_context(request), 'title': 'Czarna lista autorów', 'entries': entries,
    })


def grouped_app_list(original):
    models = {f"{app['app_label']}.{m['object_name'].lower()}": dict(m) for app in original for m in app['models']}
    # Reuse registered admins, their search, permissions and edit forms.
    shortcuts = (
        ('novels', 'texts.anthology', 'Powieści', {'is_novel__exact': '1'}),
        ('audiobooks_queue', 'texts.text', 'Audiobooki do nagrywania',
         {'for_recording__exact': '1', 'audiobook_blacklisted__exact': '0'}),
        ('audiobooks_blacklist', 'texts.text', 'Czarna lista audiobooków',
         {'audiobook_blacklisted__exact': '1'}),
        ('audio_description_tasks', 'texts.anthologytask', 'Zadania audiodeskrypcji',
         {'task_type__exact': 'audio_description'}),
        ('illustrators_active', 'illustrations.illustrator', 'Ilustratorzy aktywni',
         {'is_active__exact': '1'}),
        ('illustrators_inactive', 'illustrations.illustrator', 'Ilustratorzy nieaktywni',
         {'is_active__exact': '0'}),
    )
    for key, source, label, filters in shortcuts:
        registered = models.get(source)
        if registered and registered.get('admin_url'):
            models[key] = {'name': label, 'object_name': key,
                           'admin_url': registered['admin_url'] + '?' + urlencode(filters),
                           'view_only': True}
    for key, label in LABELS.items():
        if key in models:
            models[key]['name'] = label
    blacklist_models = [models.pop(key) for key in ('authors.blacklistedauthor', 'authors.blacklistentry') if key in models]
    if blacklist_models:
        models['blacklist'] = {'name': 'Czarna lista autorów', 'object_name': 'BlacklistHub', 'admin_url': reverse('admin:blacklist_index'), 'view_only': True}
    details = [models.pop(key) for key in DETAIL_MODELS if key in models]
    result = []
    for index, (key, name, members) in enumerate(GROUPS):
        entries = [models.pop(member) for member in members if member in models]
        if entries or key == 'history' and details:
            group = {'name': name, 'app_label': key, 'app_url': reverse('admin:index') + '#group-' + key,
                     'models': entries, 'collapsed': key not in ('submissions', 'publication', 'novels', 'translations', 'authors')}
            if key == 'history' and details:
                group['subgroups'] = [{'name': 'Szczegółowe rekordy', 'app_label': 'history-details',
                                       'models': details, 'collapsed': True}]
            result.append(group)
    if models:
        # Preserve access to every registered model without a miscellaneous root tab.
        history = next((group for group in result if group['app_label'] == 'history'), None)
        if history is None:
            history = {'name': 'Historia i diagnostyka', 'app_label': 'history',
                       'app_url': reverse('admin:index') + '#group-history', 'models': [], 'collapsed': True}
            result.insert(max(len(result) - 1, 0), history)
        history.setdefault('subgroups', []).append({'name': 'Pozostałe rekordy',
            'app_label': 'history-other', 'models': sorted(models.values(), key=lambda model: model['name']), 'collapsed': True})

    return result

