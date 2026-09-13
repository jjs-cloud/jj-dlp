"""Pluggable dependency check/install registry."""

from typing import Callable, List, Optional, Tuple

ProgressCallback = Callable[[str], None]


class Dependency:
    """Base interface for a checkable/installable external dependency."""

    name: str = ""

    def check(self) -> Tuple[bool, str]:
        """Return (is_present, message)."""
        raise NotImplementedError

    def install(self, progress_cb: Optional[ProgressCallback] = None) -> Tuple[bool, str]:
        """Attempt to install the dependency, returning (success, message)."""
        raise NotImplementedError


_registry: List[Dependency] = []


def register(dependency: Dependency) -> None:
    """Add a dependency instance to the registry."""
    _registry.append(dependency)


def get_all() -> List[Dependency]:
    """Return every registered dependency, in registration order."""
    return list(_registry)
