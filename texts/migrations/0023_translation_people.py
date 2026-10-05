from django.db import migrations, models


def fields():
    return [
        ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
        ('first_name', models.CharField('imię', max_length=100)),
        ('last_name', models.CharField('nazwisko', max_length=100)),
        ('pseudonym', models.CharField('pseudonim', max_length=100, blank=True)),
        ('email', models.EmailField('adres e-mail', max_length=254, blank=True)),
        ('phone_number', models.CharField('numer telefonu', max_length=50, blank=True)),
        ('notes', models.TextField('notatki', blank=True)),
        ('legacy_author_id', models.PositiveBigIntegerField(null=True, unique=True, editable=False)),
    ]


def move_people(apps, schema_editor):
    db = schema_editor.connection.alias
    Text = apps.get_model('texts', 'Text')
    Record = apps.get_model('texts', 'TextTranslation')
    Foreign = apps.get_model('texts', 'ForeignAuthor')
    Translator = apps.get_model('texts', 'Translator')

    def copy(model, author):
        profile, _ = model.objects.using(db).get_or_create(legacy_author_id=author.pk, defaults={
            'first_name': author.first_name, 'last_name': author.last_name,
            'pseudonym': author.pseudonym, 'email': author.email or '',
            'phone_number': author.phone_number,
        })
        return profile

    for text in Text.objects.using(db).filter(anthology__is_translated=True).iterator():
        record, _ = Record.objects.using(db).get_or_create(text_id=text.pk)
        for author in text.authors.all():
            record.foreign_authors.add(copy(Foreign, author))
        text.authors.clear()
    # Include retained translation records on anthologies whose flag was turned off.
    for record in Record.objects.using(db).all().iterator():
        for author in record.translators.all():
            record.new_translators.add(copy(Translator, author))


class Migration(migrations.Migration):
    dependencies = [('texts', '0022_anthology_translations')]
    operations = [
        migrations.CreateModel(name='ForeignAuthor', fields=fields(), options={
            'abstract': False, 'ordering': ('last_name', 'first_name', 'pk'),
            'verbose_name': 'autor zagraniczny', 'verbose_name_plural': 'autorzy zagraniczni'}),
        migrations.CreateModel(name='Translator', fields=fields()+[
            ('language', models.CharField('język / języki pracy', max_length=255, blank=True, help_text='Np. angielski, niemiecki.'))
        ], options={'abstract': False, 'ordering': ('last_name', 'first_name', 'pk'),
                    'verbose_name': 'tłumacz', 'verbose_name_plural': 'tłumacze'}),
        migrations.AddField(model_name='texttranslation', name='foreign_authors', field=models.ManyToManyField(
            to='texts.foreignauthor', related_name='translations', blank=True, verbose_name='autor zagraniczny')),
        migrations.AddField(model_name='texttranslation', name='new_translators', field=models.ManyToManyField(
            to='texts.translator', related_name='translations', blank=True, verbose_name='tłumacz')),
        migrations.RunPython(move_people),
        migrations.RemoveField(model_name='texttranslation', name='translators'),
        migrations.RenameField(model_name='texttranslation', old_name='new_translators', new_name='translators'),
    ]
