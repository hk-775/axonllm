"""Optional, pinned local decision models for the routing benchmark.

No Torch, Hugging Face, Laya, or Strands dependency is imported until start().
Models see only the prompt and the shared routing taxonomy, never gold labels.
"""

from __future__ import annotations

import gc
import importlib.metadata
import json
import math
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any, Callable

from src.gateway.autorouting_benchmark import (
    ERROR_LABEL,
    ROUTING_CRITERIA,
    ROUTING_INSTRUCTIONS,
    RouteDecision,
)


MODEL_SPECS = {
    "laya": {
        "model": "convaiinnovations/laya",
        "revision": "55cf4c4ebb4ebe31b2550e8bdf3bd21b99753851",
        "code_revision": "fa9a2a7070b1789912a49ae24603bbfb1a78b001",
        "context_tokens": 512,
        "head_tokens": 192,
        "checkpoint": "english",
    },
    "strands": {
        "model": "StrandsAgents/strands-decider-2B-hobson-v19",
        "revision": "bb282d786bc251fd4e3068de3ada9ddbb38127cd",
        "code_revision": "ddd11994bc451ffc78fa30f43037c291fd4d44b6",
        "base_model": "Qwen/Qwen3.5-2B-Base",
        "base_revision": "b1485b2fa6dfa1287294f269f5fb618e03d52d7c",
        "context_tokens": 4096,
    },
}


class DecisionModelError(RuntimeError):
    """A fixed safe error code; never include raw model output or credentials."""


def routing_request(prompt: str) -> dict[str, Any]:
    """Create the same semantic classification task used by the LLM router."""
    return {
        "state": prompt,
        "questions": {
            "task_type": {
                "type": "choice",
                "instructions": ROUTING_INSTRUCTIONS,
                "criteria": dict(ROUTING_CRITERIA),
            },
        },
    }


def parse_decision(document: dict[str, Any]) -> tuple[str, dict[str, float], float | None]:
    """Validate a complete distribution, chosen label, and native confidence."""
    answer = document.get("answers", {}).get("task_type")
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise DecisionModelError("invalid_choice_answer")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(ROUTING_CRITERIA):
        raise DecisionModelError("invalid_probability_labels")
    if any(
        type(p) not in (int, float) or not math.isfinite(p) or not 0 <= p <= 1
        for p in probabilities.values()
    ) or not math.isclose(sum(probabilities.values()), 1.0, abs_tol=0.001):
        raise DecisionModelError("invalid_probability_distribution")
    choice = answer.get("choice")
    if choice not in probabilities or probabilities[choice] < max(probabilities.values()):
        raise DecisionModelError("choice_not_argmax")
    native = answer.get("confidence")
    if native is not None and (
        type(native) not in (int, float) or not math.isfinite(native) or not 0 <= native <= 1
    ):
        raise DecisionModelError("invalid_native_confidence")
    return choice, probabilities, native


class LocalDecisionRouter:
    """Sequential local inference with synchronized device timing and no API fee."""

    def __init__(
        self, name: str, *, device: str = "cpu", local_files_only: bool = False,
    ) -> None:
        if name not in MODEL_SPECS:
            raise ValueError("unknown local decision model")
        if device not in {"cpu", "mps", "cuda"}:
            raise ValueError("unsupported local device")
        self.name = name
        self.device = device
        self.local_files_only = local_files_only
        self.metadata: dict[str, Any] = {**MODEL_SPECS[name], "device": device}
        self._predict: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self._synchronize: Callable[[], None] = lambda: None

    async def start(self) -> None:
        """Load one explicitly pinned checkpoint; fail instead of changing device."""
        if self._predict is not None:
            return
        try:
            self._load()
        except Exception as exc:
            await self.close()
            code = str(exc) if isinstance(exc, DecisionModelError) else type(exc).__name__
            raise RuntimeError(f"{self.name} initialization failed: {code}") from None

    def _load(self) -> None:
        import torch
        from huggingface_hub import snapshot_download

        if self.device == "mps" and not torch.backends.mps.is_available():
            raise DecisionModelError("mps_unavailable")
        if self.device == "cuda" and not torch.cuda.is_available():
            raise DecisionModelError("cuda_unavailable")
        torch.manual_seed(0)

        def synchronize() -> None:
            if self.device == "mps":
                torch.mps.synchronize()
            elif self.device == "cuda":
                torch.cuda.synchronize()

        self._synchronize = synchronize
        info = self.metadata
        info["packages"] = {
            p: importlib.metadata.version(p)
            for p in ("torch", "transformers", "huggingface-hub", "laya", "strands-decider")
        }
        info["compile"] = False
        info["seed"] = 0
        info["torch_threads"] = torch.get_num_threads()
        if self.name == "laya":
            from laya import Agent

            checkpoint = snapshot_download(
                info["model"], revision=info["revision"],
                local_files_only=self.local_files_only,
                allow_patterns=["rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*"],
            )
            agent = Agent(checkpoint, device=self.device, compile=False, fast=False)
            if str(agent.device) != self.device:
                raise DecisionModelError("device_changed")
            info["dtype"] = str(next(agent.model.parameters()).dtype)
            info["inference_dtype"] = str(agent.dtype_for(1))
            info["checkpoint_routing"] = "disabled"
            info["effective_temperatures"] = {
                "default": agent.temperature, "by_options": agent.temperature_by_options,
            }
            info["temperature_note"] = (
                "Upstream clamps the checkpoint's choice:11+ temperature. "
                "This benchmark uses six options, so that bucket is not used."
            )

            def predict(request: dict[str, Any]) -> dict[str, Any]:
                fallbacks = agent.cpu_fallback_count
                result = agent.system_one(
                    request["state"], request["questions"],
                    max_len=info["context_tokens"], head_max_len=info["head_tokens"],
                )
                if str(agent.device) != self.device or agent.cpu_fallback_count != fallbacks:
                    raise DecisionModelError("device_changed_or_cpu_fallback")
                usage = result.get("usage", {})
                if usage.get("truncated") or usage.get("state_tokens_dropped") or usage.get("options"):
                    raise DecisionModelError("input_truncated_or_options_collapsed")
                return result
        else:
            from strands_decider.infer import EngineConfig, SystemOneEngine
            from strands_decider.modeling import StrandsDeciderModel
            from strands_decider.schema import SystemOneRequest

            checkpoint = snapshot_download(
                info["model"], revision=info["revision"], local_files_only=self.local_files_only,
                allow_patterns=["*.json", "*.safetensors", "*.jinja", "lora/*", "LICENSE.md"],
            )
            backbone = snapshot_download(
                info["base_model"], revision=info["base_revision"],
                local_files_only=self.local_files_only,
                allow_patterns=["*.json", "*.safetensors", "*.jinja"],
            )
            # Upstream does not resolve provenance.base_model_revision. Point a
            # temporary config at the exact local base; do not mutate HF caches.
            with tempfile.TemporaryDirectory(prefix="axon-decision-") as directory:
                copied = Path(directory) / "checkpoint"
                shutil.copytree(checkpoint, copied)
                config_path = copied / "hobson_config.json"
                config = json.loads(config_path.read_text())
                config["base_model"] = backbone
                config_path.write_text(json.dumps(config))
                model = StrandsDeciderModel.load(str(copied))
            engine = SystemOneEngine(model, EngineConfig(
                device=self.device, strict_window=True, model_name=info["model"],
            ))
            info["dtype"] = str(next(engine.model.torso.parameters()).dtype)
            info["strict_window"] = True
            info["shared_prefix_cache"] = "within request; no response cache"
            info["optional_kernel_packages"] = {}
            for package in ("causal-conv1d", "flash-linear-attention", "flash-attn"):
                try:
                    version = importlib.metadata.version(package)
                except importlib.metadata.PackageNotFoundError:
                    version = None
                info["optional_kernel_packages"][package] = version
            info["effective_temperatures"] = {
                "default": engine.model.config.temperature,
                "by_kind": engine.model.config.temperature_by_kind,
            }

            def predict(request: dict[str, Any]) -> dict[str, Any]:
                return engine.evaluate(SystemOneRequest.model_validate(request)).model_dump()

        self._predict = predict
        synchronize()

    async def route(self, prompt: str) -> RouteDecision:
        """Return top probability with the native confidence stored separately."""
        if self._predict is None:
            raise DecisionModelError("model_not_started")
        self._synchronize()
        started = time.perf_counter()
        try:
            document = self._predict(routing_request(prompt))
            self._synchronize()
            choice, probabilities, native = parse_decision(document)
            return RouteDecision(
                strategy=self.name, task_type=choice,
                confidence=probabilities[choice],
                latency_ms=(time.perf_counter() - started) * 1_000,
                cost_usd=0.0, model=self.metadata["model"],
                confidence_kind="top_option_probability_uncalibrated_for_this_corpus",
                probabilities=probabilities, native_confidence=native,
            )
        except Exception as exc:
            self._synchronize()
            return RouteDecision(
                strategy=self.name, task_type=ERROR_LABEL, confidence=0.0,
                latency_ms=(time.perf_counter() - started) * 1_000,
                cost_usd=0.0, model=self.metadata["model"],
                confidence_kind="unavailable",
                error=str(exc) if isinstance(exc, DecisionModelError) else type(exc).__name__,
            )

    async def close(self) -> None:
        """Release a model before the next candidate loads on the same device."""
        self._predict = None
        gc.collect()
        # close() must also work after missing-dependency initialization failures.
        import sys
        torch = sys.modules.get("torch")
        if torch is not None:
            if self.device == "mps" and torch.backends.mps.is_available():
                torch.mps.empty_cache()
            elif self.device == "cuda" and torch.cuda.is_available():
                torch.cuda.empty_cache()
