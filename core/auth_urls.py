from django.contrib.auth import views
from django.urls import path
from django.views.decorators.cache import never_cache
from core.auth_forms import EmailAuthenticationForm
from core.invitations import InvitationConfirmView

urlpatterns = [
    path('logowanie/', views.LoginView.as_view(authentication_form=EmailAuthenticationForm,
                                                    redirect_authenticated_user=True), name='login'),
    path('wylogowanie/', views.LogoutView.as_view(), name='logout'),
    # Strony po zalogowaniu bez pamięci podręcznej przeglądarki, jak pozostałe widoki aplikacji.
    path('zmiana-hasla/', never_cache(views.PasswordChangeView.as_view()), name='password_change'),
    path('zmiana-hasla/gotowe/', never_cache(views.PasswordChangeDoneView.as_view()), name='password_change_done'),
    path('reset-hasla/', views.PasswordResetView.as_view(), name='password_reset'),
    path('reset-hasla/wyslano/', views.PasswordResetDoneView.as_view(), name='password_reset_done'),
    path('nowe-haslo/<uidb64>/<token>/', views.PasswordResetConfirmView.as_view(), name='password_reset_confirm'),
    path('zaproszenie/<uidb64>/<token>/', InvitationConfirmView.as_view(), name='account_invitation'),
    path('nowe-haslo/gotowe/', views.PasswordResetCompleteView.as_view(), name='password_reset_complete'),
]
