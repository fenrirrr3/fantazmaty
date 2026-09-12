"""Polish accent-insensitive substring lookup, portable across database engines."""
import unicodedata
from django.db.models import CharField, TextField, Value
from django.db.models.functions import Lower, Replace
from django.db.models.lookups import IContains


def fold_polish(value):
    return "".join(c for c in unicodedata.normalize("NFD", str(value).replace("ł", "l").replace("Ł", "L")) if not unicodedata.combining(c)).lower()


class PolishContains(IContains):
    lookup_name = "plcontains"

    def process_lhs(self, compiler, connection, lhs=None):
        expression = lhs if lhs is not None else self.lhs
        for source, target in zip("ĄĆĘŁŃÓŚŹŻąćęłńóśźż", "ACELNOSZZacelnoszz"):
            expression = Replace(expression, Value(source), Value(target))
        return compiler.compile(Lower(expression))

    def get_rhs_op(self, connection, rhs):
        if hasattr(self.rhs, "as_sql") or self.bilateral_transforms:
            return connection.pattern_ops["icontains"].format(connection.pattern_esc).format(rhs)
        return connection.operators["icontains"] % rhs

    def get_prep_lookup(self):
        if isinstance(self.rhs, str):
            self.rhs = fold_polish(self.rhs)
        return super().get_prep_lookup()


def register():
    CharField.register_lookup(PolishContains)
    TextField.register_lookup(PolishContains)
