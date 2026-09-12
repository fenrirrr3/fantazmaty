"""Zachowanie istniejących zgłoszeń przy przejściu na autor + antologia."""
from django.db import migrations, models
from django.utils import timezone


def merge_existing(apps, schema_editor):
    Extract = apps.get_model('texts', 'Extract')
    db = schema_editor.connection.alias
    groups = {}
    def key(value):
        return ' '.join(value.split()).casefold()
    def values(value):
        import re
        return [v.strip() for v in re.split(r'[;\r\n]+', value or '') if v.strip()]
    def union(items):
        result, seen = [], set()
        for value in items:
            if key(value) not in seen:
                result.append(value)
                seen.add(key(value))
        return result
    for item in Extract.objects.using(db).order_by('pk'):
        groups.setdefault((item.author_id, key(item.recruitment)), []).append(item)
    planned = []
    for items in groups.values():
        # Nie odrzucaj kontaktu z jednego ze scalanych rekordów.
        for field in ('full_name', 'email', 'phone_number'):
            distinct = {key(getattr(i, field)) for i in items if getattr(i, field)}
            if len(distinct) > 1:
                raise RuntimeError(f'Ekstrakty: sprzeczne pole {field} w rekordach {[i.pk for i in items]}. Ujednolić dane przed migracją.')
        titles = union(v for i in items for v in values(i.title))
        accepted = union(v for i in items for v in (values(i.accepted_titles) or (values(i.title) if i.status == 'accepted' else [])))
        rejected = union(v for i in items for v in (values(i.rejected_titles) or (values(i.title) if i.status == 'rejected' else [])))
        if {key(v) for v in accepted} & {key(v) for v in rejected}:
            raise RuntimeError(f'Ekstrakty: sprzeczne decyzje w rekordach {[i.pk for i in items]}. Rozstrzygnąć przed migracją.')
        dates = sorted(set(v for i in items for v in (values(i.submission_dates) or [i.submitted_at.isoformat()])))
        data = dict(title='\n'.join(titles), accepted_titles='\n'.join(accepted), rejected_titles='\n'.join(rejected),
            submission_dates='\n'.join(dates), submitted_at=dates[0],
            recruitment=' '.join(items[0].recruitment.split()),
            status='mixed' if accepted and rejected else 'accepted' if accepted else 'rejected' if rejected else 'new')
        for field in ('full_name', 'email', 'phone_number'):
            data[field] = next((getattr(i, field) for i in items if getattr(i, field)), '')
        planned.append((items, data))
    # Cała walidacja przed modyfikowaniem pierwszego rekordu.
    for items, data in planned:
        Extract.objects.using(db).filter(pk=items[0].pk).update(**data)
        Extract.objects.using(db).filter(pk__in=[i.pk for i in items[1:]]).delete()


class Migration(migrations.Migration):
    dependencies = [('texts', '0003_extract'), ('core', '0004_case_insensitive_comparisons')]
    operations = [
        migrations.AlterModelOptions(name='extract', options={'ordering': ('recruitment', 'full_name', 'pk'), 'verbose_name': 'udział autora w Ekstraktach', 'verbose_name_plural': 'ekstrakty'}),
        migrations.AddField(model_name='extract', name='accepted_titles', field=models.TextField('przyjęte teksty', blank=True)),
        migrations.AddField(model_name='extract', name='rejected_titles', field=models.TextField('odrzucone teksty', blank=True)),
        migrations.AddField(model_name='extract', name='submission_dates', field=models.TextField('daty nadesłania', blank=True, help_text='Daty RRRR-MM-DD, osobno w wierszach lub rozdzielone średnikami.')),
        migrations.AlterField(model_name='extract', name='recruitment', field=models.CharField('antologia / nabór', max_length=255)),
        migrations.AlterField(model_name='extract', name='status', field=models.CharField('rodzaj decyzji', max_length=20, choices=[('new', 'Bez decyzji'), ('accepted', 'Tylko przyjęte'), ('rejected', 'Tylko odrzucone'), ('mixed', 'Przyjęte i odrzucone')], default='new', editable=False)),
        migrations.AlterField(model_name='extract', name='submitted_at', field=models.DateField('pierwsza data nadesłania', default=timezone.localdate)),
        migrations.AlterField(model_name='extract', name='title', field=models.TextField('nadesłane tytuły', help_text='Jeden tytuł w wierszu lub tytuły rozdzielone średnikami.')),
        migrations.RunPython(merge_existing, atomic=True),
        migrations.AddConstraint(model_name='extract', constraint=models.UniqueConstraint(fields=('author', 'recruitment'), name='unique_extract_author_recruitment')),
    ]
