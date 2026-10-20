"""Indeksy dla tabel rosnących bez ograniczeń: historia workflow per tekst i kolejka powiadomień Discord."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0057_discord_stage_started'),
        ('texts', '0043_anthology_discord_notifications'),
    ]

    operations = [
        migrations.AddIndex(
            model_name='discordnotification',
            index=models.Index(fields=['status', 'created_at'], name='discord_status_time_idx'),
        ),
        migrations.AddIndex(
            model_name='workflowevent',
            index=models.Index(fields=['text', 'created_at', 'id'], name='workflow_text_time_idx'),
        ),
    ]
