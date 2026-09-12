from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("authors", "0001_initial")]
    operations = [
        migrations.CreateModel(
            name="BlacklistedAuthor",
            fields=[],
            options={
                "verbose_name": "autor na czarnej liście",
                "verbose_name_plural": "czarna lista autorów",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("authors.author",),
        ),
    ]
