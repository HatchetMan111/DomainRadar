"""DomainRadar – Paketinitialisierung."""

__version__ = "0.1.0"

from .domains import DomainFilter  # noqa: F401

__all__ = ["DomainFilter", "__version__"]