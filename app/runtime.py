"""The long-lived objects the API shares: settings and the AI drafter."""

from .ai.drafter import Drafter
from .config import get_settings

settings = get_settings()
drafter = Drafter(settings)
