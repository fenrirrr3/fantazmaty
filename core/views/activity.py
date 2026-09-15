from django import forms
from django.contrib.auth.decorators import login_required
from django.db.models import Q
from django.shortcuts import render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from core.models import UserActivity
from core.pagination import paginate_items
from core.permissions import superuser_required


class ActivityFilterForm(forms.Form):
    q = forms.CharField(label='Użytkownik lub działanie', required=False, max_length=255)
    method = forms.ChoiceField(label='Rodzaj', required=False, choices=(('', 'Wszystkie'), ('GET', 'Podgląd'), ('POST', 'Formularze i zmiany')))
    date_from = forms.DateField(label='Od', required=False, widget=forms.DateInput(attrs={'type':'date'}))
    date_to = forms.DateField(label='Do', required=False, widget=forms.DateInput(attrs={'type':'date'}))

    def clean(self):
        data = super().clean()
        if data.get('date_from') and data.get('date_to') and data['date_from'] > data['date_to']:
            raise forms.ValidationError('Data „Od” nie może być późniejsza niż „Do”.')
        return data


@never_cache
@login_required
@require_GET
@superuser_required
def user_activity(request):
    form = ActivityFilterForm(request.GET)
    rows = UserActivity.objects.select_related('user__person_profile')
    if form.is_valid():
        data = form.cleaned_data
        for term in data['q'].split():
            rows = rows.filter(Q(actor__plcontains=term) | Q(user__first_name__plcontains=term) | Q(user__last_name__plcontains=term) | Q(action__plcontains=term) | Q(target__plcontains=term))
        if data['method']:
            rows = rows.filter(method=data['method'])
        if data['date_from']:
            rows = rows.filter(created_at__date__gte=data['date_from'])
        if data['date_to']:
            rows = rows.filter(created_at__date__lte=data['date_to'])
    else:
        rows = rows.none()
    page = paginate_items(request, rows)
    return render(request, 'core/user_activity.html', {'form':form, 'activities':page, 'page_obj':page}, status=400 if form.errors else 200)
