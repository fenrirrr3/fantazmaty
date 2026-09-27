from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [('core', '0015_mailboxdownload')]
    operations = [migrations.AddField(
        model_name='mailboxconnection', name='recruitment_subjects',
        field=models.TextField(blank=True, default='', verbose_name='nabory do filtrowania tematów', help_text='Każdy nabór w osobnym wierszu, bez cudzysłowów i bez prefiksu Nabór:, np. Na pokład, psubraty. Filtr dopasuje temat zawierający Nabór: „Na pokład, psubraty”.'))]
