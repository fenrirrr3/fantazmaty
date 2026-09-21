from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("core", "0010_editrevision")]
    operations = [migrations.DeleteModel(name="DiscordDispatch")]
