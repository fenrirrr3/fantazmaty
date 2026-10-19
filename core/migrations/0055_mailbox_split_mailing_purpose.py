"""Wysyłka plików autorom jako osobne przeznaczenie skrzynki (zamiast pola split_mailing)."""
from django.db import migrations, models

SUBMISSIONS, RECRUITMENT, SPLIT_MAILING = 'submissions', 'recruitment', 'split_mailing'


def is_import_mailbox(box):
    """Skrzynka importu zgłoszeń („teksty” / teksty@…) albo rekrutacji – musi dalej odbierać pocztę."""
    if box.purpose == RECRUITMENT:
        return True
    return box.name.strip().casefold() == 'teksty' or box.username.casefold().startswith('teksty@')


def purpose_from_flag(apps, schema_editor):
    """Skrzynki z zaznaczoną wysyłką stają się skrzynkami wysyłkowymi – poza skrzynkami importu i rekrutacji.

    Skrzynka importu zachowuje przeznaczenie (i dane SMTP); do wysyłki trzeba dodać nową skrzynkę.
    """
    MailboxConnection = apps.get_model('core', 'MailboxConnection')
    for box in MailboxConnection.objects.filter(split_mailing=True):
        if not is_import_mailbox(box):
            box.purpose = SPLIT_MAILING
            box.save(update_fields=['purpose'])


def flag_from_purpose(apps, schema_editor):
    """Cofnięcie: skrzynka wysyłkowa wraca jako skrzynka zgłoszeń z zaznaczoną wysyłką."""
    MailboxConnection = apps.get_model('core', 'MailboxConnection')
    MailboxConnection.objects.filter(purpose=SPLIT_MAILING).update(purpose=SUBMISSIONS, split_mailing=True)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0054_mailbox_snapshot_and_smtp"),
    ]

    operations = [
        migrations.AlterField(
            model_name="mailboxconnection",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("submissions", "Zgłoszenia tekstów"),
                    ("recruitment", "Rekrutacja do zespołu"),
                    ("split_mailing", "Wysyłka plików autorom"),
                ],
                default="submissions",
                max_length=20,
                verbose_name="przeznaczenie",
            ),
        ),
        migrations.AlterField(
            model_name="mailboxconnection",
            name="host",
            field=models.CharField(
                blank=True,
                help_text="Nazwa serwera, np. imap.example.com, bez https://. Niepotrzebny przy wysyłce plików autorom.",
                max_length=253,
                verbose_name="serwer IMAP",
            ),
        ),
        migrations.AlterField(
            model_name="mailboxconnection",
            name="smtp_host",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Np. smtp.example.com, bez https://. Tylko przy wysyłce plików autorom.",
                max_length=253,
                verbose_name="serwer SMTP (wysyłka)",
            ),
        ),
        migrations.RunPython(purpose_from_flag, flag_from_purpose),
        migrations.RemoveField(
            model_name="mailboxconnection",
            name="split_mailing",
        ),
    ]
