"""Repair the explicitly confirmed identity Agata Bisiecka / Wiktor Orłowski."""
import json
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.db.models import Q
from authors.models import Author, AuthorNote
from core.edit_versions import bump
from people.models import Person
from texts.models import Extract, ForeignAuthor, NovelProfile, Review, Text, Translator


def snapshot(obj):
    return {field.name: getattr(obj, field.attname) for field in obj._meta.concrete_fields}


class Command(BaseCommand):
    help = 'Scal Wiktora Orłowskiego z Agatą Bisiecką i ustaw jej pseudonim. Domyślnie tylko podgląd.'

    def add_arguments(self, parser):
        parser.add_argument('--apply', action='store_true', help='Zapisz sprawdzone scalenie.')

    def handle(self, *args, **options):
        try:
            with transaction.atomic():
                report = self.merge()
                report['mode'] = 'zapisano' if options['apply'] else 'podgląd – nic nie zapisano'
                if not options['apply']:
                    transaction.set_rollback(True)
        except (ValidationError, IntegrityError) as error:
            raise CommandError(f'Nie zapisano żadnych zmian: {error}') from error
        self.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    def merge(self):
        target_rows = list(Author.objects.select_for_update().filter(first_name__iexact='Agata', last_name__iexact='Bisiecka'))
        if len(target_rows) != 1:
            raise CommandError('Agata Bisiecka musi wskazywać dokładnie jeden profil; nic nie zapisano.')
        target = target_rows[0]
        alias = 'Wiktor Orłowski'
        if target.pseudonym.strip() and target.pseudonym.casefold().strip() != alias.casefold():
            raise CommandError('Agata Bisiecka ma inny pseudonim; nic nie zapisano.')
        sources = list(Author.objects.select_for_update().filter(
            Q(first_name__iexact='Wiktor', last_name__iexact='Orłowski') |
            Q(first_name__iexact=alias, last_name='')).exclude(pk=target.pk))
        if len(sources) > 1:
            raise CommandError('Znaleziono kilku Wiktorów Orłowskich; nic nie zapisano.')
        if Author.objects.filter(pseudonym__iexact=alias).exclude(pk__in=[target.pk]+[s.pk for s in sources]).exists():
            raise CommandError('Inny profil używa tego pseudonimu; nic nie zapisano.')
        report = {'target_id': target.pk, 'pseudonym': alias, 'before': snapshot(target), 'moved': {}, 'warnings': []}
        if not sources:
            if target.pseudonym != alias:
                target.pseudonym = alias
                target.save(update_fields=['pseudonym'])
            report['result'] = 'Brak osobnego profilu Wiktora Orłowskiego; ustawiono lub zachowano pseudonim.'
            return report
        source = sources[0]
        if source.pseudonym.strip() and source.pseudonym.casefold().strip() != alias.casefold():
            raise CommandError('Profil Wiktora ma inny pseudonim; nic nie zapisano.')
        report['source'] = snapshot(source)
        # Reject future unhandled relations rather than let CASCADE silently remove them.
        supported = {'authors.authornote', 'texts.text', 'texts.review', 'texts.extract', 'texts.novelprofile', 'people.person'}
        for rel in Author._meta.related_objects:
            if rel.related_model._meta.label_lower not in supported:
                if rel.related_model._default_manager.filter(**{rel.field.name: source}).exists():
                    raise CommandError(f'Nieobsługiwane powiązanie {rel.related_model._meta.label}; nic nie zapisano.')
        people = list(Person.objects.select_for_update().filter(author_profile_id__in=[target.pk, source.pk]))
        if len(people) > 1:
            raise CommandError('Oba profile autorów mają różne profile członków zespołu; nic nie zapisano.')
        for model in (ForeignAuthor, Translator):
            links = list(model.objects.select_for_update().filter(legacy_author_id__in=[target.pk, source.pk]))
            if len(links) > 1:
                raise CommandError('Obie osoby mają osobne profile tłumaczeń; nic nie zapisano.')
            for row in links:
                if row.legacy_author_id == source.pk:
                    row.legacy_author_id = target.pk
                    row.save(update_fields=['legacy_author_id'])
                    report['moved'][model._meta.label_lower] = [row.pk]
        # Snapshot private contact differences before deleting the duplicate.
        for field in ('email', 'phone_number'):
            old, other = getattr(target, field), getattr(source, field)
            if old and other and old.casefold() != other.casefold():
                report['warnings'].append(f'Różne {field}: zachowano dane Agaty; dane Wiktora pozostają w prywatnej notatce scalania.')
            elif not old and other:
                if field == 'email':
                    source.email = None
                    source.save(update_fields=['email'])
                setattr(target, field, other)
        target.pseudonym = alias
        target.has_contract = target.has_contract or source.has_contract
        target.is_blacklisted = target.is_blacklisted or source.is_blacklisted
        target.contact = target.contact and source.contact
        target.save()
        for model, field in ((Text, 'authors'), (NovelProfile, 'authors'), (Review, 'coauthors')):
            rows = list(model.objects.select_for_update().filter(**{field: source}).distinct())
            report['moved'][f'{model._meta.label_lower}.{field}'] = [row.pk for row in rows]
            for row in rows:
                manager = getattr(row, field)
                manager.add(target)
                manager.remove(source)
        for model, field in ((Review, 'author'), (AuthorNote, 'author'), (Person, 'author_profile')):
            rows = list(model.objects.select_for_update().filter(**{field: source}))
            report['moved'][f'{model._meta.label_lower}.{field}'] = [row.pk for row in rows]
            for row in rows:
                # Preserve historical submission fields, dates and identity snapshots.
                model.objects.filter(pk=row.pk).update(**{field: target})
                bump(model._meta.label_lower, row.pk, 'default')
        # A lead author must not be duplicated among coauthors.
        for review in Review.objects.filter(author=target, coauthors=target):
            review.coauthors.remove(target)
        report['extract_snapshots'] = []
        for row in Extract.objects.select_for_update().filter(author=source):
            other = Extract.objects.select_for_update().filter(author=target, recruitment=row.recruitment).first()
            report['extract_snapshots'].append(snapshot(row))
            if other:
                report['extract_snapshots'].append(snapshot(other))
                for field in ('title', 'accepted_titles', 'rejected_titles', 'submission_dates'):
                    left = getattr(other, field)
                    right = getattr(row, field)
                    if field == 'submission_dates':
                        left = left or other.submitted_at.isoformat()
                        right = right or row.submitted_at.isoformat()
                    setattr(other, field, '\n'.join(filter(None, [left, right])))
                other.save()  # Conflicting accepted/rejected titles abort the whole merge.
                row.delete()
            else:
                row.author = target
                row.save(update_fields=['author'])
        AuthorNote.objects.create(author=target, content='Scalenie potwierdzonej tożsamości Agata Bisiecka / Wiktor Orłowski. Dane przed scaleniem:\n' + json.dumps(report, ensure_ascii=False, default=str, indent=2))
        source.delete()
        report['result'] = 'Scalono powiązania; pozostał profil Agaty Bisieckiej z pseudonimem Wiktor Orłowski.'
        report['after'] = snapshot(target)
        return report
