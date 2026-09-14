import csv
from django.core.management.base import BaseCommand
from people.models import Person

class Command(BaseCommand):
    help = 'Eksport TSV zespołu z trwałymi ID, do ponownego importu.'
    def handle(self, *args, **options):
        writer = csv.writer(self.stdout, delimiter='\t')
        writer.writerow(['Nazwisko i imię','E-mail','E-mail Dropbox','Funkcja','ID osoby','ID konta','ID autora'])
        for person in Person.objects.filter(is_active=True).select_related('user').prefetch_related('roles').order_by('last_name','first_name','pk'):
            roles = list(person.roles.values_list('name',flat=True))
            if person.user_id and person.user.is_superuser:roles.append('Superuser')
            for role in roles or ['']:
                writer.writerow([f'{person.last_name} {person.first_name}',person.email or '',person.dropbox_email or '',role,person.pk,person.user_id or '',person.author_profile_id or ''])
