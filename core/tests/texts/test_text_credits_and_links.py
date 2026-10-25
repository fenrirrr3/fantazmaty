"""Strony tekstów i antologii: stopka tekstu (zasługi), lista osób według roli, zadanie typografii
okładki i stałe adresy stron."""
from django.contrib.auth import get_user_model
from django.urls import reverse

from core.selectors.people import role_names_context
from core.supervision import text_credit_groups
from fantazmaty.testing import create_superuser
from fantazmaty.testing.workflow import StatusAssignmentFixtures
from people.models import Person, Role
from texts.models import Anthology, Review, ReviewAssignment, Text
from fantazmaty.testing.history import import_completed_workflow


class TextPagesTests(StatusAssignmentFixtures):
    def test_text_credits_include_only_this_text_completed_work_and_reviews(self):
        book = Anthology.objects.create(title='Antologia', cover_author='Inna osoba')
        first = Text.objects.create(title='Pierwszy', anthology=book, length=100)
        second = Text.objects.create(title='Drugi', anthology=book, length=100)
        for text, name in ((first, 'Ala'), (second, 'Ewa')):
            user = create_superuser(name, name + '@example.com', 'password')
            Person.objects.create(user=user, email=user.email, first_name=name, last_name='Test')
            import_completed_workflow(text_id=text.pk, next_stage='ready', stages=[
                {'stage_type': 'editing', 'assigned_to': user.email}, {'stage_type': 'styling', 'assigned_to': user.email}])
            review = Review.objects.create(title=text.title, anthology=book, copied_text=text, status='accepted', length=100)
            ReviewAssignment.objects.create(review=review, user=user, position=1, opinion='yes')
        groups = {row['label']: row['names'] for row in text_credit_groups(first)}
        self.assertEqual(groups['Redakcja'], ['Ala Test'])
        self.assertEqual(groups['Kontrola przed składem'], ['Ala Test'])
        self.assertEqual(groups['Recenzje'], ['Ala Test'])
        self.assertEqual(groups['Ilustracja'], [])

    def test_role_names_surname_order_and_footer_only_for_superuser(self):
        role = Role.objects.get_or_create(name='Koordynator recenzji')[0]
        users = {}
        for username, first_name, last_name in (('coord', 'Adam', 'Zeta'), ('zofia', 'Zofia', 'Alfa')):
            users[username] = get_user_model().objects.create_user(username, f'{username}@example.com')
            person = Person.objects.create(user=users[username], email=users[username].email,
                                           first_name=first_name, last_name=last_name)
            person.roles.add(role)
        self.assertEqual(role_names_context({'role': str(role.pk)})['names'], ['Zofia Alfa', 'Adam Zeta'])
        text = Text.objects.create(anthology=Anthology.objects.create(title='Nabór testowy'), title='Tekst', length=1000)
        url = reverse('core:assigned_text_detail', args=[text.pk])
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(url), '<span>Stopka tekstu</span>', html=True)
        self.assertNotContains(self.client.get(url), 'Zofia Alfa, Adam Zeta')
        self.client.force_login(users['coord'])
        self.assertNotContains(self.client.get(url), 'Lista osób z zespołu według roli')

    def test_new_anthology_gets_one_typography_task_and_edit_form_includes_it(self):
        self.client.force_login(self.admin)
        book = Anthology.objects.create(title='Typografia test')
        task = book.production_tasks.get(task_type='cover_typography')
        self.assertEqual(task.status, 'not_commissioned')
        book.save()
        self.assertEqual(book.production_tasks.filter(task_type='cover_typography').count(), 1)
        self.assertContains(self.client.get(reverse('core:anthology_detail', args=[book.pk])), 'cover_typography-status')
        response = self.client.get(reverse('core:task_list'), {'task': 'cover_typography'})
        self.assertIn(('cover_typography', 'Typografia okładki'), response.context['task_choices'])
        self.assertTrue(all(len(row['cells']) == 6 for row in response.context['tasks']))
        response = self.client.get(reverse('admin:texts_anthologytask_change',
                                           args=[book.production_tasks.get(task_type='cover_typography').pk]))
        self.assertContains(response, 'Typografia okładki')

    def test_canonical_routes(self):
        self.assertEqual(reverse('core:workflow_list'), '/tabelka-zbiorcza/')
        self.assertEqual(reverse('core:dashboard_tasks'), '/pulpit/zadania/')
        self.assertIn('/skrzynki-zgloszen/', reverse('admin:core_mailboxconnection_changelist'))
        self.assertIn('/popraw/', reverse('admin:workflow_stage_correct', args=[1]))
