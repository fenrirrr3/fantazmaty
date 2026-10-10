from django.db import migrations


def populate(apps, schema_editor):
    db = schema_editor.connection.alias
    Audio = apps.get_model('core', 'Audiobook')
    Contact = apps.get_model('core', 'AudioContributor')
    User = apps.get_model('auth', 'User')
    for audio in Audio.objects.using(db).order_by('pk').iterator():
        fields = []
        for role in ('narrator', 'engineer'):
            name = ' '.join(getattr(audio, f'{role}_name').split())
            email = getattr(audio, f'{role}_email').strip().lower()
            if not name or getattr(audio, f'{role}_contact_id'):
                continue
            matches = list(Contact.objects.using(db).filter(name__iexact=name, email__iexact=email)[:2])
            contact = matches[0] if matches else Contact.objects.using(db).create(name=name, email=email)
            if email and not contact.user_id:
                users = list(User.objects.using(db).filter(email__iexact=email)[:2])
                if len(users) == 1:
                    contact.user_id = users[0].pk
                    contact.save(update_fields=['user_id'], using=db)
            setattr(audio, f'{role}_contact_id', contact.pk)
            fields.append(f'{role}_contact')
        if fields:
            audio.save(update_fields=fields, using=db)


class Migration(migrations.Migration):
    dependencies = [('core', '0030_contacts_and_team_status')]
    operations = [migrations.RunPython(populate, migrations.RunPython.noop)]
