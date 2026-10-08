"""Human names for existing objects; never use form contents or credentials."""
import re
from django.apps import apps
from django.urls import resolve, Resolver404


MODEL_KEYS = (
    ('chapter_id', 'texts.Text'), ('text_id', 'texts.Text'), ('review_id', 'texts.Review'),
    ('author_id', 'authors.Author'), ('novel_id', 'texts.Anthology'), ('anthology_id', 'texts.Anthology'),
    ('illustrator_id', 'illustrations.Illustrator'), ('illustration_id', 'illustrations.Illustration'),
    ('proposal_id', 'illustrations.CoverProposal'), ('stage_id', 'workflow.WorkflowStage'),
    ('vacation_id', 'people.Vacation'), ('term_id', 'texts.VocabularyTerm'), ('person_id', 'people.Person'),
)


def object_for_match(match):
    admin_model = getattr(getattr(match.func, 'model_admin', None), 'model', None)
    if match.namespace == 'admin' and admin_model:
        return admin_model, match.kwargs.get('object_id')
    for key, label in MODEL_KEYS:
        if key in match.kwargs:
            if key == 'person_id' and (match.url_name or '').startswith('translation_person'):
                label = 'texts.ForeignAuthor' if match.kwargs.get('kind') == 'author' else 'texts.Translator'
            return apps.get_model(label), match.kwargs[key]
    if 'pk' in match.kwargs:
        name = match.url_name or ''
        label = ('core.Recruitment' if name.startswith('recruitment_') else
                 'texts.Extract' if name.startswith('extract_') else
                 'core.AnthologyCorrection' if name.startswith('correction_') else None)
        if label:
            return apps.get_model(label), match.kwargs['pk']
    return None, None


def object_name(obj, depth=0):
    if depth > 2:
        return ''
    # Approved display fields only: no note contents, messages or secrets.
    for field in ('display_name', 'full_name', 'title', 'name', 'story_title'):
        value = getattr(obj, field, '')
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = ' '.join(str(getattr(obj, field, '') or '') for field in ('first_name', 'last_name')).strip()
    if value:
        return value
    for field in ('text', 'review', 'anthology', 'person', 'author', 'recruitment'):
        related = getattr(obj, field, None)
        if hasattr(related, '_meta'):
            value = object_name(related, depth + 1)
            if value:
                return value
    return ''


def target_for_match(match, *, lookup=True):
    model, pk = object_for_match(match)
    if not model or pk is None or not str(pk).isascii() or not str(pk).isdecimal() or len(str(pk)) > 18:
        return ''
    fallback = f'{model._meta.verbose_name}: #{pk}'
    if lookup:
        obj = model.objects.filter(pk=pk).first()
        if obj:
            name = object_name(obj)
            if name:
                return f'{name[:225]} (#{pk})'[:255]
    return fallback[:255]


def present_activities(entries):
    from core.activity import describe_request, LABELS
    cache = {}
    for entry in entries:
        entry.display_action, entry.display_target = entry.action, entry.target
        try:
            match = resolve(entry.path)
        except (Resolver404, ValueError):
            match = None
        if match:
            label, _ = describe_request(match, entry.method)
            prefixes = ''.join(re.findall(r'(?:Próba / formularz: |Podgląd user_id #\d+: )', entry.action))
            entry.display_action = prefixes + label
            legacy = not entry.target or bool(re.fullmatch(r'(?:\w+_id: )?#\d+', entry.target))
            if legacy:
                if entry.path not in cache:
                    cache[entry.path] = target_for_match(match)
                entry.display_target = cache[entry.path] or entry.target
        else:
            for name, label in LABELS.items():
                if entry.action in (name, name.replace('_', ' ')):
                    entry.display_action = label
                    break
