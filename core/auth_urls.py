from django.contrib.auth import views
from django.urls import path

urlpatterns = [
    path('logowanie/', views.LoginView.as_view(), name='login'),
    path('wylogowanie/', views.LogoutView.as_view(), name='logout'),
    path('zmiana-hasla/', views.PasswordChangeView.as_view(), name='password_change'),
    path('zmiana-hasla/gotowe/', views.PasswordChangeDoneView.as_view(), name='password_change_done'),
    path('reset-hasla/', views.PasswordResetView.as_view(), name='password_reset'),
    path('reset-hasla/wyslano/', views.PasswordResetDoneView.as_view(), name='password_reset_done'),
    path('nowe-haslo/<uidb64>/<token>/', views.PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('nowe-haslo/gotowe/', views.PasswordResetCompleteView.as_view(), name='password_reset_complete'),
]
