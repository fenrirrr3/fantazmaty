"""Uruchom przez manage.py shell < import_ballady.py. Nie kasuje ani nie nadpisuje danych."""
from datetime import date
from django.core.exceptions import ValidationError
from django.core.management.base import CommandError
from django.db import transaction
from authors.models import Author
from people.models import Person
from texts.models import Anthology, Text
from workflow.models import WorkflowStage as S, WorkflowRoleAssignment as A
from workflow.import_context import importing_completed

BOOK = 'Ballady ze spalonego traktu'
SOURCE = 'ballady-2026-09-29-v1'
MAILS = {
    'aneta': 'anetastawiszynska.marciniak@gmail.com',
    'sg': 'magdalenafilozofia@gmail.com',
    'zw': 'mzwolinska.korekta@gmail.com',
    'mk': 'm.kowalewicz@protonmail.com',
    'bs': 'beata.sagan.szendzielorz@gmail.com',
    'kp': 'katarzyna.pikosz@gmail.com',
}
# Tytuł, znaki, status źródłowy, start, koniec, UWAGI, redaktorzy po kolei, koordynator, K1, W1.
ROWS = [
('Preludium',37290,'first_verification','2026-07-05','2026-09-16','',['aneta','sg','zw'],'sg','bs','kp'),
('Lekcja o demonach',17236,'author_editing','2025-08-31','','6.07 wróciło do Alicji',['aneta'],'sg','',''),
('Lora Ley',19726,'editing_control','2026-05-14','2026-06-10','',['aneta'],'sg','',''),
('Kolorowych koszmarów',14619,'first_verification','2026-09-22','2026-09-24','',['sg'],'mk','zw','kp'),
('Dysonans',37892,'author_editing','2026-04-07','','',['sg'],'','',''),
('Jałmużna dla wędrowca',35775,'author_editing','2026-05-14','','',['zw'],'','',''),
('Księżyc nad świętym gajem',50263,'author_editing','2026-05-14','','',['zw'],'','',''),
('Zamojska Fuga',36130,'author_editing','2026-05-14','','',['zw'],'','',''),
('Krytyka literacka',13239,'ready_for_editing','','','Alicja ma do tego zajrzeć po przerobieniu redakcji 1-8',[],'','',''),
('Danse Macabre',35520,'author_editing','2026-05-21','','Po red u ALicji',['aneta'],'','',''),
('Dzięciecy hejnał',9089,'author_editing','2026-09-03','','28.08 wrócił od Alicji',['zw'],'','',''),
('Babski wieczór',43821,'author_editing','2026-08-11','','Po red u Alicji',['aneta'],'','',''),
('Al vol!',22891,'author_editing','2026-08-13','','',['zw'],'','',''),
('Nie śpię, bo słucham oud',31080,'author_editing','2026-08-16','','',['aneta'],'','',''),
('An droe. Brama donikąd',43929,'author_editing','2026-09-10','','',['zw'],'','',''),
]


def one(query, label):
    values = list(query[:2])
    if len(values) != 1:
        raise CommandError(f'{label}: wymagany dokładnie jeden rekord, znaleziono {len(values)} (2 oznacza co najmniej 2). Niczego nie zapisano.')
    return values[0]


def run():
    created = []
    skipped = []
    with transaction.atomic():
        author = one(Author.objects.select_for_update().filter(first_name__iexact='Alicja', last_name__iexact='Janusz'), 'Autorka Alicja Janusz')
        users = {}
        for key, email in MAILS.items():
            profile = one(Person.objects.select_for_update().select_related('user').filter(email__iexact=email), f'Profil {email}')
            if not profile.user_id:
                raise CommandError(f'Profil {email} nie ma powiązanego konta. Niczego nie zapisano.')
            users[key] = profile.user
        books = list(Anthology.objects.select_for_update().filter(title__iexact=BOOK)[:2])
        if len(books) > 1:
            raise CommandError('Znaleziono kilka antologii o tej nazwie. Niczego nie zapisano.')
        book = books[0] if books else Anthology(title=BOOK, status=Anthology.Status.IN_PREPARATION)
        if book.status != Anthology.Status.IN_PREPARATION:
            raise CommandError('Antologia nie jest w przygotowaniu. Niczego nie zapisano.')
        book.full_clean(); book.save()
        # Sprawdź całą paczkę przed tworzeniem tekstów. Powtórzenie własnego importu nic nie zmienia.
        pending = []
        for number, row in enumerate(ROWS, 1):
            existing = Text.objects.select_for_update().filter(import_source=SOURCE, import_source_row=number).first()
            if existing:
                if existing.anthology_id != book.pk or existing.title != row[0]:
                    raise CommandError(f'Zmieniona tożsamość wcześniej importowanego tekstu: {row[0]}.')
                skipped.append(row[0]); continue
            if Text.objects.filter(anthology=book,title__iexact=row[0]).exists():
                raise CommandError(f'Tekst „{row[0]}” już istnieje poza tym importem. Nie nadpisano go; cała operacja wycofana.')
            pending.append((number,row))
        token = importing_completed.set(True)
        try:
            for number,row in pending:
                title,length,status,start,end,note,editors,coord,proof,verifier = row
                text = Text(title=title,length=length,anthology=book,coordinator_note=note,import_source=SOURCE,import_source_row=number)
                text.full_clean();text.save();text.authors.add(author)
                assignments = {}
                def assignment(role, keys):
                    for n,key in enumerate(keys,1):
                        a=A(text=text,role=role,assigned_to=users[key],execution_number=n,is_current=n==len(keys),assigned_at=None)
                        a.full_clean();a.save()
                        A.objects.filter(pk=a.pk).update(assigned_at=None)
                        assignments[(role,n)]=a
                    return assignments.get((role,len(keys)))
                editor=assignment('editor',editors)
                coordinator=assignment('editing_coordinator',[coord] if coord else [])
                proofreader=assignment('proofreader_1',[proof] if proof else [])
                check=assignment('verifier_1',[verifier] if verifier else [])
                def completed(kind,a,n=1,dates=False):
                    s=S(text=text,stage_type=kind,assignment=a,iteration=n,execution_number=n,
                        is_current=a.is_current and not (status=='first_verification' and kind in ('editing_control','first_proofreading')),
                        is_released=not (status=='first_verification' and kind in ('editing_control','first_proofreading')),is_completed=True,imported_completed=True,
                        started_at=date.fromisoformat(start) if dates and start else None,
                        ended_at=date.fromisoformat(end) if dates and end else None)
                    s.full_clean();s.save()
                for n in range(1,len(editors)+1):
                    completed('editing',assignments[('editor',n)],n)
                if status in ('first_verification','editing_control') and coordinator:
                    completed('editing_control',coordinator,dates=status=='editing_control')
                if proofreader:completed('first_proofreading',proofreader)
                if check:completed('first_verification',check,dates=status=='first_verification')
                next_kind='author_editing' if status=='first_verification' else 'first_proofreading' if status=='editing_control' else status
                active=S(text=text,stage_type=next_kind,assignment=editor if next_kind=='author_editing' else None,
                    started_at=date.fromisoformat(end) if status=='first_verification' and end else date.fromisoformat(start) if status=='author_editing' and start else None)
                active.full_clean();active.save()
                created.append(title)
        finally:
            importing_completed.reset(token)
    print(f'OK: dodano {len(created)} tekstów; pominięto {len(skipped)} już zaimportowanych. Autorzy i konta pozostawione bez zmian.')
    for title in created:print(' + '+title)

run()
