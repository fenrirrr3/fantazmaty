"""Lektorzy i montażyści wyłącznie w spisie (jak ilustratorzy).

1. Osoby wpisane przy audiobooku bez wpisu w spisie dostają wpis (istniejący: ten sam e-mail albo
   jednoznacznie to samo imię i nazwisko) – audiobook wskazuje odtąd wpis spisu.
2. Wpisy spisu tracą powiązanie z kontem; konta i role zespołu zostają bez zmian.
Nic nie jest nadpisywane; migracja jest idempotentna.
"""
import unicodedata

from django.db import migrations, models


def key(value):
    value = ' '.join((value or '').split()).casefold().replace('ł', 'l')
    return ''.join(ch for ch in unicodedata.normalize('NFKD', value) if not unicodedata.combining(ch))


def link_typed_people(apps, schema_editor):
    Contributor = apps.get_model('core', 'AudioContributor')
    Audiobook = apps.get_model('core', 'Audiobook')
    created = linked = 0
    for role in ('narrator', 'engineer'):
        for audio in Audiobook.objects.filter(**{f'{role}_contact__isnull': True}).exclude(**{f'{role}_name': ''}).order_by('pk'):
            name = ' '.join(getattr(audio, f'{role}_name').split())
            email = (getattr(audio, f'{role}_email') or '').strip().lower()
            entry = Contributor.objects.filter(email__iexact=email).first() if email else None
            if entry is None:
                same = [c for c in Contributor.objects.all() if key(c.name) == key(name) and (not email or not c.email)]
                entry = same[0] if len(same) == 1 else None
            if entry is None:
                entry = Contributor.objects.create(name=name, email=email)
                created += 1
            setattr(audio, f'{role}_contact', entry)
            setattr(audio, f'{role}_name', entry.name)
            setattr(audio, f'{role}_email', entry.email)
            audio.save(update_fields=[f'{role}_contact', f'{role}_name', f'{role}_email'])
            linked += 1
    if linked:
        print(f'\n  Spis lektorów i montażu: przypisano {linked}, nowe wpisy {created}')


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0047_initial_replacements"),
    ]

    operations = [
        migrations.RunPython(link_typed_people, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name="audiocontributor",
            name="user",
        ),
        migrations.AddField(
            model_name="audiobook",
            name="coordinator_notes",
            field=models.TextField(blank=True, verbose_name="uwagi koordynatora"),
        ),
        migrations.AddField(
            model_name="audiocontributor",
            name="is_active",
            field=models.BooleanField(
                db_index=True,
                default=True,
                help_text="Wyłączenie ukrywa wpis w aktywnym spisie i przy nowych przypisaniach. Dotychczasowe nagrania pozostają.",
                verbose_name="aktywny",
            ),
        ),
        migrations.AlterField(
            model_name="audiobook",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Nieprzypisane"),
                    ("recording", "Nagrywanie"),
                    ("proofreading", "Korekta"),
                    ("corrections", "Nanoszenie poprawek"),
                    ("editing", "Montaż"),
                    ("published", "Opublikowane"),
                ],
                db_index=True,
                default="pending",
                max_length=24,
                verbose_name="status audiobooka",
            ),
        ),
    ]
