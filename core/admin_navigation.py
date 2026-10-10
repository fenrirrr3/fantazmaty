"""Task-oriented navigation without changing model registrations or permissions."""
from django.template.response import TemplateResponse
from django.urls import reverse
from urllib.parse import urlencode

# Same order and names as the site menu: submissions, texts, anthologies, art, audio,
# people, recruitment, then accounts, settings and history (admin only).
GROUPS = (
    ('submissions', 'Zgłoszenia', ('texts.review',)),
    ('texts', 'Teksty', ('texts.text', 'texts.texttranslation', 'texts.foreignauthor', 'texts.translator', 'novels',
                         'texts.novelprofile', 'texts.vocabularyterm', 'texts.extract', 'texts.extractvolume')),
    ('publication', 'Antologie i wydanie', ('texts.anthology', 'texts.anthologytask', 'core.anthologycorrection',
                                            'core.postlayoutassignment')),
    ('art', 'Grafika', ('illustrations.illustration', 'illustrations.illustrator', 'illustrators_active', 'illustrators_inactive',
                        'illustrations.coverproposal', 'illustrations.publicillustrationsettings')),
    ('audio', 'Audio', ('core.audiobook', 'core.audiobookstage', 'core.audiocontributor', 'audiobooks_queue', 'audiobooks_blacklist',
                        'core.audiodescription', 'audio_description_tasks', 'core.publicaudiobooksettings')),
    ('people', 'Ludzie', ('people.person', 'people.vacation', 'authors.author', 'blacklist')),
    ('recruitment', 'Rekrutacja', ('core.recruitment', 'recruitment_mailbox')),
    ('accounts', 'Konta i uprawnienia', ('auth.user', 'people.role', 'auth.group')),
    ('settings', 'Ustawienia', ('core.mailboxconnection',)),
    ('history', 'Historia i diagnostyka', ('workflow.workflowstage', 'workflow.workflowroleassignment', 'core.workflowevent', 'core.useractivity')),
)
EXPANDED = ('submissions', 'texts', 'publication')
DETAIL_MODELS = ('texts.extracttextlink', 'texts.textnote', 'authors.authornote', 'texts.reviewassignment', 'texts.reviewers',
                 'workflow.workflowrepetition', 'workflow.workflowhandoff')
LABELS = {
    'texts.review': 'Recenzje', 'people.person': 'Zespół', 'texts.extract': 'Ekstrakty',
    'texts.extractvolume': 'Tomy Ekstraktów', 'core.useractivity': 'Aktywność użytkowników',
    'core.workflowevent': 'Zmiany workflow i powiadomienia', 'texts.vocabularyterm': 'Słownik tagów i gatunków',
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
        if key in models:
            entries.append({**models[key], "name": label})
    return TemplateResponse(
        request,
        "admin/blacklist_index.html",
        {
            **site.each_context(request),
            "title": "Czarna lista autorów",
            "entries": entries,
        },
    )


def grouped_app_list(original):
    models = {
        f"{app['app_label']}.{m['object_name'].lower()}": dict(m)
        for app in original
        for m in app["models"]
    }
    # Reuse registered admins, their search, permissions and edit forms.
    shortcuts = (
        (
            "recruitment_mailbox",
            "core.mailboxconnection",
            "Skrzynka rekrutacyjna",
            {"purpose__exact": "recruitment"},
        ),
        ("novels", "texts.anthology", "Powieści", {"is_novel__exact": "1"}),
        (
            "audiobooks_queue",
            "texts.text",
            "Audiobooki do nagrywania",
            {"for_recording__exact": "1", "audiobook_blacklisted__exact": "0"},
        ),
        (
            "audiobooks_blacklist",
            "texts.text",
            "Czarna lista audiobooków",
            {"audiobook_blacklisted__exact": "1"},
        ),
        (
            "audio_description_tasks",
            "texts.anthologytask",
            "Zadania audiodeskrypcji",
            {"task_type__exact": "audio_description"},
        ),
        (
            "illustrators_active",
            "illustrations.illustrator",
            "Ilustratorzy aktywni",
            {"is_active__exact": "1"},
        ),
        (
            "illustrators_inactive",
            "illustrations.illustrator",
            "Ilustratorzy nieaktywni",
            {"is_active__exact": "0"},
        ),
    )
    for key, source, label, filters in shortcuts:
        registered = models.get(source)
        if registered and registered.get("admin_url"):
            models[key] = {
                "name": label,
                "object_name": key,
                "admin_url": registered["admin_url"] + "?" + urlencode(filters),
                "view_only": True,
            }
    for key, label in LABELS.items():
        if key in models:
            models[key]["name"] = label
    blacklist_models = [
        models.pop(key)
        for key in ("authors.blacklistedauthor", "authors.blacklistentry")
        if key in models
    ]
    if blacklist_models:
        models["blacklist"] = {
            "name": "Czarna lista autorów",
            "object_name": "BlacklistHub",
            "admin_url": reverse("admin:blacklist_index"),
            "view_only": True,
        }
    details = [models.pop(key) for key in DETAIL_MODELS if key in models]
    result = []
    for index, (key, name, members) in enumerate(GROUPS):
        entries = [models.pop(member) for member in members if member in models]
        if entries or key == "history" and details:
            group = {
                "name": name,
                "app_label": key,
                "app_url": reverse("admin:index") + "#group-" + key,
                "models": entries,
                "collapsed": key not in EXPANDED,
            }
            if key == "history" and details:
                group["subgroups"] = [
                    {
                        "name": "Szczegółowe rekordy",
                        "app_label": "history-details",
                        "models": details,
                        "collapsed": True,
                    }
                ]
            result.append(group)
    if models:
        # Preserve access to every registered model without a miscellaneous root tab.
        history = next((group for group in result if group["app_label"] == "history"), None)
        if history is None:
            history = {
                "name": "Historia i diagnostyka",
                "app_label": "history",
                "app_url": reverse("admin:index") + "#group-history",
                "models": [],
                "collapsed": True,
            }
            result.insert(max(len(result) - 1, 0), history)
        history.setdefault("subgroups", []).append(
            {
                "name": "Pozostałe rekordy",
                "app_label": "history-other",
                "models": sorted(models.values(), key=lambda model: model["name"]),
                "collapsed": True,
            }
        )

    return result
