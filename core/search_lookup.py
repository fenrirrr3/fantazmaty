"""Accent-insensitive substring lookup without a chain of eighteen SQL replaces."""
import unicodedata
from django.db import connections
from django.db.backends.signals import connection_created
from django.db.models import CharField, TextField, Func, Value
from django.db.models.functions import Lower, Replace, Collate, Cast
from django.db.models.lookups import IContains


def fold_polish(value):
    return ''.join(c for c in unicodedata.normalize('NFD', str(value or '').replace('ł', 'l').replace('Ł', 'L')) if not unicodedata.combining(c)).lower()


def install_sqlite_function(sender=None, connection=None, **kwargs):
    if connection is not None and connection.vendor == 'sqlite' and connection.connection is not None:
        connection.connection.create_function('cms_fold_text', 1, fold_polish, deterministic=True)


class PolishContains(IContains):
    lookup_name = 'plcontains'

    def process_lhs(self, compiler, connection, lhs=None):
        expression = lhs if lhs is not None else self.lhs
        if connection.vendor == 'sqlite':
            expression = Func(expression, function='cms_fold_text', output_field=TextField())
        elif connection.vendor == 'mysql':
            # The Unicode collation handles accents; ł needs explicit folding.
            expression = Lower(Replace(Replace(expression, Value('ł'), Value('l')), Value('Ł'), Value('L')))
            expression = Collate(Func(expression, function='CONVERT', template='CONVERT(%(expressions)s USING utf8mb4)', output_field=TextField()), 'utf8mb4_unicode_ci')
        else:
            for source, target in zip('ĄĆĘŁŃÓŚŹŻąćęłńóśźż', 'ACELNOSZZacelnoszz'):
                expression = Replace(expression, Value(source), Value(target))
            expression = Lower(expression)
        return compiler.compile(expression)

    def get_rhs_op(self, connection, rhs):
        if hasattr(self.rhs, 'as_sql') or self.bilateral_transforms:
            return connection.pattern_ops['icontains'].format(connection.pattern_esc).format(rhs)
        return connection.operators['icontains'] % rhs

    def get_prep_lookup(self):
        if isinstance(self.rhs, str): self.rhs = fold_polish(self.rhs)
        return super().get_prep_lookup()


def register():
    CharField.register_lookup(PolishContains)
    TextField.register_lookup(PolishContains)
    connection_created.connect(install_sqlite_function, dispatch_uid='cms-fold-text', weak=False)
    for connection in connections.all(initialized_only=True):
        install_sqlite_function(connection=connection)
