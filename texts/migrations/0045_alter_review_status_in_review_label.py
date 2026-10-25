# Status recenzji „in_review”: etykieta „W trakcie” zamiast „W recenzjach” (wartość w bazie bez zmian).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('texts', '0044_text_take_lock'),
    ]

    operations = [
        migrations.AlterField(
            model_name='review',
            name='status',
            field=models.CharField(choices=[('new', 'Do recenzji'), ('in_review', 'W trakcie'), ('to_decide', 'Do decyzji'), ('accepted', 'Przyjęty'), ('rejected', 'Odrzucony'), ('withdrawn', 'Wycofany')], db_index=True, default='new', max_length=20, verbose_name='status'),
        ),
    ]
