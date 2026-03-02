"""Anton Google Auth — pluggable Google OAuth login for FastAPI services."""

from .auth import AntonGoogleAuth
from ._logo import ANTON_LOGO_B64

__all__ = ["AntonGoogleAuth", "ANTON_LOGO_B64"]
__version__ = "0.1.0"
