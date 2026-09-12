from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('people', '0002_person_previous_data'),
        ('core', '0005_recruitment_department_recruitment_first_name_and_more'),
    ]
    operations = [migrations.DeleteModel(name='Rekrutacja')]
