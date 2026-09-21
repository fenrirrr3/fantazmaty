from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("texts", "0011_alter_anthology_status")]
    operations = [
        migrations.RemoveConstraint(model_name="text", name="unique_text_historical_source"),
        migrations.RenameField(model_name="text", old_name="historical_source", new_name="import_source"),
        migrations.RenameField(model_name="text", old_name="historical_source_row", new_name="import_source_row"),
        migrations.AlterField(model_name="text", name="import_source", field=models.CharField("źródło importu", max_length=100, blank=True, default="", editable=False)),
        migrations.AlterField(model_name="text", name="import_source_row", field=models.PositiveIntegerField("LP importu", null=True, blank=True, editable=False)),
        migrations.AddConstraint(model_name="text", constraint=models.UniqueConstraint(fields=("import_source", "import_source_row"), name="unique_text_import_source")),
        migrations.RemoveField(model_name="text", name="is_historical"),
        migrations.DeleteModel(name="HistoricalTextAssignment"),
    ]
