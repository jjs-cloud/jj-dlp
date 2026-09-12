"""Site plugin registry: a simple {id: SitePlugin instance} lookup."""

from typing import Dict, List, Type

from jj_dlp.core.plugins.base import FieldDef, SitePlugin

__all__ = ["FieldDef", "SitePlugin", "register_plugin", "get_plugin", "list_plugin_ids"]

_REGISTRY: Dict[str, SitePlugin] = {}


def register_plugin(plugin_cls: Type[SitePlugin]) -> Type[SitePlugin]:
    """Instantiate and register a SitePlugin subclass under its id. Usable as a decorator."""
    instance = plugin_cls()
    _REGISTRY[instance.id] = instance
    return plugin_cls


def get_plugin(plugin_id: str) -> SitePlugin:
    """Return the registered plugin instance for this id, or raise KeyError."""
    try:
        return _REGISTRY[plugin_id]
    except KeyError:
        raise KeyError(f"No site plugin registered with id '{plugin_id}'") from None


def list_plugin_ids() -> List[str]:
    """Return every currently registered plugin id."""
    return list(_REGISTRY.keys())


from jj_dlp.core.plugins import chatsite  # noqa: E402,F401  registers the plugin
