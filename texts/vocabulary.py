"""Canonical vocabulary layered over the existing comma-separated text fields."""
import hashlib
import json
import unicodedata

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import transaction

from texts.models import NovelProfile, Text, VocabularyTerm
from texts.catalog_models import term_key


def tokens(value):
    seen, result = set(), []
    for item in (value or '').replace('\r', '\n').replace('\n', ',').split(','):
        name = ' '.join(unicodedata.normalize('NFKC', item).split())
        key = term_key(name)
        if name and key not in seen:
            seen.add(key)
            result.append(name)
    return result


def canonicalize(value, kind, *, register=False):
    names = tokens(value)
    entries = {term.key: term for term in VocabularyTerm.objects.filter(
        kind=kind, key__in=[term_key(name) for name in names]).select_related('canonical')}
    result = []
    for name in names:
        if len(name) > (100 if kind == 'genre' else 255):
            raise ValidationError('Nazwa hasła słownika jest zbyt długa.')
        term = entries.get(term_key(name))
        if term:
            name = (term.canonical or term).name
        elif register:
            if len(name) > (100 if kind == 'genre' else 255):
                raise ValidationError('Nazwa hasła słownika jest zbyt długa.')
            term, _ = VocabularyTerm.objects.get_or_create(kind=kind, key=term_key(name), defaults={'name': name})
            name = (term.canonical or term).name
        result.append(name)
    value = ', '.join(tokens(', '.join(result)))
    if len(value) > (100 if kind == 'genre' else 5000):
        raise ValidationError('Po ujednoliceniu pole przekracza dopuszczalną długość.')
    return value


def refresh_dictionary():
    added = 0
    for model in (Text, NovelProfile):
        for tags, genre in model.objects.values_list('tags', 'genre').iterator():
            for kind, value in [('tag', tags), ('genre', genre)]:
                for name in tokens(value):
                    if len(name) <= (100 if kind == 'genre' else 255):
                        _, created = VocabularyTerm.objects.get_or_create(
                            kind=kind, key=term_key(name), defaults={'name': name})
                        added += created
    return added


def merge_plan(source, target):
    if source.pk == target.pk or source.kind != target.kind or target.canonical_id:
        raise ValidationError('Wybierz inną nazwę główną tego samego rodzaju.')
    keys = {source.key, *source.aliases.values_list('key', flat=True)}
    field = 'tags' if source.kind == 'tag' else 'genre'
    changes = []
    for model in (Text, NovelProfile):
        for row in model.objects.order_by('pk').values('pk', field):
            before = row[field]
            parts = tokens(before)
            if not any(term_key(name) in keys for name in parts):
                continue
            after = ', '.join(tokens(', '.join(target.name if term_key(name) in keys else name for name in parts)))
            if len(after) > (100 if field == 'genre' else 5000):
                raise ValidationError(f'Rekord {row["pk"]}: nazwa docelowa przekroczy limit pola {field}.')
            if before != after:
                changes.append({'model': model._meta.label_lower, 'id': row['pk'], 'field': field, 'before': before, 'after': after})
    data = {'source': [source.pk, source.name, source.canonical_id], 'target': [target.pk, target.name], 'keys': sorted(keys), 'changes': changes}
    digest = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return changes, digest


def merge_token(source, target, user, digest):
    return signing.dumps({'source': source.pk, 'target': target.pk, 'user': user.pk, 'digest': digest}, salt='vocabulary-merge')


@transaction.atomic
def apply_merge(source_id, target_id, user, token):
    from core.permissions import is_coordinator
    from django.core.exceptions import PermissionDenied
    if not is_coordinator(user):
        raise PermissionDenied
    # Lock content before dictionary records, consistent with text forms.
    list(Text.objects.select_for_update().order_by('pk').values_list('pk', flat=True))
    list(NovelProfile.objects.select_for_update().order_by('pk').values_list('pk', flat=True))
    entries = {term.pk: term for term in VocabularyTerm.objects.select_for_update().order_by('pk')}
    source, target = entries[source_id], entries[target_id]
    changes, digest = merge_plan(source, target)
    try:
        data = signing.loads(token, salt='vocabulary-merge', max_age=3600)
    except signing.BadSignature as exc:
        raise ValidationError('Podgląd wygasł. Wygeneruj go ponownie.') from exc
    if data != {'source': source.pk, 'target': target.pk, 'user': user.pk, 'digest': digest}:
        raise ValidationError('Dane zmieniły się od podglądu. Nic nie zapisano; przygotuj nowy podgląd.')
    for change in changes:
        model = Text if change['model'] == 'texts.text' else NovelProfile
        obj = model.objects.get(pk=change['id'])
        setattr(obj, change['field'], change['after'])
        obj.save(update_fields=[change['field']])
    source.aliases.update(canonical=target)
    source.canonical = target
    source.save(update_fields=['canonical'])
    return len(changes)
