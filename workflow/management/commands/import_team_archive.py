"""One atomic preview/commit for prepared people, authors, texts and completed work."""
import json
from collections import Counter
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, IntegrityError
from authors.models import Author
from people.models import Person
from texts.models import Text, Anthology
from workflow.completed_import import import_completed_workflow


class Command(BaseCommand):
    help='Wspólny podgląd/import osób, autorów, tekstów i zakończonych etapów. Zapis tylko z --commit.'
    def add_arguments(self,parser):
        parser.add_argument('file');parser.add_argument('--commit',action='store_true')
    def handle(self,*args,**options):
        self.counts=Counter();conflicts=[]
        try:
            data=json.loads(Path(options['file']).read_text(encoding='utf-8-sig'))
            if not isinstance(data,dict) or set(data)!={'schema_version','source','people','authors','texts'} or data['schema_version']!=1:
                raise ValueError('Wymagane schema_version=1, source, people, authors, texts.')
            source=data['source']
            if not isinstance(source,str) or not source.strip() or len(source)>100:raise ValueError('Nieprawidłowe source.')
            for key in ('people','authors','texts'):
                if not isinstance(data[key],list):raise ValueError(f'{key} musi być listą.')
            with transaction.atomic():
                for section,handler in [('people',self.person),('authors',self.author),('texts',lambda row:self.text(row,source))]:
                    seen=set()
                    for n,row in enumerate(data[section],1):
                        before=self.counts.copy()
                        try:
                            with transaction.atomic():
                                if not isinstance(row,dict):raise ValueError('Rekord musi być obiektem.')
                                key=row.get('source_row') if section=='texts' else str(row.get('email','')).strip().casefold()
                                if key in seen:raise ValueError('Powtórzony identyfikator w pliku.')
                                seen.add(key);handler(row)
                        except (ValueError,TypeError,KeyError,ValidationError,IntegrityError) as exc:
                            self.counts=before;conflicts.append(f'{section} [{n}]: {exc}')
                if conflicts or not options['commit']:transaction.set_rollback(True)
            report={'mode':'wycofano' if conflicts else 'zapis' if options['commit'] else 'podglad','counts':dict(self.counts),'conflicts':conflicts,'new_accounts':'nieaktywne, bez hasła; import nie nadaje ról ani superusera'}
            self.stdout.write(json.dumps(report,ensure_ascii=False,indent=2))
            if conflicts:raise CommandError('Nie zapisano żadnych zmian. Popraw konflikty z zestawienia.')
        except (OSError,ValueError,TypeError,KeyError) as exc:raise CommandError(str(exc)) from exc

    def identity(self,model,row,extra=()):
        if set(row)-{'email','first_name','last_name',*extra}:raise ValueError('Nieznane pola.')
        if not all(isinstance(row.get(k),str) and row[k].strip() for k in ('email','first_name','last_name')):raise ValueError('Wymagane imię, nazwisko i e-mail.')
        email=row['email'].strip().lower()
        matches=list(model.objects.select_for_update().filter(email__iexact=email)[:2])
        if len(matches)>1:raise ValueError('E-mail wskazuje kilka rekordów; uporządkuj je przed importem.')
        obj=matches[0] if matches else None
        if obj and (obj.first_name.strip()!=row['first_name'].strip() or obj.last_name.strip()!=row['last_name'].strip()):raise ValueError('Inne imię/nazwisko przy tym adresie; wymagane ręczne rozstrzygnięcie tożsamości.')
        return obj,email

    def person(self,row):
        user,email=self.identity(get_user_model(),row)
        person,mapped=self.identity(Person,row)
        if person and person.user_id and (not user or person.user_id!=user.pk):raise ValueError('Profil ma inne powiązane konto.')
        if user and Person.objects.filter(user=user).exclude(pk=person.pk if person else None).exists():raise ValueError('Konto ma inny profil.')
        if not user:
            user=get_user_model()(username=email,email=email,first_name=row['first_name'].strip(),last_name=row['last_name'].strip(),is_active=False)
            user.set_unusable_password();user.full_clean();user.save();self.counts['nowe_konta']+=1
        if not person:
            person=Person(first_name=row['first_name'].strip(),last_name=row['last_name'].strip(),email=email,user=user,is_active=False)
            person.full_clean();person.save();self.counts['nowe_osoby']+=1
        elif person.user_id is None:
            person.user=user;person.full_clean();person.save(update_fields=['user']);self.counts['powiazane_konta']+=1
        else:self.counts['osoby_bez_zmian']+=1

    def author(self,row):
        author,email=self.identity(Author,row,('phone_number','pseudonym','has_contract','contact'))
        new=author is None
        if new:author=Author(first_name=row['first_name'].strip(),last_name=row['last_name'].strip(),email=email)
        changed=False
        for field in ('phone_number','pseudonym','has_contract','contact'):
            value=row.get(field)
            if value is None or value=='':continue
            if field in ('has_contract','contact') and type(value) is not bool:raise ValueError('Flagi muszą mieć wartości true/false.')
            if field in ('phone_number','pseudonym'):
                if not isinstance(value,str):raise ValueError('Oczekiwano tekstu.')
                value='' if value=='__CLEAR__' else value.strip()
            if getattr(author,field)!=value:changed=True;setattr(author,field,value)
        author.full_clean();author.save()
        self.counts['nowi_autorzy' if new else 'zaktualizowani_autorzy' if changed else 'autorzy_bez_zmian']+=1

    def text(self,row,source):
        if set(row)!={'source_row','title','anthology','length','authors','stages','next_stage'}:raise ValueError('Nieprawidłowe pola tekstu.')
        if type(row['source_row']) is not int or row['source_row']<1:raise ValueError('source_row musi być dodatnią liczbą.')
        if type(row['length']) is not int or row['length']<1:raise ValueError('Długość musi być dodatnią liczbą całkowitą.')
        if not isinstance(row['title'],str) or not row['title'].strip():raise ValueError('Brak tytułu.')
        if not isinstance(row['anthology'],str) or not row['anthology'].strip():raise ValueError('Brak antologii.')
        anthologies=list(Anthology.objects.filter(title__iexact=row['anthology'].strip())[:2])
        if len(anthologies)>1:raise ValueError('Niejednoznaczna antologia.')
        anthology=anthologies[0] if anthologies else Anthology(title=row['anthology'].strip())
        if not anthology.pk:anthology.full_clean();anthology.save();self.counts['nowe_antologie']+=1
        if not isinstance(row['authors'],list) or not row['authors']:raise ValueError('Wymagana lista e-maili autorów.')
        authors=[]
        for email in row['authors']:
            author=Author.objects.filter(email__iexact=email).first()
            if not author:raise ValueError(f'Nie znaleziono autora {email}.')
            authors.append(author.pk)
        text=Text.objects.select_for_update().filter(import_source=source,import_source_row=row['source_row']).first()
        if text:
            if text.title!=row['title'].strip() or text.anthology_id!=anthology.pk or text.length!=row['length'] or set(text.authors.values_list('pk',flat=True))!=set(authors):raise ValueError('Tekst zmienił się; nie nadpisano istniejących danych.')
        else:
            if Text.objects.filter(title__iexact=row['title'].strip(),anthology=anthology).exists():raise ValueError('Podobny identyfikator tekstu bez tego źródła; rozstrzygnij dopasowanie.')
            text=Text(title=row['title'].strip(),anthology=anthology,length=row['length'],import_source=source,import_source_row=row['source_row'])
            text.full_clean();text.save();text.authors.set(authors);self.counts['nowe_teksty']+=1
        added=import_completed_workflow(text_id=text.pk,stages=row['stages'],next_stage=row['next_stage'])
        self.counts['nowe_workflow' if added else 'workflow_bez_zmian']+=1
        if added:
            self.counts['nowe_etapy']=self.counts.get('nowe_etapy',0)+text.workflow_stages.count()
            self.counts['nowe_przypisania']=self.counts.get('nowe_przypisania',0)+text.workflow_role_assignments.count()
