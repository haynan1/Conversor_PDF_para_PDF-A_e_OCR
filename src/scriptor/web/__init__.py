"""Interface local do Scriptor, para quem não usa linha de comando."""

from __future__ import annotations

from .server import Studio, launch

__all__ = ["Studio", "launch"]
