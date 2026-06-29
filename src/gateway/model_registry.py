"""Model Registry: loads and manages model configuration from YAML."""

import logging
from pathlib import Path

import yaml

from .config import DEFAULT_CONFIG
from .models import (
    ProviderModelMapping,
    RoutingStrategy,
    TokenPricing,
    ValidationError,
    ModelConfig,
)

logger = logging.getLogger(__name__)

VALID_PROVIDERS = DEFAULT_CONFIG.valid_providers

VALID_ROUTING_STRATEGIES = {s.value for s in RoutingStrategy}


class ModelRegistry:
    """Loads and manages model configuration from YAML."""

    def __init__(self) -> None:
        self.models: dict[str, ModelConfig] = {}

    def load(self, config_path: str) -> None:
        """Load and validate YAML configuration file.

        Invalid entries are rejected with warnings; valid entries are loaded.
        """
        path = Path(config_path)
        raw = path.read_text(encoding="utf-8")
        config = yaml.safe_load(raw)
        if config is None:
            config = {}
        self._load_config(config)

    @staticmethod
    def from_yaml(yaml_str: str) -> "ModelRegistry":
        """Parse YAML string into ModelRegistry."""
        config = yaml.safe_load(yaml_str)
        if config is None:
            config = {}
        registry = ModelRegistry()
        registry._load_config(config)
        return registry

    def resolve(self, model: str) -> list[ProviderModelMapping]:
        """Resolve model to provider-specific mappings.

        Raises KeyError if model not found.
        """
        if model not in self.models:
            raise KeyError(f"Unknown model: {model}")
        return self.models[model].providers

    def list_models(self) -> list[ModelConfig]:
        """Return all configured models."""
        return list(self.models.values())

    def validate(self, config: dict) -> list[ValidationError]:
        """Validate configuration dict. Returns list of errors (empty if valid)."""
        errors: list[ValidationError] = []
        entries = config.get("models")

        if entries is None:
            errors.append(ValidationError(
                field="models",
                message="Missing required top-level key 'models'",
            ))
            return errors

        if not isinstance(entries, list):
            errors.append(ValidationError(
                field="models",
                message="'models' must be a list",
            ))
            return errors

        seen_names: set[str] = set()

        for idx, entry in enumerate(entries):
            prefix = f"models[{idx}]"

            if not isinstance(entry, dict):
                errors.append(ValidationError(
                    field=prefix,
                    message="Entry must be a mapping",
                ))
                continue

            # Required: name
            name = entry.get("name")
            if not name:
                errors.append(ValidationError(
                    field=f"{prefix}.name",
                    message="Missing required field 'name'",
                ))
            elif not isinstance(name, str):
                errors.append(ValidationError(
                    field=f"{prefix}.name",
                    message="'name' must be a string",
                ))
            else:
                if name in seen_names:
                    errors.append(ValidationError(
                        field=f"{prefix}.name",
                        message=f"Duplicate model name: '{name}'",
                    ))
                seen_names.add(name)

            # Required: description
            description = entry.get("description")
            if not description:
                errors.append(ValidationError(
                    field=f"{prefix}.description",
                    message="Missing required field 'description'",
                ))

            # Optional: routing_strategy
            strategy = entry.get("routing_strategy")
            if strategy is not None and strategy not in VALID_ROUTING_STRATEGIES:
                errors.append(ValidationError(
                    field=f"{prefix}.routing_strategy",
                    message=f"Invalid routing strategy: '{strategy}'. Must be one of: {sorted(VALID_ROUTING_STRATEGIES)}",
                ))

            # Required: providers
            providers = entry.get("providers")
            if providers is None:
                errors.append(ValidationError(
                    field=f"{prefix}.providers",
                    message="Missing required field 'providers'",
                ))
            elif not isinstance(providers, list):
                errors.append(ValidationError(
                    field=f"{prefix}.providers",
                    message="'providers' must be a list",
                ))
            elif len(providers) == 0:
                errors.append(ValidationError(
                    field=f"{prefix}.providers",
                    message="'providers' must not be empty",
                ))
            else:
                for pidx, prov in enumerate(providers):
                    pprefix = f"{prefix}.providers[{pidx}]"
                    if not isinstance(prov, dict):
                        errors.append(ValidationError(
                            field=pprefix,
                            message="Provider entry must be a mapping",
                        ))
                        continue

                    prov_name = prov.get("provider")
                    if not prov_name:
                        errors.append(ValidationError(
                            field=f"{pprefix}.provider",
                            message="Missing required field 'provider'",
                        ))
                    elif prov_name not in VALID_PROVIDERS:
                        errors.append(ValidationError(
                            field=f"{pprefix}.provider",
                            message=f"Invalid provider: '{prov_name}'. Must be one of: {sorted(VALID_PROVIDERS)}",
                        ))

                    model_id = prov.get("model_id")
                    if not model_id:
                        errors.append(ValidationError(
                            field=f"{pprefix}.model_id",
                            message="Missing required field 'model_id'",
                        ))

        return errors

    def pretty_print(self) -> str:
        """Format current registry state as valid YAML."""
        entries = []
        for model_config in self.models.values():
            entry: dict = {
                "name": model_config.name,
                "description": model_config.description,
                "routing_strategy": model_config.routing_strategy.value,
            }
            if model_config.capabilities is not None:
                entry["capabilities"] = model_config.capabilities

            provider_list = []
            for pm in model_config.providers:
                prov_entry: dict = {
                    "provider": pm.provider,
                    "model_id": pm.model_id,
                    "weight": pm.weight,
                    "fallback_order": pm.fallback_order,
                }
                if pm.pricing is not None:
                    prov_entry["pricing"] = {
                        "prompt_token_cost": pm.pricing.prompt_token_cost,
                        "completion_token_cost": pm.pricing.completion_token_cost,
                    }
                provider_list.append(prov_entry)

            entry["providers"] = provider_list
            entries.append(entry)

        return yaml.dump(
            {"models": entries},
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )

    # --- Internal helpers ---

    def _load_config(self, config: dict) -> None:
        """Load config dict into the registry with partial loading."""
        errors = self.validate(config)

        # Build a set of entry indices that have errors
        error_indices = self._error_indices(errors, config)

        entries = config.get("models")
        if not isinstance(entries, list):
            if errors:
                for err in errors:
                    logger.warning("Config validation error: [%s] %s", err.field, err.message)
            return

        for idx, entry in enumerate(entries):
            if idx in error_indices:
                entry_errors = [
                    e for e in errors
                    if e.field.startswith(f"models[{idx}]")
                ]
                for err in entry_errors:
                    logger.warning("Skipping invalid entry: [%s] %s", err.field, err.message)
                continue

            name = entry["name"]

            if name in self.models:
                logger.warning(
                    "Skipping duplicate model name: '%s'", name
                )
                continue

            self.models[name] = self._parse_entry(entry)

    def _parse_entry(self, entry: dict) -> ModelConfig:
        """Parse a single validated model entry."""
        strategy_str = entry.get("routing_strategy", "round-robin")
        routing_strategy = RoutingStrategy(strategy_str)

        providers = []
        for prov in entry["providers"]:
            pricing = None
            if "pricing" in prov:
                pricing = TokenPricing(
                    prompt_token_cost=float(prov["pricing"]["prompt_token_cost"]),
                    completion_token_cost=float(prov["pricing"]["completion_token_cost"]),
                )
            providers.append(ProviderModelMapping(
                provider=prov["provider"],
                model_id=prov["model_id"],
                weight=float(prov.get("weight", 1.0)),
                fallback_order=int(prov.get("fallback_order", 0)),
                pricing=pricing,
            ))

        capabilities = entry.get("capabilities")
        if capabilities is not None:
            capabilities = list(capabilities)

        return ModelConfig(
            name=entry["name"],
            description=entry["description"],
            providers=providers,
            routing_strategy=routing_strategy,
            capabilities=capabilities,
        )

    @staticmethod
    def _error_indices(errors: list[ValidationError], config: dict) -> set[int]:
        """Determine which entry indices have validation errors."""
        bad: set[int] = set()
        for err in errors:
            field = err.field
            if field.startswith("models["):
                bracket_end = field.index("]")
                try:
                    idx = int(field[len("models["):bracket_end])
                    bad.add(idx)
                except ValueError:
                    pass
            elif field == "models":
                entries = config.get("models")
                if isinstance(entries, list):
                    bad.update(range(len(entries)))
        return bad
