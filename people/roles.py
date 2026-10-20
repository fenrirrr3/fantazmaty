"""Nazwy ról zespołu (``people.Role.name``) – jedyne miejsce, w którym kod je zapisuje.

Kod sprawdzający uprawnienia, filtry i komendy korzysta wyłącznie z tych stałych.
Etykiety interfejsu (nagłówki kolumn, etykiety ról workflow typu „Korektor 1”)
nie są nazwami ról i tu nie trafiają. Moduł nie importuje modeli, więc można go
używać w modelach, sygnałach i uprawnieniach bez cykli importu.
"""
from django.db.models import Q

# Role wykonawcze.
REVIEWER = "Recenzent"
EDITOR = "Redaktor"
PROOFREADER = "Korektor"
VERIFIER = "Weryfikator"
POST_LAYOUT_PROOFREADER = "Korektor poskładowy"
DESIGNER = "Grafik"
SOUND_ENGINEER = "Dźwiękowiec"
NARRATOR = "Lektor"
TYPESETTER = "Składacz"
TRANSLATOR = "Tłumacz"
ILLUSTRATOR = "Ilustrator"
AUDIOBOOK_PROOFREADER = "Korektor audiobooków"
SOCIAL_MEDIA = "Social Media"

# Rola honorowa – wyłącznie wyróżniona etykieta, nie daje żadnych uprawnień ani dostępu.
EMPEROR = "Imperator Ludzkości"

# Koordynatorzy działów.
EDITING_COORDINATOR = "Koordynator redakcji"
AUDIOBOOK_COORDINATOR = "Koordynator audiobooków"
VERIFICATION_COORDINATOR = "Koordynator weryfikacji"
ILLUSTRATION_COORDINATOR = "Koordynator ilustracji"
REVIEW_COORDINATOR = "Koordynator recenzji"
PROOFREADING_COORDINATOR = "Koordynator korekty"
RECRUITMENT_COORDINATOR = "Koordynator rekrutacji"

# Ogólne role koordynatora – wycofane (nie można ich utworzyć), ale „Koordynator” to
# nazwa dawnej grupy kont (dostęp historyczny) i alias „dowolny koordynator” w has_role.
COORDINATOR = "Koordynator"
TEAM_COORDINATOR = "Koordynator zespołu"
RETIRED_ROLES = (COORDINATOR, TEAM_COORDINATOR)

COORDINATOR_ROLES = (
    EDITING_COORDINATOR, AUDIOBOOK_COORDINATOR, VERIFICATION_COORDINATOR,
    ILLUSTRATION_COORDINATOR, REVIEW_COORDINATOR, PROOFREADING_COORDINATOR,
    RECRUITMENT_COORDINATOR,
)

# Role dające dostęp do audiobooków (lektorów i montażystów prowadzi spis, nie konta).
AUDIOBOOK_ROLES = (NARRATOR, SOUND_ENGINEER, AUDIOBOOK_PROOFREADER)

# Kolejność ról w filtrze zespołu; pozostałe role trafiają na koniec.
TEAM_ROLE_ORDER = (
    EMPEROR, REVIEWER, EDITOR, PROOFREADER, VERIFIER, POST_LAYOUT_PROOFREADER,
    DESIGNER, SOUND_ENGINEER, NARRATOR, TYPESETTER, TRANSLATOR, SOCIAL_MEDIA,
    EDITING_COORDINATOR, AUDIOBOOK_COORDINATOR,
    VERIFICATION_COORDINATOR, ILLUSTRATION_COORDINATOR,
    REVIEW_COORDINATOR, PROOFREADING_COORDINATOR,
    RECRUITMENT_COORDINATOR,
)

# Standardowy zestaw ról tworzony komendą ensure_team_roles.
STANDARD_ROLES = tuple(dict.fromkeys((*TEAM_ROLE_ORDER, ILLUSTRATOR, AUDIOBOOK_PROOFREADER)))

_COORDINATOR_KEY = COORDINATOR.casefold()


def is_general_coordinator_name(name):
    """Czy nazwa to ogólna rola/grupa „Koordynator” (bez działu), bez względu na wielkość liter."""
    return (name or "").casefold() == _COORDINATOR_KEY


def is_coordinator_role_name(name):
    """Czy nazwa roli daje dostęp koordynatora: „Koordynator” albo „Koordynator <dział>”.

    Wymagana spacja po słowie „Koordynator”, więc np. „Koordynatorzy-stażyści” nie jest rolą koordynatora.
    """
    name = (name or "").casefold()
    return name == _COORDINATOR_KEY or name.startswith(_COORDINATOR_KEY + " ")


def coordinator_role_q(field="name"):
    """Warunek ``Q`` odpowiadający ``is_coordinator_role_name`` dla pola z nazwą roli (np. ``"roles__name"``)."""
    return Q(**{f"{field}__iexact": COORDINATOR}) | Q(**{f"{field}__istartswith": COORDINATOR + " "})
