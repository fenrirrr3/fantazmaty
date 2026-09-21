"""Private capability used only while the transactional workflow importer writes."""
from contextvars import ContextVar

importing_completed = ContextVar("importing_completed", default=False)
