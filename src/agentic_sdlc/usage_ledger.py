"""Provider-neutral usage ledger: tokens, time, model, worker and cost accounting.

Every AI or agent activity Forge dispatches — planning, context building,
implementation, verification assistance, review, repair — gets one stable
``UsageRecord`` linked to the project, work unit, issue, change request,
lifecycle stage, run, attempt, mission, agent, model, provider and worker it
belongs to. The record carries a pre-dispatch **estimate**, a post-run
**actual**, and the **infrastructure** usage of the run, each optional and each
immutable once written.

Three rules keep the accounting honest:

- **Unknown is unknown.** A provider or runner that does not expose a token
  count, a runtime, or a price yields ``None`` — serialized as ``null`` with an
  explicit status — never a fabricated number. Aggregates sum only known values
  and report how many records were unknown.
- **Subscriptions are not pay-as-you-go.** Claude Max/Code and Codex
  subscription usage is recorded against the plan's capacity unit and, when a
  reference rate is supplied, as a *pay-as-you-go equivalent* that is labeled
  ``subscription`` and never presented as billed dollars.
- **Telemetry carries no credentials or prompts.** No field can hold a secret,
  every document key that looks like one is rejected, and free text is capped
  so a raw prompt cannot be smuggled into an accounting record.

Estimates learn from actuals through ``EstimatorCalibration``: a versioned,
serializable set of correction coefficients keyed by model, stage, task class
and observed complexity, with cold-start fallback to less specific keys and,
finally, to the uncorrected baseline.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, replace
from datetime import datetime
from enum import StrEnum
from typing import Any

from .event_ledger import (
    EventActor,
    LifecycleStage,
    WorkUnitRef,
    load_event_actor,
    load_work_unit_ref,
)
from .project_registry import EMPTY_PROJECT_REGISTRY, ProjectRegistry

__all__ = [
    "CALIBRATION_SCHEMA_VERSION",
    "ESTIMATE_DIMENSIONS",
    "ESTIMATOR_VERSION",
    "GROUP_DIMENSIONS",
    "PRICING_SCHEMA_VERSION",
    "TOKEN_DIMENSIONS",
    "USAGE_LEDGER_SCHEMA_VERSION",
    "USAGE_SCHEMA_TYPES",
    "USAGE_SCHEMA_VERSION",
    "AppliedCoefficient",
    "BillingMode",
    "EstimateError",
    "EstimatorCalibration",
    "InfrastructureUsage",
    "MonetaryEquivalent",
    "MonetaryStatus",
    "PricingSnapshot",
    "TokenCounts",
    "UsageActual",
    "UsageError",
    "UsageEstimate",
    "UsageLedger",
    "UsageRecord",
    "UsageResult",
    "estimate_error",
    "load_pricing_document",
    "load_pricing_snapshot",
    "load_usage_record",
    "monetary_equivalent",
    "secret_bearing_usage_fields",
    "usage_id_for",
]

USAGE_SCHEMA_VERSION = 1
USAGE_LEDGER_SCHEMA_VERSION = 1
PRICING_SCHEMA_VERSION = 1
CALIBRATION_SCHEMA_VERSION = 1
ESTIMATOR_VERSION = "1.0.0"

TOKEN_DIMENSIONS: tuple[str, ...] = ("input", "output", "cacheRead", "cacheWrite")
ESTIMATE_DIMENSIONS: tuple[str, ...] = TOKEN_DIMENSIONS + ("runtimeSeconds",)

#: Free text in an accounting record is bounded so a prompt cannot be smuggled in.
MAX_TEXT_LENGTH = 256
_RATIO_FLOOR = 0.05
_RATIO_CEILING = 20.0

_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_SECRET_KEY = re.compile(
    r"credential|secret|token(?!s$)|password|passphrase|api_?key|authorization|bearer"
    r"|private_?key|prompt(?!sha256$)|messages|completion_text",
    re.I,
)


class UsageError(ValueError):
    """Raised when a usage record, pricing snapshot, or calibration is invalid."""


class BillingMode(StrEnum):
    PAYG = "payg"
    SUBSCRIPTION = "subscription"


class MonetaryStatus(StrEnum):
    """How a monetary figure should be read."""

    PAYG = "payg"
    SUBSCRIPTION = "subscription"
    UNKNOWN = "unknown"


class UsageResult(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ABANDONED = "abandoned"
    INCONCLUSIVE = "inconclusive"
    UNKNOWN = "unknown"


# -- validation helpers -----------------------------------------------------


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise UsageError(message)


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    _require(isinstance(value, Mapping), f"{label} must be an object")
    assert isinstance(value, Mapping)
    for key in value:
        _require(isinstance(key, str), f"{label} keys must be strings")
        _require(
            _SECRET_KEY.search(key) is None,
            f"{label} must not carry credentials, secrets or prompts: {key}",
        )
    return value


def _known_keys(entry: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise UsageError(f"{label} declares unknown keys: " + ", ".join(unknown))


def _text(entry: Mapping[str, Any], key: str, label: str, *, required: bool = False) -> str:
    value = entry.get(key, "")
    if value is None:
        value = ""
    _require(isinstance(value, str), f"{label}: {key} must be a string")
    assert isinstance(value, str)
    value = value.strip()
    _require(not required or bool(value), f"{label}: {key} is required")
    _require(
        len(value) <= MAX_TEXT_LENGTH,
        f"{label}: {key} exceeds {MAX_TEXT_LENGTH} characters; usage records carry "
        "references and labels, never prompts or transcripts",
    )
    return value


def _optional_int(
    entry: Mapping[str, Any], key: str, label: str, *, minimum: int = 0
) -> int | None:
    value = entry.get(key)
    if value is None:
        return None
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= minimum,
        f"{label}: {key} must be an integer >= {minimum} or null",
    )
    assert isinstance(value, int)
    return value


def _optional_float(
    entry: Mapping[str, Any], key: str, label: str, *, minimum: float = 0.0
) -> float | None:
    value = entry.get(key)
    if value is None:
        return None
    _require(
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and value == value
        and value not in (float("inf"), float("-inf"))
        and value >= minimum,
        f"{label}: {key} must be a finite number >= {minimum} or null",
    )
    return float(value)


def _timestamp(value: str, label: str) -> str:
    _require(bool(_TIMESTAMP.fullmatch(value)), f"{label} must be an RFC 3339 timestamp: {value!r}")
    return value


def _optional_timestamp(entry: Mapping[str, Any], key: str, label: str) -> str:
    value = _text(entry, key, label)
    if value:
        _timestamp(value, f"{label}: {key}")
    return value


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _seconds_between(start: str, end: str, label: str) -> int:
    delta = (_parse_time(end) - _parse_time(start)).total_seconds()
    _require(delta >= 0, f"{label}: timestamps run backwards ({start} .. {end})")
    return int(delta)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical(document: Any) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _round(value: float | None, digits: int = 6) -> float | None:
    return None if value is None else round(value, digits)


# -- token counts -----------------------------------------------------------


@dataclass(frozen=True)
class TokenCounts:
    """Input, output and cache token counts; ``None`` means the count is unknown."""

    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None

    def value(self, dimension: str) -> int | None:
        return {
            "input": self.input,
            "output": self.output,
            "cacheRead": self.cache_read,
            "cacheWrite": self.cache_write,
        }[dimension]

    @property
    def status(self) -> str:
        known = [self.value(dimension) is not None for dimension in TOKEN_DIMENSIONS]
        if all(known):
            return "known"
        if not any(known):
            return "unknown"
        return "partial"

    @property
    def total(self) -> int | None:
        """The total is known only when every component is known."""
        if self.status != "known":
            return None
        return sum(self.value(dimension) or 0 for dimension in TOKEN_DIMENSIONS)

    def as_dict(self) -> dict[str, Any]:
        return {
            "input": self.input,
            "output": self.output,
            "cacheRead": self.cache_read,
            "cacheWrite": self.cache_write,
            "total": self.total,
            "status": self.status,
        }


_TOKEN_KEYS = frozenset({"input", "output", "cacheRead", "cacheWrite", "total", "status"})


def _load_tokens(raw: Any, label: str) -> TokenCounts:
    if raw is None:
        return TokenCounts()
    entry = _mapping(raw, label)
    _known_keys(entry, _TOKEN_KEYS, label)
    return TokenCounts(
        input=_optional_int(entry, "input", label),
        output=_optional_int(entry, "output", label),
        cache_read=_optional_int(entry, "cacheRead", label),
        cache_write=_optional_int(entry, "cacheWrite", label),
    )


# -- pricing ----------------------------------------------------------------


@dataclass(frozen=True)
class PricingSnapshot:
    """The versioned price list one monetary figure was computed from.

    A pay-as-you-go snapshot prices tokens directly. A subscription snapshot
    names the plan and its capacity unit; its per-token rates, when present,
    are *reference* rates for a pay-as-you-go equivalent and never bills.
    """

    pricing_id: str
    provider: str
    model: str
    billing_mode: BillingMode
    version: str
    source: str = ""
    effective_at: str = ""
    input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None
    cache_read_usd_per_million: float | None = None
    cache_write_usd_per_million: float | None = None
    plan_name: str = ""
    plan_capacity_unit: str = ""
    plan_monthly_usd: float | None = None
    capacity_units_per_million_tokens: float | None = None

    def rate(self, dimension: str) -> float | None:
        return {
            "input": self.input_usd_per_million,
            "output": self.output_usd_per_million,
            "cacheRead": self.cache_read_usd_per_million,
            "cacheWrite": self.cache_write_usd_per_million,
        }[dimension]

    @property
    def priced_dimensions(self) -> tuple[str, ...]:
        return tuple(item for item in TOKEN_DIMENSIONS if self.rate(item) is not None)

    def usd_for(self, tokens: TokenCounts) -> float | None:
        """Price the priced components; unknown means any priced count is unknown."""
        priced = self.priced_dimensions
        if not priced:
            return None
        total = 0.0
        for dimension in priced:
            count = tokens.value(dimension)
            if count is None:
                return None
            total += count * (self.rate(dimension) or 0.0) / 1_000_000
        return round(total, 6)

    def as_dict(self) -> dict[str, Any]:
        return {
            "pricingId": self.pricing_id,
            "provider": self.provider,
            "model": self.model,
            "billingMode": self.billing_mode.value,
            "version": self.version,
            "source": self.source,
            "effectiveAt": self.effective_at,
            "usdPerMillion": {
                "input": self.input_usd_per_million,
                "output": self.output_usd_per_million,
                "cacheRead": self.cache_read_usd_per_million,
                "cacheWrite": self.cache_write_usd_per_million,
            },
            "plan": {
                "name": self.plan_name,
                "capacityUnit": self.plan_capacity_unit,
                "monthlyUsd": self.plan_monthly_usd,
                "capacityUnitsPerMillionTokens": self.capacity_units_per_million_tokens,
            },
        }


_PRICING_KEYS = frozenset(
    {
        "pricingId",
        "provider",
        "model",
        "billingMode",
        "version",
        "source",
        "effectiveAt",
        "usdPerMillion",
        "plan",
    }
)
_RATE_KEYS = frozenset(TOKEN_DIMENSIONS)
_PLAN_KEYS = frozenset({"name", "capacityUnit", "monthlyUsd", "capacityUnitsPerMillionTokens"})


def load_pricing_snapshot(document: Any) -> PricingSnapshot:
    """Parse one pricing snapshot fail-closed."""
    label = "pricing snapshot"
    entry = _mapping(document, label)
    _known_keys(entry, _PRICING_KEYS, label)
    mode_value = _text(entry, "billingMode", label, required=True)
    try:
        billing_mode = BillingMode(mode_value)
    except ValueError as error:
        raise UsageError(f"{label}: unknown billingMode: {mode_value}") from error
    rates = _mapping(entry.get("usdPerMillion") or {}, f"{label} usdPerMillion")
    _known_keys(rates, _RATE_KEYS, f"{label} usdPerMillion")
    plan = _mapping(entry.get("plan") or {}, f"{label} plan")
    _known_keys(plan, _PLAN_KEYS, f"{label} plan")
    snapshot = PricingSnapshot(
        pricing_id=_text(entry, "pricingId", label, required=True),
        provider=_text(entry, "provider", label, required=True),
        model=_text(entry, "model", label, required=True),
        billing_mode=billing_mode,
        version=_text(entry, "version", label, required=True),
        source=_text(entry, "source", label),
        effective_at=_optional_timestamp(entry, "effectiveAt", label),
        input_usd_per_million=_optional_float(rates, "input", f"{label} usdPerMillion"),
        output_usd_per_million=_optional_float(rates, "output", f"{label} usdPerMillion"),
        cache_read_usd_per_million=_optional_float(rates, "cacheRead", f"{label} usdPerMillion"),
        cache_write_usd_per_million=_optional_float(rates, "cacheWrite", f"{label} usdPerMillion"),
        plan_name=_text(plan, "name", f"{label} plan"),
        plan_capacity_unit=_text(plan, "capacityUnit", f"{label} plan"),
        plan_monthly_usd=_optional_float(plan, "monthlyUsd", f"{label} plan"),
        capacity_units_per_million_tokens=_optional_float(
            plan, "capacityUnitsPerMillionTokens", f"{label} plan"
        ),
    )
    if billing_mode is BillingMode.PAYG:
        _require(
            snapshot.input_usd_per_million is not None
            and snapshot.output_usd_per_million is not None,
            f"{label} {snapshot.pricing_id}: pay-as-you-go pricing requires input and output "
            "usdPerMillion rates",
        )
        _require(
            not snapshot.plan_name and not snapshot.plan_capacity_unit,
            f"{label} {snapshot.pricing_id}: pay-as-you-go pricing cannot declare a plan",
        )
    else:
        _require(
            bool(snapshot.plan_name) and bool(snapshot.plan_capacity_unit),
            f"{label} {snapshot.pricing_id}: subscription pricing requires plan.name and "
            "plan.capacityUnit",
        )
    return snapshot


def load_pricing_document(document: Any) -> dict[str, PricingSnapshot]:
    """Parse a ``{"schemaVersion": 1, "pricing": [...]}`` document into snapshots by ID."""
    label = "pricing document"
    entry = _mapping(document, label)
    _known_keys(entry, frozenset({"schemaVersion", "pricing"}), label)
    _require(
        entry.get("schemaVersion") == PRICING_SCHEMA_VERSION,
        f"unsupported pricing document schemaVersion: {entry.get('schemaVersion')!r}",
    )
    raw = entry.get("pricing", [])
    _require(isinstance(raw, list), f"{label}: pricing must be an array")
    assert isinstance(raw, list)
    snapshots: dict[str, PricingSnapshot] = {}
    for item in raw:
        snapshot = load_pricing_snapshot(item)
        _require(
            snapshot.pricing_id not in snapshots,
            f"{label}: duplicate pricingId {snapshot.pricing_id}",
        )
        snapshots[snapshot.pricing_id] = snapshot
    return snapshots


# -- monetary equivalent ----------------------------------------------------


@dataclass(frozen=True)
class MonetaryEquivalent:
    """What a run cost, or would have cost, stated in the only terms that are true.

    ``billed_usd`` is set only for pay-as-you-go pricing. Subscription runs
    carry a ``payg_equivalent_usd`` reference figure (when reference rates and
    token counts exist) and the plan capacity they consumed, and are labeled
    ``subscription`` so no dashboard can present them as dollars billed.
    """

    status: MonetaryStatus
    pricing: PricingSnapshot | None = None
    payg_equivalent_usd: float | None = None
    billed_usd: float | None = None
    plan_capacity_units: float | None = None
    plan_capacity_unit: str = ""
    basis: str = ""

    @property
    def billing_mode(self) -> str:
        return self.pricing.billing_mode.value if self.pricing else ""

    @property
    def pricing_id(self) -> str:
        return self.pricing.pricing_id if self.pricing else ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "billingMode": self.billing_mode,
            "pricingId": self.pricing_id,
            "pricing": self.pricing.as_dict() if self.pricing else None,
            "paygEquivalentUsd": self.payg_equivalent_usd,
            "billedUsd": self.billed_usd,
            "planCapacityUnits": self.plan_capacity_units,
            "planCapacityUnit": self.plan_capacity_unit,
            "basis": self.basis,
        }


def monetary_equivalent(
    tokens: TokenCounts,
    pricing: PricingSnapshot | None,
    *,
    plan_capacity_units: float | None = None,
) -> MonetaryEquivalent:
    """Derive the monetary equivalent of ``tokens`` under ``pricing`` without inventing."""
    if pricing is None:
        _require(
            plan_capacity_units is None,
            "plan capacity units require a subscription pricing snapshot naming the unit",
        )
        return MonetaryEquivalent(status=MonetaryStatus.UNKNOWN, basis="no pricing snapshot")

    usd = pricing.usd_for(tokens)
    unknown_priced = [item for item in pricing.priced_dimensions if tokens.value(item) is None]

    if pricing.billing_mode is BillingMode.PAYG:
        _require(
            plan_capacity_units is None,
            "pay-as-you-go pricing has no plan capacity unit",
        )
        if usd is None:
            return MonetaryEquivalent(
                status=MonetaryStatus.UNKNOWN,
                pricing=pricing,
                basis="token counts unknown for priced components: " + ", ".join(unknown_priced),
            )
        return MonetaryEquivalent(
            status=MonetaryStatus.PAYG,
            pricing=pricing,
            payg_equivalent_usd=usd,
            billed_usd=usd,
            basis="tokens priced at the pay-as-you-go snapshot rates",
        )

    _require(
        plan_capacity_units is None or plan_capacity_units >= 0,
        "plan capacity units cannot be negative",
    )
    capacity_basis = "plan capacity units unknown"
    units = plan_capacity_units
    if units is not None:
        capacity_basis = "plan capacity units observed"
    elif pricing.capacity_units_per_million_tokens is not None and tokens.total is not None:
        units = round(tokens.total * pricing.capacity_units_per_million_tokens / 1_000_000, 6)
        capacity_basis = "plan capacity units estimated from tokens"
    if usd is None:
        equivalent_basis = (
            "no reference rates in the subscription snapshot"
            if not pricing.priced_dimensions
            else "token counts unknown for reference-priced components: "
            + ", ".join(unknown_priced)
        )
    else:
        equivalent_basis = "pay-as-you-go equivalent at reference rates; not billed"
    return MonetaryEquivalent(
        status=MonetaryStatus.SUBSCRIPTION,
        pricing=pricing,
        payg_equivalent_usd=usd,
        billed_usd=None,
        plan_capacity_units=units,
        plan_capacity_unit=pricing.plan_capacity_unit,
        basis=f"{equivalent_basis}; {capacity_basis}",
    )


_MONETARY_KEYS = frozenset(
    {
        "status",
        "billingMode",
        "pricingId",
        "pricing",
        "paygEquivalentUsd",
        "billedUsd",
        "planCapacityUnits",
        "planCapacityUnit",
        "basis",
    }
)


def _load_monetary(raw: Any, label: str) -> MonetaryEquivalent:
    if raw is None:
        return MonetaryEquivalent(status=MonetaryStatus.UNKNOWN, basis="no pricing snapshot")
    entry = _mapping(raw, label)
    _known_keys(entry, _MONETARY_KEYS, label)
    status_value = _text(entry, "status", label, required=True)
    try:
        status = MonetaryStatus(status_value)
    except ValueError as error:
        raise UsageError(f"{label}: unknown status: {status_value}") from error
    pricing = load_pricing_snapshot(entry["pricing"]) if entry.get("pricing") else None
    monetary = MonetaryEquivalent(
        status=status,
        pricing=pricing,
        payg_equivalent_usd=_optional_float(entry, "paygEquivalentUsd", label),
        billed_usd=_optional_float(entry, "billedUsd", label),
        plan_capacity_units=_optional_float(entry, "planCapacityUnits", label),
        plan_capacity_unit=_text(entry, "planCapacityUnit", label),
        basis=_text(entry, "basis", label),
    )
    declared_id = _text(entry, "pricingId", label)
    _require(
        not declared_id or declared_id == monetary.pricing_id,
        f"{label}: pricingId does not match the embedded pricing snapshot",
    )
    if status is MonetaryStatus.PAYG:
        _require(
            pricing is not None and pricing.billing_mode is BillingMode.PAYG,
            f"{label}: status payg requires an embedded pay-as-you-go pricing snapshot",
        )
        _require(
            monetary.billed_usd is not None,
            f"{label}: status payg requires billedUsd",
        )
    elif status is MonetaryStatus.SUBSCRIPTION:
        _require(
            pricing is not None and pricing.billing_mode is BillingMode.SUBSCRIPTION,
            f"{label}: status subscription requires an embedded subscription pricing snapshot",
        )
        _require(
            monetary.billed_usd is None,
            f"{label}: subscription usage is never billed per run; billedUsd must be null",
        )
    else:
        _require(
            monetary.billed_usd is None and monetary.payg_equivalent_usd is None,
            f"{label}: status unknown cannot carry monetary values",
        )
    return monetary


# -- estimate, actual, infrastructure ---------------------------------------


@dataclass(frozen=True)
class AppliedCoefficient:
    """Which calibration key corrected one estimated dimension, and by how much."""

    dimension: str
    key: str
    samples: int
    coefficient: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "key": self.key,
            "samples": self.samples,
            "coefficient": self.coefficient,
        }


@dataclass(frozen=True)
class UsageEstimate:
    """What a run was expected to consume before it was dispatched."""

    tokens: TokenCounts = TokenCounts()
    runtime_seconds: int | None = None
    monetary: MonetaryEquivalent = MonetaryEquivalent(status=MonetaryStatus.UNKNOWN)
    estimator_version: str = ESTIMATOR_VERSION
    basis: str = ""
    calibration: tuple[AppliedCoefficient, ...] = ()

    def value(self, dimension: str) -> int | None:
        if dimension == "runtimeSeconds":
            return self.runtime_seconds
        return self.tokens.value(dimension)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens.as_dict(),
            "runtimeSeconds": self.runtime_seconds,
            "monetary": self.monetary.as_dict(),
            "estimatorVersion": self.estimator_version,
            "basis": self.basis,
            "calibration": [item.as_dict() for item in self.calibration],
        }


@dataclass(frozen=True)
class UsageActual:
    """What a run actually consumed, as far as the provider and runner exposed it."""

    tokens: TokenCounts = TokenCounts()
    runtime_seconds: int | None = None
    wait_seconds: int | None = None
    monetary: MonetaryEquivalent = MonetaryEquivalent(status=MonetaryStatus.UNKNOWN)
    result: UsageResult = UsageResult.UNKNOWN
    source: str = ""
    queued_at: str = ""
    started_at: str = ""
    finished_at: str = ""

    def __post_init__(self) -> None:
        """Derive runtime and wait from timestamps when they were not observed directly."""
        for name in ("queued_at", "started_at", "finished_at"):
            value = getattr(self, name)
            if value:
                _timestamp(value, f"usage actual: {name}")
        if self.started_at and self.finished_at:
            observed = _seconds_between(
                self.started_at, self.finished_at, "usage actual: startedAt/finishedAt"
            )
            if self.runtime_seconds is None:
                object.__setattr__(self, "runtime_seconds", observed)
        if self.queued_at and self.started_at:
            observed = _seconds_between(
                self.queued_at, self.started_at, "usage actual: queuedAt/startedAt"
            )
            if self.wait_seconds is None:
                object.__setattr__(self, "wait_seconds", observed)
        _require(
            self.runtime_seconds is None or self.runtime_seconds >= 0,
            "usage actual: runtimeSeconds cannot be negative",
        )
        _require(
            self.wait_seconds is None or self.wait_seconds >= 0,
            "usage actual: waitSeconds cannot be negative",
        )

    def value(self, dimension: str) -> int | None:
        if dimension == "runtimeSeconds":
            return self.runtime_seconds
        return self.tokens.value(dimension)

    def as_dict(self) -> dict[str, Any]:
        return {
            "tokens": self.tokens.as_dict(),
            "runtimeSeconds": self.runtime_seconds,
            "waitSeconds": self.wait_seconds,
            "monetary": self.monetary.as_dict(),
            "result": self.result.value,
            "source": self.source,
            "queuedAt": self.queued_at,
            "startedAt": self.started_at,
            "finishedAt": self.finished_at,
        }


@dataclass(frozen=True)
class InfrastructureUsage:
    """Runner, CI-minute, storage and network usage — never mixed into AI cost."""

    runner_class: str = ""
    runner_seconds: int | None = None
    ci_minutes: float | None = None
    cost_usd: float | None = None
    storage_bytes: int | None = None
    network_bytes: int | None = None
    basis: str = ""

    @property
    def status(self) -> str:
        return "known" if self.cost_usd is not None else "unknown"

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "runnerClass": self.runner_class,
            "runnerSeconds": self.runner_seconds,
            "ciMinutes": self.ci_minutes,
            "costUsd": self.cost_usd,
            "storageBytes": self.storage_bytes,
            "networkBytes": self.network_bytes,
            "basis": self.basis,
        }


_ESTIMATE_KEYS = frozenset(
    {"tokens", "runtimeSeconds", "monetary", "estimatorVersion", "basis", "calibration"}
)
_COEFFICIENT_KEYS = frozenset({"dimension", "key", "samples", "coefficient"})
_ACTUAL_KEYS = frozenset(
    {
        "tokens",
        "runtimeSeconds",
        "waitSeconds",
        "monetary",
        "result",
        "source",
        "queuedAt",
        "startedAt",
        "finishedAt",
    }
)
_INFRASTRUCTURE_KEYS = frozenset(
    {
        "status",
        "runnerClass",
        "runnerSeconds",
        "ciMinutes",
        "costUsd",
        "storageBytes",
        "networkBytes",
        "basis",
    }
)


def _load_estimate(raw: Any) -> UsageEstimate | None:
    if raw is None:
        return None
    label = "usage estimate"
    entry = _mapping(raw, label)
    _known_keys(entry, _ESTIMATE_KEYS, label)
    calibration = []
    raw_calibration = entry.get("calibration") or []
    _require(isinstance(raw_calibration, list), f"{label}: calibration must be an array")
    for item in raw_calibration:
        coefficient = _mapping(item, f"{label} calibration")
        _known_keys(coefficient, _COEFFICIENT_KEYS, f"{label} calibration")
        dimension = _text(coefficient, "dimension", f"{label} calibration", required=True)
        _require(
            dimension in ESTIMATE_DIMENSIONS,
            f"{label} calibration: unknown dimension {dimension}",
        )
        samples = _optional_int(coefficient, "samples", f"{label} calibration")
        value = _optional_float(coefficient, "coefficient", f"{label} calibration")
        _require(
            samples is not None and value is not None,
            f"{label} calibration: samples and coefficient are required",
        )
        assert samples is not None and value is not None
        calibration.append(
            AppliedCoefficient(
                dimension=dimension,
                key=_text(coefficient, "key", f"{label} calibration", required=True),
                samples=samples,
                coefficient=value,
            )
        )
    return UsageEstimate(
        tokens=_load_tokens(entry.get("tokens"), f"{label} tokens"),
        runtime_seconds=_optional_int(entry, "runtimeSeconds", label),
        monetary=_load_monetary(entry.get("monetary"), f"{label} monetary"),
        estimator_version=_text(entry, "estimatorVersion", label) or ESTIMATOR_VERSION,
        basis=_text(entry, "basis", label),
        calibration=tuple(calibration),
    )


def _load_actual(raw: Any) -> UsageActual | None:
    if raw is None:
        return None
    label = "usage actual"
    entry = _mapping(raw, label)
    _known_keys(entry, _ACTUAL_KEYS, label)
    result_value = _text(entry, "result", label) or UsageResult.UNKNOWN.value
    try:
        result = UsageResult(result_value)
    except ValueError as error:
        raise UsageError(f"{label}: unknown result: {result_value}") from error
    return UsageActual(
        tokens=_load_tokens(entry.get("tokens"), f"{label} tokens"),
        runtime_seconds=_optional_int(entry, "runtimeSeconds", label),
        wait_seconds=_optional_int(entry, "waitSeconds", label),
        monetary=_load_monetary(entry.get("monetary"), f"{label} monetary"),
        result=result,
        source=_text(entry, "source", label),
        queued_at=_optional_timestamp(entry, "queuedAt", label),
        started_at=_optional_timestamp(entry, "startedAt", label),
        finished_at=_optional_timestamp(entry, "finishedAt", label),
    )


def _load_infrastructure(raw: Any) -> InfrastructureUsage | None:
    if raw is None:
        return None
    label = "infrastructure usage"
    entry = _mapping(raw, label)
    _known_keys(entry, _INFRASTRUCTURE_KEYS, label)
    return InfrastructureUsage(
        runner_class=_text(entry, "runnerClass", label),
        runner_seconds=_optional_int(entry, "runnerSeconds", label),
        ci_minutes=_optional_float(entry, "ciMinutes", label),
        cost_usd=_optional_float(entry, "costUsd", label),
        storage_bytes=_optional_int(entry, "storageBytes", label),
        network_bytes=_optional_int(entry, "networkBytes", label),
        basis=_text(entry, "basis", label),
    )


# -- estimate error -----------------------------------------------------------


@dataclass(frozen=True)
class EstimateError:
    """Estimate-versus-actual error per dimension, only where both sides are known."""

    dimensions: Mapping[str, dict[str, Any]]
    status: str
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "dimensions": {name: dict(value) for name, value in self.dimensions.items()},
        }


def _dimension_error(estimated: float, actual: float) -> dict[str, Any]:
    absolute = actual - estimated
    percentage = round(absolute / estimated * 100, 2) if estimated > 0 else None
    return {
        "estimated": estimated,
        "actual": actual,
        "absolute": _round(absolute),
        "percentage": percentage,
        "percentageBasis": (
            "absolute error over the estimate" if estimated > 0 else "estimate was zero"
        ),
    }


def estimate_error(record: UsageRecord) -> EstimateError:
    """Compare a record's estimate to its actual, dimension by dimension."""
    if record.estimate is None or record.actual is None:
        missing = "estimate" if record.estimate is None else "actual"
        return EstimateError(dimensions={}, status="unavailable", reason=f"no {missing} recorded")
    dimensions: dict[str, dict[str, Any]] = {}
    for dimension in ESTIMATE_DIMENSIONS:
        estimated = record.estimate.value(dimension)
        actual = record.actual.value(dimension)
        if estimated is not None and actual is not None:
            dimensions[dimension] = _dimension_error(float(estimated), float(actual))
    estimated_total = record.estimate.tokens.total
    actual_total = record.actual.tokens.total
    if estimated_total is not None and actual_total is not None:
        dimensions["totalTokens"] = _dimension_error(float(estimated_total), float(actual_total))
    estimated_usd = record.estimate.monetary.payg_equivalent_usd
    actual_usd = record.actual.monetary.payg_equivalent_usd
    if estimated_usd is not None and actual_usd is not None:
        dimensions["paygEquivalentUsd"] = _dimension_error(estimated_usd, actual_usd)
    if not dimensions:
        return EstimateError(
            dimensions={},
            status="unavailable",
            reason="no dimension has both an estimated and an actual value",
        )
    return EstimateError(dimensions=dimensions, status="available")


# -- usage record -------------------------------------------------------------


def usage_id_for(project_id: str, work_unit_id: str, run_id: str, attempt: int = 1) -> str:
    """A stable, content-derived identity for one run attempt of one work unit."""
    _require(bool(project_id.strip()) and bool(work_unit_id.strip()), "project and unit required")
    _require(bool(run_id.strip()), "run_id is required")
    _require(attempt >= 1, "attempt numbers start at 1")
    identity = _canonical([project_id.strip(), work_unit_id.strip(), run_id.strip(), attempt])
    return "usage-" + _sha256_text(identity)[:16]


@dataclass(frozen=True)
class UsageRecord:
    """One stable accounting record for one run attempt of one work unit."""

    usage_id: str
    work_unit: WorkUnitRef
    stage: LifecycleStage
    run_id: str
    recorded_at: str
    actor: EventActor
    attempt: int = 1
    task_class: str = ""
    mission_id: str = ""
    mission_version: str = ""
    lifecycle_event_id: str = ""
    complexity_class: str = ""
    review_round: int | None = None
    repair_cycle: int | None = None
    estimate: UsageEstimate | None = None
    actual: UsageActual | None = None
    infrastructure: InfrastructureUsage | None = None
    schema_version: int = USAGE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Reject an unusable record at construction, not deep inside an aggregate."""
        _require(bool(self.usage_id.strip()), "usage record: usageId is required")
        _require(bool(self.run_id.strip()), "usage record: runId is required")
        _require(
            isinstance(self.attempt, int)
            and not isinstance(self.attempt, bool)
            and self.attempt >= 1,
            "usage record: attempt numbers start at 1",
        )
        _timestamp(self.recorded_at, "usage record: recordedAt")

    @property
    def project_id(self) -> str:
        return self.work_unit.project_id

    @property
    def work_unit_id(self) -> str:
        return self.work_unit.work_unit_id

    @property
    def run_scope(self) -> tuple[str, str, str]:
        return self.project_id, self.work_unit_id, self.run_id

    @property
    def occurred_at(self) -> str:
        """When the usage happened: the run's end, else its start, else the record time."""
        if self.actual is not None:
            return self.actual.finished_at or self.actual.started_at or self.recorded_at
        return self.recorded_at

    @property
    def estimate_error(self) -> EstimateError:
        return estimate_error(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schema_version,
            "usageId": self.usage_id,
            "workUnit": self.work_unit.as_dict(),
            "stage": self.stage.value,
            "taskClass": self.task_class,
            "runId": self.run_id,
            "attempt": self.attempt,
            "missionId": self.mission_id,
            "missionVersion": self.mission_version,
            "actor": self.actor.as_dict(),
            "lifecycleEventId": self.lifecycle_event_id,
            "complexityClass": self.complexity_class,
            "reviewRound": self.review_round,
            "repairCycle": self.repair_cycle,
            "recordedAt": self.recorded_at,
            "estimate": self.estimate.as_dict() if self.estimate else None,
            "actual": self.actual.as_dict() if self.actual else None,
            "infrastructure": self.infrastructure.as_dict() if self.infrastructure else None,
        }


_RECORD_KEYS = frozenset(
    {
        "schemaVersion",
        "usageId",
        "workUnit",
        "stage",
        "taskClass",
        "runId",
        "attempt",
        "missionId",
        "missionVersion",
        "actor",
        "lifecycleEventId",
        "complexityClass",
        "reviewRound",
        "repairCycle",
        "recordedAt",
        "estimate",
        "actual",
        "infrastructure",
    }
)


def load_usage_record(document: Any) -> UsageRecord:
    """Parse one usage record fail-closed."""
    label = "usage record"
    entry = _mapping(document, label)
    _known_keys(entry, _RECORD_KEYS, label)
    version = entry.get("schemaVersion", USAGE_SCHEMA_VERSION)
    _require(
        version == USAGE_SCHEMA_VERSION,
        f"unsupported usage record schemaVersion: {version!r}",
    )
    stage_value = _text(entry, "stage", label, required=True)
    try:
        stage = LifecycleStage(stage_value)
    except ValueError as error:
        raise UsageError(f"{label}: unknown stage: {stage_value}") from error
    attempt = _optional_int(entry, "attempt", label, minimum=1)
    try:
        work_unit = load_work_unit_ref(entry.get("workUnit", {}))
        actor = load_event_actor(entry.get("actor", {}))
    except ValueError as error:  # LedgerError is a ValueError
        raise UsageError(f"{label}: {error}") from error
    return UsageRecord(
        usage_id=_text(entry, "usageId", label, required=True),
        work_unit=work_unit,
        stage=stage,
        task_class=_text(entry, "taskClass", label),
        run_id=_text(entry, "runId", label, required=True),
        attempt=attempt if attempt is not None else 1,
        mission_id=_text(entry, "missionId", label),
        mission_version=_text(entry, "missionVersion", label),
        actor=actor,
        lifecycle_event_id=_text(entry, "lifecycleEventId", label),
        complexity_class=_text(entry, "complexityClass", label),
        review_round=_optional_int(entry, "reviewRound", label),
        repair_cycle=_optional_int(entry, "repairCycle", label),
        recorded_at=_timestamp(
            _text(entry, "recordedAt", label, required=True), f"{label}: recordedAt"
        ),
        estimate=_load_estimate(entry.get("estimate")),
        actual=_load_actual(entry.get("actual")),
        infrastructure=_load_infrastructure(entry.get("infrastructure")),
    )


# -- aggregation --------------------------------------------------------------


class _Sum:
    """A sum that remembers how many contributions were unknown."""

    __slots__ = ("known", "total", "unknown")

    def __init__(self) -> None:
        self.total = 0.0
        self.known = 0
        self.unknown = 0

    def add(self, value: float | int | None) -> None:
        if value is None:
            self.unknown += 1
        else:
            self.total += value
            self.known += 1

    def as_dict(self) -> dict[str, Any]:
        value: float | int | None
        if self.known == 0:
            value = None
        elif float(self.total).is_integer():
            value = int(self.total)
        else:
            value = round(self.total, 6)
        return {"value": value, "knownRecords": self.known, "unknownRecords": self.unknown}


class _Mean:
    __slots__ = ("count", "total")

    def __init__(self) -> None:
        self.total = 0.0
        self.count = 0

    def add(self, value: float) -> None:
        self.total += value
        self.count += 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.total / self.count, 4) if self.count else None,
            "samples": self.count,
        }


def _iso_week(timestamp: str) -> str:
    year, week, _ = _parse_time(timestamp).isocalendar()
    return f"{year}-W{week:02d}"


GROUP_DIMENSIONS: Mapping[str, str] = {
    "project": "projectId",
    "workUnit": "workUnitId",
    "issue": "issueRef",
    "changeRequest": "changeRequestRef",
    "run": "runId",
    "stage": "stage",
    "taskClass": "taskClass",
    "mission": "missionId",
    "agent": "agentId",
    "provider": "provider",
    "model": "model",
    "modelAlias": "modelAlias",
    "worker": "worker",
    "reviewRound": "reviewRound",
    "repairCycle": "repairCycle",
    "billingMode": "billingMode",
    "result": "result",
    "day": "day",
    "week": "week",
    "month": "month",
}


def _group_value(record: UsageRecord, dimension: str) -> str:
    monetary = record.actual.monetary if record.actual else None
    if monetary is None or monetary.status is MonetaryStatus.UNKNOWN:
        monetary = record.estimate.monetary if record.estimate else None
    lookup = {
        "project": record.project_id,
        "workUnit": record.work_unit_id,
        "issue": record.work_unit.issue_ref,
        "changeRequest": record.work_unit.change_request_ref,
        "run": record.run_id,
        "stage": record.stage.value,
        "taskClass": record.task_class,
        "mission": record.mission_id,
        "agent": record.actor.agent_id or record.actor.name,
        "provider": record.actor.provider,
        "model": record.actor.model,
        "modelAlias": record.actor.model_alias,
        "worker": record.actor.worker,
        "reviewRound": "" if record.review_round is None else str(record.review_round),
        "repairCycle": "" if record.repair_cycle is None else str(record.repair_cycle),
        "billingMode": monetary.billing_mode if monetary else "",
        "result": record.actual.result.value if record.actual else UsageResult.UNKNOWN.value,
        "day": record.occurred_at[:10],
        "week": _iso_week(record.occurred_at),
        "month": record.occurred_at[:7],
    }
    return lookup[dimension]


class _Bucket:
    """Accumulates one aggregation bucket; unknown values are counted, never guessed."""

    def __init__(self, key: Mapping[str, str]) -> None:
        self.key = dict(key)
        self.records = 0
        self.runs: set[tuple[str, str, str]] = set()
        self.retry_attempts = 0
        self.by_result: dict[str, int] = {}
        self.estimated_tokens = {name: _Sum() for name in TOKEN_DIMENSIONS}
        self.estimated_total = _Sum()
        self.estimated_runtime = _Sum()
        self.estimated_payg = _Sum()
        self.estimated_capacity: dict[str, _Sum] = {}
        self.actual_tokens = {name: _Sum() for name in TOKEN_DIMENSIONS}
        self.actual_total = _Sum()
        self.actual_runtime = _Sum()
        self.actual_wait = _Sum()
        self.actual_payg = _Sum()
        self.actual_billed = _Sum()
        self.actual_capacity: dict[str, _Sum] = {}
        self.runtime_by_result: dict[str, _Sum] = {}
        self.monetary_status: dict[str, int] = {}
        self.error_abs_pct: dict[str, _Mean] = {}
        self.infra_cost = _Sum()
        self.infra_runner_seconds = _Sum()
        self.infra_ci_minutes = _Sum()

    def add(self, record: UsageRecord) -> None:
        self.records += 1
        self.runs.add(record.run_scope)
        if record.attempt > 1:
            self.retry_attempts += 1
        result = record.actual.result.value if record.actual else UsageResult.UNKNOWN.value
        self.by_result[result] = self.by_result.get(result, 0) + 1

        estimate = record.estimate
        for name in TOKEN_DIMENSIONS:
            self.estimated_tokens[name].add(estimate.tokens.value(name) if estimate else None)
        self.estimated_total.add(estimate.tokens.total if estimate else None)
        self.estimated_runtime.add(estimate.runtime_seconds if estimate else None)
        self.estimated_payg.add(estimate.monetary.payg_equivalent_usd if estimate else None)
        if estimate and estimate.monetary.plan_capacity_unit:
            self.estimated_capacity.setdefault(estimate.monetary.plan_capacity_unit, _Sum()).add(
                estimate.monetary.plan_capacity_units
            )

        actual = record.actual
        for name in TOKEN_DIMENSIONS:
            self.actual_tokens[name].add(actual.tokens.value(name) if actual else None)
        self.actual_total.add(actual.tokens.total if actual else None)
        self.actual_runtime.add(actual.runtime_seconds if actual else None)
        self.actual_wait.add(actual.wait_seconds if actual else None)
        self.actual_payg.add(actual.monetary.payg_equivalent_usd if actual else None)
        self.actual_billed.add(actual.monetary.billed_usd if actual else None)
        self.runtime_by_result.setdefault(result, _Sum()).add(
            actual.runtime_seconds if actual else None
        )
        status = actual.monetary.status.value if actual else MonetaryStatus.UNKNOWN.value
        self.monetary_status[status] = self.monetary_status.get(status, 0) + 1
        if actual and actual.monetary.plan_capacity_unit:
            self.actual_capacity.setdefault(actual.monetary.plan_capacity_unit, _Sum()).add(
                actual.monetary.plan_capacity_units
            )

        error = record.estimate_error
        for name, value in error.dimensions.items():
            if value["percentage"] is not None:
                self.error_abs_pct.setdefault(name, _Mean()).add(abs(value["percentage"]))

        infra = record.infrastructure
        self.infra_cost.add(infra.cost_usd if infra else None)
        self.infra_runner_seconds.add(infra.runner_seconds if infra else None)
        self.infra_ci_minutes.add(infra.ci_minutes if infra else None)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "records": self.records,
            "runs": len(self.runs),
            "retryAttempts": self.retry_attempts,
            "byResult": dict(sorted(self.by_result.items())),
            "estimated": {
                "tokens": {name: total.as_dict() for name, total in self.estimated_tokens.items()},
                "totalTokens": self.estimated_total.as_dict(),
                "runtimeSeconds": self.estimated_runtime.as_dict(),
                "paygEquivalentUsd": self.estimated_payg.as_dict(),
                "planCapacityUnits": {
                    unit: total.as_dict() for unit, total in sorted(self.estimated_capacity.items())
                },
            },
            "actual": {
                "tokens": {name: total.as_dict() for name, total in self.actual_tokens.items()},
                "totalTokens": self.actual_total.as_dict(),
                "runtimeSeconds": self.actual_runtime.as_dict(),
                "waitSeconds": self.actual_wait.as_dict(),
                "runtimeSecondsByResult": {
                    name: total.as_dict() for name, total in sorted(self.runtime_by_result.items())
                },
                "paygEquivalentUsd": self.actual_payg.as_dict(),
                "billedUsd": self.actual_billed.as_dict(),
                "planCapacityUnits": {
                    unit: total.as_dict() for unit, total in sorted(self.actual_capacity.items())
                },
                "monetaryStatus": dict(sorted(self.monetary_status.items())),
            },
            "estimateError": {
                "meanAbsolutePercentage": {
                    name: mean.as_dict() for name, mean in sorted(self.error_abs_pct.items())
                }
            },
            "infrastructure": {
                "costUsd": self.infra_cost.as_dict(),
                "runnerSeconds": self.infra_runner_seconds.as_dict(),
                "ciMinutes": self.infra_ci_minutes.as_dict(),
            },
        }


# -- ledger -------------------------------------------------------------------


def _merge_section(existing: Any, incoming: Any, usage_id: str, section: str) -> Any:
    """A section can be filled once; it can never be changed or blanked."""
    if incoming is None:
        return existing
    if existing is None:
        return incoming
    _require(
        existing.as_dict() == incoming.as_dict(),
        f"usage record {usage_id}: {section} is immutable once recorded",
    )
    return existing


class UsageLedger:
    """Stable, immutable-once-written usage records across every Forge project.

    A record is identified by its ``usage_id``. Re-appending it with an
    identical payload is a no-op; appending it with a section that was still
    unknown (an actual arriving after the estimate) fills that section; any
    other difference fails closed. History is therefore never rewritten, while
    a run can still be accounted for in two moments.
    """

    def __init__(self, *, registry: ProjectRegistry = EMPTY_PROJECT_REGISTRY) -> None:
        self._registry = registry
        self._records: dict[str, UsageRecord] = {}

    @property
    def registry(self) -> ProjectRegistry:
        return self._registry

    @property
    def records(self) -> tuple[UsageRecord, ...]:
        return tuple(self._records.values())

    def __len__(self) -> int:
        return len(self._records)

    def get(self, usage_id: str) -> UsageRecord:
        record = self._records.get(usage_id)
        _require(record is not None, f"unknown usage record: {usage_id}")
        assert record is not None
        return record

    def append(self, record: UsageRecord) -> UsageRecord:
        """Record usage; replay is a no-op, filling an unknown section is allowed."""
        if not self._registry.is_empty:
            _require(
                record.project_id in self._registry,
                f"unregistered project: {record.project_id}",
            )
        existing = self._records.get(record.usage_id)
        if existing is None:
            self._records[record.usage_id] = record
            return record
        _require(
            replace(existing, estimate=None, actual=None, infrastructure=None).as_dict()
            == replace(record, estimate=None, actual=None, infrastructure=None).as_dict(),
            f"usage record {record.usage_id}: identity and references are immutable",
        )
        estimate = _merge_section(existing.estimate, record.estimate, record.usage_id, "estimate")
        actual = _merge_section(existing.actual, record.actual, record.usage_id, "actual")
        infrastructure = _merge_section(
            existing.infrastructure, record.infrastructure, record.usage_id, "infrastructure"
        )
        merged = replace(existing, estimate=estimate, actual=actual, infrastructure=infrastructure)
        self._records[record.usage_id] = merged
        return merged

    def extend(self, records: Iterable[UsageRecord]) -> tuple[UsageRecord, ...]:
        return tuple(self.append(record) for record in records)

    # -- queries -----------------------------------------------------------

    def _scope(
        self, project_id: str | None, project_ids: Sequence[str] | None
    ) -> frozenset[str] | None:
        _require(project_id is None or project_ids is None, "pass project_id or project_ids")
        if project_id is not None:
            return frozenset({project_id})
        if project_ids is not None:
            return frozenset(project_ids)
        return None

    def query(
        self,
        *,
        project_id: str | None = None,
        project_ids: Sequence[str] | None = None,
        work_unit_id: str | None = None,
        stage: LifecycleStage | str | None = None,
        model: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> tuple[UsageRecord, ...]:
        """Filter records; project scoping is exact so projects never leak."""
        scope = self._scope(project_id, project_ids)
        wanted_stage = LifecycleStage(stage) if stage is not None else None
        return tuple(
            record
            for record in sorted(self._records.values(), key=lambda r: (r.occurred_at, r.usage_id))
            if (scope is None or record.project_id in scope)
            and (work_unit_id is None or record.work_unit_id == work_unit_id)
            and (wanted_stage is None or record.stage is wanted_stage)
            and (model is None or record.actor.model == model)
            and (since is None or record.occurred_at >= since)
            and (until is None or record.occurred_at <= until)
        )

    def aggregate(
        self,
        *,
        group_by: Sequence[str] = ("project",),
        project_id: str | None = None,
        project_ids: Sequence[str] | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Roll usage up by any combination of the documented group dimensions."""
        dimensions = tuple(group_by)
        _require(bool(dimensions), "group_by requires at least one dimension")
        for dimension in dimensions:
            _require(dimension in GROUP_DIMENSIONS, f"unknown group dimension: {dimension}")
        records = self.query(
            project_id=project_id, project_ids=project_ids, since=since, until=until
        )
        buckets: dict[tuple[str, ...], _Bucket] = {}
        totals = _Bucket({})
        for record in records:
            values = tuple(_group_value(record, dimension) for dimension in dimensions)
            bucket = buckets.get(values)
            if bucket is None:
                bucket = buckets[values] = _Bucket(
                    {GROUP_DIMENSIONS[d]: v for d, v in zip(dimensions, values, strict=True)}
                )
            bucket.add(record)
            totals.add(record)
        return {
            "schemaVersion": USAGE_LEDGER_SCHEMA_VERSION,
            "groupBy": [GROUP_DIMENSIONS[d] for d in dimensions],
            "buckets": [buckets[key].as_dict() for key in sorted(buckets)],
            "totals": totals.as_dict(),
            "limitations": [
                "sums cover known values only; unknownRecords counts what was not observable",
                "billedUsd is set only for pay-as-you-go pricing; subscription usage is never "
                "presented as billed",
                "paygEquivalentUsd for subscription usage is a reference figure at the "
                "snapshot's reference rates",
                "infrastructure cost is reported separately from AI cost equivalent and is "
                "never combined into one total",
            ],
        }

    # -- persistence -------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": USAGE_LEDGER_SCHEMA_VERSION,
            "usageSchemaVersion": USAGE_SCHEMA_VERSION,
            "records": [record.as_dict() for record in self._records.values()],
        }

    @classmethod
    def from_dict(
        cls,
        document: Any,
        *,
        registry: ProjectRegistry = EMPTY_PROJECT_REGISTRY,
    ) -> UsageLedger:
        entry = _mapping(document, "usage ledger")
        _known_keys(
            entry, frozenset({"schemaVersion", "usageSchemaVersion", "records"}), "usage ledger"
        )
        version = entry.get("schemaVersion")
        _require(
            version == USAGE_LEDGER_SCHEMA_VERSION,
            f"unsupported usage ledger schemaVersion: {version!r}",
        )
        raw = entry.get("records", [])
        _require(isinstance(raw, list), "usage ledger records must be an array")
        assert isinstance(raw, list)
        ledger = cls(registry=registry)
        for item in raw:
            ledger.append(load_usage_record(item))
        return ledger


# -- estimator calibration ------------------------------------------------------


@dataclass
class _Coefficient:
    samples: int = 0
    mean_ratio: float = 0.0

    def observe(self, ratio: float) -> None:
        self.samples += 1
        self.mean_ratio += (ratio - self.mean_ratio) / self.samples


def _calibration_keys(model: str, stage: str, task_class: str, complexity: str) -> tuple[str, ...]:
    """Most specific key first, down to the global key.

    The stage shapes a run's token profile more than the model does, so the
    path narrows to ``stage`` alone before reaching the global key.
    """
    model_key = f"model={model.strip()}"
    stage_key = f"stage={stage.strip()}"
    task_key = f"taskClass={task_class.strip()}"
    complexity_key = f"complexity={complexity.strip()}"
    return (
        ";".join((model_key, stage_key, task_key, complexity_key)),
        ";".join((model_key, stage_key, task_key)),
        ";".join((model_key, stage_key)),
        stage_key,
        "*",
    )


class EstimatorCalibration:
    """Learned correction coefficients: actual / estimate, by model, stage, task and complexity.

    Coefficients are simple running means of the observed ratio, clamped so one
    wild run cannot dominate. Each observation updates every key on the path
    from the most specific to the global one, so a cold key falls back to the
    most specific ancestor that has enough samples — and to the uncorrected
    baseline when none does.
    """

    def __init__(
        self,
        *,
        min_samples: int = 3,
        estimator_version: str = ESTIMATOR_VERSION,
    ) -> None:
        self.min_samples = min_samples
        self.estimator_version = estimator_version
        self._coefficients: dict[str, dict[str, _Coefficient]] = {}
        self._observed: set[str] = set()

    @property
    def min_samples(self) -> int:
        """How many observations a calibration key needs before it corrects anything."""
        return self._min_samples

    @min_samples.setter
    def min_samples(self, value: int) -> None:
        _require(
            isinstance(value, int) and not isinstance(value, bool) and value >= 1,
            "min_samples must be an integer >= 1",
        )
        self._min_samples = value

    @property
    def observed_usage_ids(self) -> frozenset[str]:
        return frozenset(self._observed)

    def observe(self, record: UsageRecord) -> bool:
        """Learn from one record; replaying an observed record changes nothing."""
        if record.usage_id in self._observed:
            return False
        if record.estimate is None or record.actual is None:
            return False
        if record.actual.result in {UsageResult.CANCELLED, UsageResult.UNKNOWN}:
            # A cancelled or unobserved run says nothing about the estimator.
            return False
        learned = False
        keys = _calibration_keys(
            record.actor.model, record.stage.value, record.task_class, record.complexity_class
        )
        for dimension in ESTIMATE_DIMENSIONS:
            estimated = record.estimate.value(dimension)
            actual = record.actual.value(dimension)
            if estimated is None or actual is None or estimated <= 0:
                continue
            ratio = min(max(actual / estimated, _RATIO_FLOOR), _RATIO_CEILING)
            for key in keys:
                self._coefficients.setdefault(key, {}).setdefault(
                    dimension, _Coefficient()
                ).observe(ratio)
            learned = True
        self._observed.add(record.usage_id)
        return learned

    def observe_all(self, records: Iterable[UsageRecord]) -> int:
        return sum(1 for record in records if self.observe(record))

    def coefficient(
        self,
        dimension: str,
        *,
        model: str,
        stage: LifecycleStage | str,
        task_class: str = "",
        complexity: str = "",
    ) -> AppliedCoefficient:
        """The most specific well-sampled coefficient, or the cold-start default."""
        _require(dimension in ESTIMATE_DIMENSIONS, f"unknown estimate dimension: {dimension}")
        stage_value = stage.value if isinstance(stage, LifecycleStage) else stage
        for key in _calibration_keys(model, stage_value, task_class, complexity):
            item = self._coefficients.get(key, {}).get(dimension)
            if item is not None and item.samples >= self.min_samples:
                return AppliedCoefficient(
                    dimension=dimension,
                    key=key,
                    samples=item.samples,
                    coefficient=round(item.mean_ratio, 6),
                )
        return AppliedCoefficient(dimension=dimension, key="cold-start", samples=0, coefficient=1.0)

    def estimate(
        self,
        *,
        baseline_tokens: TokenCounts,
        baseline_runtime_seconds: int | None,
        model: str,
        stage: LifecycleStage | str,
        task_class: str = "",
        complexity: str = "",
        pricing: PricingSnapshot | None = None,
        plan_capacity_units: float | None = None,
    ) -> UsageEstimate:
        """Correct a caller-supplied baseline; unknown baselines stay unknown."""
        applied: list[AppliedCoefficient] = []
        corrected: dict[str, int | None] = {}
        for dimension in ESTIMATE_DIMENSIONS:
            baseline = (
                baseline_runtime_seconds
                if dimension == "runtimeSeconds"
                else baseline_tokens.value(dimension)
            )
            if baseline is None:
                corrected[dimension] = None
                continue
            _require(baseline >= 0, f"baseline {dimension} cannot be negative")
            item = self.coefficient(
                dimension, model=model, stage=stage, task_class=task_class, complexity=complexity
            )
            applied.append(item)
            corrected[dimension] = int(round(baseline * item.coefficient))
        tokens = TokenCounts(
            input=corrected["input"],
            output=corrected["output"],
            cache_read=corrected["cacheRead"],
            cache_write=corrected["cacheWrite"],
        )
        calibrated = [item for item in applied if item.key != "cold-start"]
        if not applied:
            basis = "no baseline supplied; every dimension is unknown"
        elif not calibrated:
            basis = "cold-start: uncorrected baseline, no calibration key has enough samples"
        else:
            basis = "calibrated: " + ", ".join(
                f"{item.dimension} via {item.key} (n={item.samples})" for item in calibrated
            )
        return UsageEstimate(
            tokens=tokens,
            runtime_seconds=corrected["runtimeSeconds"],
            monetary=monetary_equivalent(tokens, pricing, plan_capacity_units=plan_capacity_units),
            estimator_version=self.estimator_version,
            basis=basis,
            calibration=tuple(applied),
        )

    # -- persistence -------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": CALIBRATION_SCHEMA_VERSION,
            "estimatorVersion": self.estimator_version,
            "minSamples": self.min_samples,
            "observedUsageIds": sorted(self._observed),
            "coefficients": {
                key: {
                    dimension: {"samples": item.samples, "meanRatio": round(item.mean_ratio, 6)}
                    for dimension, item in sorted(values.items())
                }
                for key, values in sorted(self._coefficients.items())
            },
        }

    @classmethod
    def from_dict(cls, document: Any) -> EstimatorCalibration:
        label = "estimator calibration"
        entry = _mapping(document, label)
        _known_keys(
            entry,
            frozenset(
                {
                    "schemaVersion",
                    "estimatorVersion",
                    "minSamples",
                    "observedUsageIds",
                    "coefficients",
                }
            ),
            label,
        )
        _require(
            entry.get("schemaVersion") == CALIBRATION_SCHEMA_VERSION,
            f"unsupported {label} schemaVersion: {entry.get('schemaVersion')!r}",
        )
        min_samples = _optional_int(entry, "minSamples", label, minimum=1)
        calibration = cls(
            min_samples=min_samples if min_samples is not None else 3,
            estimator_version=_text(entry, "estimatorVersion", label) or ESTIMATOR_VERSION,
        )
        observed = entry.get("observedUsageIds", [])
        _require(
            isinstance(observed, list) and all(isinstance(item, str) for item in observed),
            f"{label}: observedUsageIds must be an array of strings",
        )
        calibration._observed = set(observed)
        raw = _mapping(entry.get("coefficients", {}), f"{label} coefficients")
        for key, values in raw.items():
            per_dimension = _mapping(values, f"{label} coefficients {key}")
            for dimension, item in per_dimension.items():
                _require(
                    dimension in ESTIMATE_DIMENSIONS,
                    f"{label}: unknown estimate dimension {dimension}",
                )
                stats = _mapping(item, f"{label} coefficients {key} {dimension}")
                _known_keys(stats, frozenset({"samples", "meanRatio"}), f"{label} coefficient")
                samples = _optional_int(stats, "samples", f"{label} coefficient", minimum=1)
                mean_ratio = _optional_float(stats, "meanRatio", f"{label} coefficient")
                _require(
                    samples is not None and mean_ratio is not None,
                    f"{label}: coefficient samples and meanRatio are required",
                )
                assert samples is not None and mean_ratio is not None
                calibration._coefficients.setdefault(key, {})[dimension] = _Coefficient(
                    samples=samples, mean_ratio=mean_ratio
                )
        return calibration


# -- credential-free guarantee ----------------------------------------------------

#: Every dataclass that makes up one serialized usage record.
USAGE_SCHEMA_TYPES = (
    UsageRecord,
    UsageEstimate,
    UsageActual,
    InfrastructureUsage,
    MonetaryEquivalent,
    PricingSnapshot,
    TokenCounts,
    AppliedCoefficient,
)


def secret_bearing_usage_fields() -> tuple[str, ...]:
    """Usage-schema field names that could hold a secret or a prompt: structurally empty."""
    return tuple(
        f"{schema.__name__}.{item.name}"
        for schema in USAGE_SCHEMA_TYPES
        for item in fields(schema)
        if _SECRET_KEY.search(item.name) is not None
    )
