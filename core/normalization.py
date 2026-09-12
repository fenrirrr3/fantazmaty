"""Wspólne porządkowanie danych wejściowych, bez zmiany treści notatek."""

import unicodedata


def compact(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def lower(value):
    return compact(value).lower()


def upper(value):
    return compact(value).upper()


def person_name(value):
    return compact(value).title()


def email(value):
    # Spacje wewnątrz adresu pozostają błędem, nie naprawiamy ich na ślepo.
    return unicodedata.normalize("NFKC", value or "").strip().lower()


AUTHOR_FIELDS = {
    "first_name": person_name, "last_name": person_name,
    "pseudonym": compact, "email": email,
}
TEXT_FIELDS = {"title": compact, "content_warnings": lower}
REVIEW_FIELDS = {
    **TEXT_FIELDS, "author_first_name": person_name, "author_last_name": person_name,
    "email": email, "genre": compact, "phone_number": compact,
}


class NormalizedModelMixin:
    normalization_fields = {}

    def normalize_fields(self, fields=None):
        for name, normalize in self.normalization_fields.items():
            if fields is None or name in fields:
                setattr(self, name, normalize(getattr(self, name)))

    def clean_fields(self, exclude=None):
        self.normalize_fields(set(self.normalization_fields) - set(exclude or ()))
        return super().clean_fields(exclude=exclude)

    def save(self, *args, **kwargs):
        self.normalize_fields(kwargs.get("update_fields"))
        return super().save(*args, **kwargs)


class NormalizedFormMixin:
    normalization_fields = {}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.is_bound:
            self.data = self.data.copy()
            for name, normalize in self.normalization_fields.items():
                key = self.add_prefix(name)
                if name in self.fields and key in self.data:
                    self.data[key] = normalize(self.data[key])
