"""Reguła „Zakończenie etapu” ogłasza też rozpoczęcia: nowa nazwa i tag [zdarzenie] w domyślnej treści.

Zmieniamy tylko treść identyczną z dotychczasową domyślną – treści wpisane w panelu zostają bez zmian.
"""
from django.db import migrations, models

OLD = 'Zakończono etap „[etap]”: „[tytuł]” – [autor] ([antologia]). Wykonanie: [osoba].'
NEW = '[zdarzenie] etap „[etap]”: „[tytuł]” – [autor] ([antologia]). Wykonanie: [osoba].'


def replace_default(apps, schema_editor, old, new):
    Template = apps.get_model('core', 'NotificationTemplate')
    db = schema_editor.connection.alias
    Template.objects.using(db).filter(rule__trigger='stage_completed', content=old).update(content=new)


def forwards(apps, schema_editor):
    replace_default(apps, schema_editor, OLD, NEW)


def backwards(apps, schema_editor):
    replace_default(apps, schema_editor, NEW, OLD)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0056_social_media_quote"),
    ]

    operations = [
        migrations.AlterField(
            model_name="discordnotification",
            name="trigger",
            field=models.CharField(
                choices=[
                    ("review_new", "Nowy tekst do recenzji"),
                    ("ready_for_editing", "Nowy tekst do redakcji"),
                    ("first_proofreading", "Nowy tekst do pierwszej korekty"),
                    ("third_proofreading", "Nowy tekst do trzeciej korekty"),
                    ("first_verification", "Nowy tekst do pierwszej weryfikacji"),
                    ("second_verification", "Nowy tekst do drugiej weryfikacji"),
                    ("third_verification", "Nowy tekst do trzeciej weryfikacji"),
                    (
                        "stage_completed",
                        "Rozpoczęcie i zakończenie etapu (wszystkie etapy)",
                    ),
                    (
                        "stage_waiting",
                        "Tekst nadal czeka na osobę (po 7, 14 i 21 dniach)",
                    ),
                    ("audiobook_production", "Produkcja audiobooka (wszystkie zmiany)"),
                ],
                db_index=True,
                max_length=32,
                verbose_name="zdarzenie",
            ),
        ),
        migrations.AlterField(
            model_name="notificationrule",
            name="trigger",
            field=models.CharField(
                choices=[
                    ("review_new", "Nowy tekst do recenzji"),
                    ("ready_for_editing", "Nowy tekst do redakcji"),
                    ("first_proofreading", "Nowy tekst do pierwszej korekty"),
                    ("third_proofreading", "Nowy tekst do trzeciej korekty"),
                    ("first_verification", "Nowy tekst do pierwszej weryfikacji"),
                    ("second_verification", "Nowy tekst do drugiej weryfikacji"),
                    ("third_verification", "Nowy tekst do trzeciej weryfikacji"),
                    (
                        "stage_completed",
                        "Rozpoczęcie i zakończenie etapu (wszystkie etapy)",
                    ),
                    (
                        "stage_waiting",
                        "Tekst nadal czeka na osobę (po 7, 14 i 21 dniach)",
                    ),
                    ("audiobook_production", "Produkcja audiobooka (wszystkie zmiany)"),
                ],
                editable=False,
                max_length=32,
                unique=True,
                verbose_name="kiedy",
            ),
        ),
        migrations.RunPython(forwards, backwards),
    ]
