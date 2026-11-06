"""Panel admina Mikołajek (tylko superuser i tylko przy włączonym module): edycje z zamknięciem zapisów, losowaniem
i wysyłką listów oraz zgłoszenia z ręcznym dodawaniem i wysyłką adresu pary."""
import logging

from django.contrib import admin, messages
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse
from django.utils import timezone
from django.utils.html import format_html
from django.views.decorators.http import require_POST

from authors.admin import SuperuserOnlyAdminMixin

from . import services
from .forms import PanelEntryForm
from .models import Edition, Entry

logger = logging.getLogger(__name__)


class ModuleAdminMixin(SuperuserOnlyAdminMixin):
    """Superuser i włączony moduł Mikołajki – inaczej sekcja jest ukryta, a adresy panelu niedostępne."""

    def has_superuser_access(self, request):
        return SuperuserOnlyAdminMixin.has_superuser_access(request) and services.enabled()


@admin.register(Edition)
class EditionAdmin(ModuleAdminMixin, admin.ModelAdmin):
    list_display = ('__str__', 'signup_deadline', 'status', 'confirmed_count', 'entry_count', 'drawn_at')
    fields = ('year', 'signup_deadline', 'status', 'closed_at', 'drawn_at', 'counts')
    readonly_fields = ('status', 'closed_at', 'drawn_at', 'counts')
    change_form_template = 'admin/mikolajki/edition/change_form.html'

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(
            entry_total=Count('entries', filter=Q(entries__withdrawn_at__isnull=True)),
            confirmed_total=Count('entries', filter=Q(entries__withdrawn_at__isnull=True,
                                                      entries__confirmed_at__isnull=False)))

    @admin.display(description='stan')
    def status(self, obj):
        return obj.status_label if obj.pk else '–'

    @admin.display(description='potwierdzone zgłoszenia', ordering='confirmed_total')
    def confirmed_count(self, obj):
        return obj.confirmed_total

    @admin.display(description='wszystkie zgłoszenia (bez wycofanych)', ordering='entry_total')
    def entry_count(self, obj):
        return obj.entry_total

    @admin.display(description='zgłoszenia')
    def counts(self, obj):
        if not obj.pk:
            return '–'
        entries = obj.entries.all()
        active = entries.filter(withdrawn_at__isnull=True)
        url = reverse('admin:mikolajki_entry_changelist') + f'?edition__id__exact={obj.pk}'
        return format_html(
            '<a href="{}">Potwierdzone: {}</a>, niepotwierdzone: {}, wycofane: {}; listy wysłane: {}, paczki '
            'przygotowane: {}, adresy wysłane: {}', url, active.filter(confirmed_at__isnull=False).count(),
            active.filter(confirmed_at__isnull=True).count(), entries.filter(withdrawn_at__isnull=False).count(),
            entries.filter(letter_sent_at__isnull=False).count(),
            entries.filter(package_prepared_at__isnull=False).count(),
            entries.filter(address_sent_at__isnull=False).count())

    def get_urls(self):
        return [
            path('<int:pk>/close/', self.admin_site.admin_view(require_POST(self.close_view)), name='mikolajki_edition_close'),
            path('<int:pk>/draw/', self.admin_site.admin_view(require_POST(self.draw_view)), name='mikolajki_edition_draw'),
            path('<int:pk>/letters/', self.admin_site.admin_view(require_POST(self.letters_view)),
                 name='mikolajki_edition_letters'),
        ] + super().get_urls()

    def _edition(self, request, pk):
        edition = get_object_or_404(Edition, pk=pk)
        if not self.has_change_permission(request, edition):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        return edition

    def _back(self, edition):
        return redirect('admin:mikolajki_edition_change', edition.pk)

    @staticmethod
    def _letters_result(request, sent, failed):
        if sent:
            messages.success(request, f'Wysłano listy par: {sent}.')
        if failed:
            messages.error(request, 'Nie udało się wysłać listów do: ' + ', '.join(entry.email for entry in failed)
                           + '. Użyj „Wyślij niewysłane listy”, gdy serwer pocztowy będzie dostępny.')

    def close_view(self, request, pk):
        edition = self._edition(request, pk)
        if edition.signups_open():
            Edition.objects.filter(pk=edition.pk, closed_at__isnull=True).update(closed_at=timezone.now())
            messages.success(request, 'Zapisy zamknięte – nowe zgłoszenia i potwierdzenia nie są już możliwe.')
        return self._back(edition)

    def draw_view(self, request, pk):
        edition = self._edition(request, pk)
        try:
            circle = services.draw(edition)
        except services.DrawError as error:
            messages.error(request, str(error))
        else:
            messages.success(request, f'Rozlosowano pary: {len(circle)} osób w jednym kole (bez par wzajemnych).')
            self._letters_result(request, *services.send_letters(edition, request))
        return self._back(edition)

    def letters_view(self, request, pk):
        edition = self._edition(request, pk)
        if edition.drawn_at:
            self._letters_result(request, *services.send_letters(edition, request))
        return self._back(edition)

    def change_view(self, request, object_id, form_url='', extra_context=None):
        edition = Edition.objects.filter(pk=object_id).first() if str(object_id).isdigit() else None
        extra = {'mikolajki_edition': edition, 'can_close': bool(edition and edition.signups_open()),
                 'can_draw': bool(edition and not edition.signups_open() and not edition.drawn_at),
                 'unsent_letters': edition.entries.filter(recipient__isnull=False, letter_sent_at__isnull=True).count()
                 if edition and edition.drawn_at else 0}
        return super().change_view(request, object_id, form_url, {**extra, **(extra_context or {})})

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (obj is None or not obj.entries.exists())


class StatusFilter(admin.SimpleListFilter):
    title = 'stan'
    parameter_name = 'stan'
    OPTIONS = (
        ('unconfirmed', 'Niepotwierdzone', Q(confirmed_at__isnull=True, withdrawn_at__isnull=True)),
        ('confirmed', 'Potwierdzone', Q(confirmed_at__isnull=False, withdrawn_at__isnull=True)),
        ('withdrawn', 'Wycofane', Q(withdrawn_at__isnull=False)),
        ('prepared', 'Paczka przygotowana – do wysłania adresu',
         Q(package_prepared_at__isnull=False, address_sent_at__isnull=True)),
        ('address_sent', 'Adres pary wysłany', Q(address_sent_at__isnull=False)),
    )

    def lookups(self, request, model_admin):
        return [(key, label) for key, label, _ in self.OPTIONS]

    def queryset(self, request, queryset):
        found = {key: condition for key, _, condition in self.OPTIONS}.get(self.value())
        return queryset.filter(found) if found is not None else queryset


@admin.register(Entry)
class EntryAdmin(ModuleAdminMixin, admin.ModelAdmin):
    form = PanelEntryForm
    list_display = ('full_name', 'email', 'edition', 'status', 'package_prepared_at', 'address_sent_at')
    list_filter = ('edition', StatusFilter)
    search_fields = ('full_name', 'email', 'code')
    list_select_related = ('edition',)
    actions = ('send_addresses',)
    change_form_template = 'admin/mikolajki/entry/change_form.html'
    readonly_fields = ('code', 'status', 'source', 'consent_at', 'confirmed_at', 'withdrawn_at', 'recipient_link',
                       'giver_link', 'letter_sent_at', 'package_prepared_at', 'address_sent_at', 'created_at',
                       'updated_at')
    fieldsets = (
        (None, {'fields': ('edition', 'email', 'full_name', 'shipping_address', 'phone', 'letter')}),
        ('Stan zgłoszenia', {'fields': ('code', 'status', 'source', 'consent_at', 'confirmed_at', 'withdrawn_at',
                                        'created_at', 'updated_at')}),
        ('Losowanie i paczka', {'fields': ('recipient_link', 'giver_link', 'letter_sent_at', 'package_prepared_at',
                                           'address_sent_at')}),
    )

    def get_fieldsets(self, request, obj=None):
        # Przy dodawaniu: same dane (kod, stan i potwierdzenie ustawia zapis).
        return self.fieldsets[:1] if obj is None else self.fieldsets

    def get_changeform_initial_data(self, request):
        initial = super().get_changeform_initial_data(request)
        edition = Edition.current()
        if edition is not None and 'edition' not in initial:
            initial['edition'] = edition.pk
        return initial

    @admin.display(description='stan')
    def status(self, obj):
        return obj.status_label

    @admin.display(description='wylosowana para (komu przygotowuje paczkę)')
    def recipient_link(self, obj):
        return self._link(obj.recipient)

    @admin.display(description='kto przygotowuje paczkę dla tej osoby')
    def giver_link(self, obj):
        return self._link(Entry.objects.filter(recipient=obj).first() if obj.pk else None)

    @staticmethod
    def _link(entry):
        if entry is None:
            return '–'
        return format_html('<a href="{}">{} ({})</a>', reverse('admin:mikolajki_entry_change', args=[entry.pk]),
                           entry.full_name, entry.email)

    def save_model(self, request, obj, form, change):
        if not change:
            obj.code = services.new_code()
            obj.source = Entry.Source.PANEL
            obj.confirmed_at = timezone.now()
        super().save_model(request, obj, form, change)
        if not change:
            try:
                services.send_summary(obj, request)
            except Exception:  # noqa: BLE001
                logger.exception('Mikołajki: nie udało się wysłać podsumowania dodanego zgłoszenia')
                messages.warning(request, f'Zgłoszenie dodane, ale nie udało się wysłać podsumowania na {obj.email}. '
                                          f'Kod uczestnika: {obj.code}.')
            else:
                messages.info(request, f'Podsumowanie z kodem uczestnika wysłano na {obj.email}.')

    def get_actions(self, request):
        actions = super().get_actions(request)
        # Usuwanie pojedynczo (z ekranu zgłoszenia) – rozlosowanych zgłoszeń nie da się usunąć.
        actions.pop('delete_selected', None)
        return actions

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (obj is None or obj.edition.drawn_at is None)

    def _send_address(self, request, entry):
        """Adres pary do osoby z przygotowaną paczką; komunikat błędu albo None."""
        if entry.recipient_id is None or entry.package_prepared_at is None:
            return f'{entry.full_name}: paczka nie jest oznaczona jako przygotowana.'
        try:
            services.send_address(entry, request)
        except Exception:  # noqa: BLE001
            logger.exception('Mikołajki: nie udało się wysłać adresu pary')
            return f'{entry.full_name}: nie udało się wysłać wiadomości.'
        Entry.objects.filter(pk=entry.pk).update(address_sent_at=timezone.now())
        return None

    @admin.action(description='Wyślij adres pary (zgłoszenia z przygotowaną paczką)')
    def send_addresses(self, request, queryset):
        errors, sent = [], 0
        for entry in queryset.select_related('recipient', 'edition'):
            error = self._send_address(request, entry)
            if error:
                errors.append(error)
            else:
                sent += 1
        if sent:
            messages.success(request, f'Wysłano adresy par: {sent}.')
        for error in errors:
            messages.error(request, error)

    def get_urls(self):
        return [path('<int:pk>/address/', self.admin_site.admin_view(require_POST(self.address_view)),
                     name='mikolajki_entry_address')] + super().get_urls()

    def address_view(self, request, pk):
        entry = get_object_or_404(Entry.objects.select_related('recipient', 'edition'), pk=pk)
        if not self.has_change_permission(request, entry):
            from django.core.exceptions import PermissionDenied
            raise PermissionDenied
        error = self._send_address(request, entry)
        if error:
            messages.error(request, error)
        else:
            messages.success(request, f'Adres pary wysłano na {entry.email}.')
        return redirect('admin:mikolajki_entry_change', entry.pk)
