import csv
from django.core.management.base import BaseCommand
from people.models import Person

class Command(BaseCommand):
    help = 'Pomocniczy raport TSV zespołu z ID. Nie jest kopią bazy ani formatem importu.'
    def add_arguments(self, parser):
        parser.add_argument('--include-inactive', action='store_true', help='Uwzględnij również nieaktywne profile i konta.')

    def handle(self, *args, **options):
        writer = csv.writer(self.stdout, delimiter='\t')
        writer.writerow(['Nazwisko i imię','E-mail','E-mail Dropbox','Funkcja','ID osoby','ID konta','ID autora'])
        people = Person.objects.all() if options['include_inactive'] else Person.objects.active()
        for person in people.select_related('user').prefetch_related('roles').order_by('last_name','first_name','pk'):
            roles = [role.name for role in person.roles.all()]
            if person.user_id and person.user.is_superuser:roles.append('Superuser')
            for role in roles or ['']:
                writer.writerow([f'{person.last_name} {person.first_name}',person.email or '',person.dropbox_email or '',role,person.pk,person.user_id or '',person.author_profile_id or ''])
