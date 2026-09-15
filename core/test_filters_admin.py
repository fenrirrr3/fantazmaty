from django.test import TestCase
from django.urls import reverse
from django.contrib import admin
from django.test import RequestFactory
from django.core.exceptions import ValidationError
from core.tests import CoreTestDataMixin
from core.admin_access import SuperuserAdminAuthenticationForm
from texts.models import Anthology, Review, ReviewAssignment

class FiltersAdminTests(CoreTestDataMixin, TestCase):
    def test_admin_superuser_only(self):
        self.coordinator.is_staff = True
        self.coordinator.save()
        request = RequestFactory().get('/panel/')
        request.user = self.coordinator
        self.assertFalse(admin.site.has_permission(request))
        self.client.force_login(self.coordinator)
        self.assertEqual(self.client.get(reverse('admin:index')).status_code,302)
        self.assertNotContains(self.client.get(reverse('core:home')), 'Panel administracyjny')
        with self.assertRaises(ValidationError):
            SuperuserAdminAuthenticationForm().confirm_login_allowed(self.coordinator)
        self.client.force_login(self.superuser)
        self.assertEqual(self.client.get(reverse('admin:index')).status_code,200)

    def test_review_anthology_filters_live_and_archived(self):
        other = Anthology.objects.create(title='Inna antologia')
        self.client.force_login(self.reviewer)
        for old in (False, True):
            for anthology in (self.anthology, other):
                review = Review.objects.create(title='Żółty smok', anthology=anthology, old_reviews=old, genre='fantasy', length=1000, email='test@example.com')
                ReviewAssignment.objects.create(review=review, user=self.reviewer, position=1, opinion='yes')
            response = self.client.get(reverse('core:my_reviews'), {'view':'archived' if old else 'all', 'anthology':self.anthology.pk, 'q':'zolty'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.context['page_obj'].paginator.count,1)
            self.assertContains(response,'name="anthology"')
            for field in ('author','status','sort','hide_ready'):
                self.assertNotContains(response, 'name="'+field+'"')
            self.assertNotContains(response,'Wyświetlono')
            self.assertContains(response,'Strona 1 z 1')
