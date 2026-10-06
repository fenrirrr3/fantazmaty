import hashlib
import unicodedata
from django.db import migrations


def seed(apps, schema_editor):
    Text = apps.get_model('texts', 'Text')
    Term = apps.get_model('texts', 'VocabularyTerm')
    alias = schema_editor.connection.alias
    seen = set()
    for tags, genre in Text.objects.using(alias).values_list('tags', 'genre').iterator():
        for kind, value in [('tag', tags), ('genre', genre)]:
            for part in (value or '').replace('\r', '\n').replace('\n', ',').split(','):
                name = ' '.join(unicodedata.normalize('NFKC', part).split())
                if not name or len(name) > (100 if kind == 'genre' else 255):
                    continue
                key = hashlib.sha256(name.casefold().encode('utf-8')).hexdigest()
                if (kind, key) not in seen:
                    Term.objects.using(alias).get_or_create(kind=kind, key=key, defaults={'name': name})
                    seen.add((kind, key))


class Migration(migrations.Migration):
    dependencies = [('texts', '0027_novelprofile_vocabularyterm_anthology_is_novel_and_more')]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
