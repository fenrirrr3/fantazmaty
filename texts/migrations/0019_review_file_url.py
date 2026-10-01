from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("texts", "0018_review_publication_detached")]
    operations = [migrations.AddField(model_name="review", name="file_url", field=models.URLField(blank=True, max_length=1000, verbose_name="folder Dropbox"))]
