"""Run from the project directory; preview by default, write with --apply."""
import os
import sys

from django.core.management import execute_from_command_line


if __name__ == '__main__':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'fantazmaty.settings')
    execute_from_command_line([sys.argv[0], 'merge_workflow_assignments', *sys.argv[1:]])
