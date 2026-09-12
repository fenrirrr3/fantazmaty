from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("texts", "0001_initial")]
    operations = [migrations.AddField(
        model_name="review", name="is_hidden",
        field=models.BooleanField("ukryty", default=False, db_index=True),
    )]
