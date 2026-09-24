"""Native multi-account orchestration inside Vicoa."""

from .store import ControlPlane, ControlPlaneError

__all__ = ["ControlPlane", "ControlPlaneError"]
