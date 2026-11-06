from django.urls import path

from . import views

app_name = 'mikolajki'

urlpatterns = [
    path('', views.secret_santa, name='secret_santa'),
    path('potwierdz/<str:token>/', views.secret_santa_confirm, name='secret_santa_confirm'),
    path('moje-zgloszenie/', views.secret_santa_entry, name='secret_santa_entry'),
]
