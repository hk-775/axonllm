"""Adapter registry for looking up provider adapters by name."""

from src.gateway.adapters.base import ProviderAdapter


class AdapterRegistry:
    """Maintains a mapping of provider names to ProviderAdapter instances."""

    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, provider_name: str, adapter: ProviderAdapter) -> None:
        """Register an adapter for a provider name."""
        self._adapters[provider_name] = adapter

    def get(self, provider_name: str) -> ProviderAdapter:
        """Look up an adapter by provider name.

        Raises:
            KeyError: If no adapter is registered for the given provider name.
        """
        try:
            return self._adapters[provider_name]
        except KeyError:
            raise KeyError(
                f"No adapter registered for provider '{provider_name}'. "
                f"Available providers: {list(self._adapters.keys())}"
            )
