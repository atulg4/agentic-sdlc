"""Dependency-aware parallel work graph with isolated agent workspaces.

An approved parent work request decomposes into a validated DAG of bounded
work units. Independent units run in parallel in isolated workspaces;
conflicting units serialize or fail closed. Fan-in composes only attested,
immutable artifacts against a pinned base, and the composed tree must pass
final verification before the graph completes.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .policy import ProjectPolicy

__all__ = [
    "GraphExecutor",
    "LeaseManager",
    "WorkGraph",
    "WorkGraphError",
    "WorkUnitSpec",
    "validate_graph",
]

_UNIT_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_SHA256_REF = re.compile(r"^sha256:[0-9a-f]{64}$")
DEFAULT_COMPOSITION = "independent-merge"


class WorkGraphError(ValueError):
    """Raised when a work graph, lease, or composition rule is violated."""


@dataclass(frozen=True)
class WorkUnitSpec:
    """One bounded node in the parallel work DAG."""

    unit_id: str
    mission_id: str
    mission_version: str
    output_artifact: str
    depends_on: tuple[str, ...] = ()
    owned_paths: tuple[str, ...] = ()
    read_paths: tuple[str, ...] = ("**",)
    write_paths: tuple[str, ...] = ()
    shared_resources: tuple[str, ...] = ()
    verification_gates: tuple[str, ...] = ("deterministic-ci",)
    concurrency_group: str = ""
    composition: str = DEFAULT_COMPOSITION
    provider: str = ""
    timeout_seconds: int = 3600
    max_retries: int = 1
    max_tokens: int = 500_000

    @property
    def read_only(self) -> bool:
        return not self.write_paths

    def as_dict(self) -> dict[str, Any]:
        return {
            "unitId": self.unit_id,
            "missionId": self.mission_id,
            "missionVersion": self.mission_version,
            "outputArtifact": self.output_artifact,
            "dependsOn": list(self.depends_on),
            "ownedPaths": list(self.owned_paths),
            "readPaths": list(self.read_paths),
            "writePaths": list(self.write_paths),
            "sharedResources": list(self.shared_resources),
            "verificationGates": list(self.verification_gates),
            "concurrencyGroup": self.concurrency_group,
            "composition": self.composition,
            "provider": self.provider,
            "timeoutSeconds": self.timeout_seconds,
            "maxRetries": self.max_retries,
            "maxTokens": self.max_tokens,
            "readOnly": self.read_only,
        }


def _patterns_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Conservative overlap check between two write scopes."""
    for a in left:
        for b in right:
            if a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a):
                return True
    return False


def _toposort(units: Mapping[str, WorkUnitSpec]) -> tuple[str, ...]:
    remaining = {unit_id: set(spec.depends_on) for unit_id, spec in units.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(unit_id for unit_id, deps in remaining.items() if not deps)
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise WorkGraphError(f"work graph contains a dependency cycle among: {cycle}")
        for unit_id in ready:
            order.append(unit_id)
            del remaining[unit_id]
        for deps in remaining.values():
            deps.difference_update(ready)
    return tuple(order)


def _ancestors(units: Mapping[str, WorkUnitSpec]) -> dict[str, frozenset[str]]:
    order = _toposort(units)
    ancestors: dict[str, frozenset[str]] = {}
    for unit_id in order:
        spec = units[unit_id]
        merged: set[str] = set()
        for dependency in spec.depends_on:
            merged.add(dependency)
            merged.update(ancestors[dependency])
        ancestors[unit_id] = frozenset(merged)
    return ancestors


@dataclass(frozen=True)
class WorkGraph:
    """A validated DAG of work units in deterministic topological order."""

    units: Mapping[str, WorkUnitSpec]
    order: tuple[str, ...]
    ancestors: Mapping[str, frozenset[str]]

    def get(self, unit_id: str) -> WorkUnitSpec:
        spec = self.units.get(unit_id)
        if spec is None:
            raise WorkGraphError(f"unknown work unit: {unit_id}")
        return spec

    def ordered(self, first: str, second: str) -> bool:
        """True when one unit transitively depends on the other."""
        return first in self.ancestors[second] or second in self.ancestors[first]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": 1,
            "order": list(self.order),
            "units": {unit_id: spec.as_dict() for unit_id, spec in sorted(self.units.items())},
        }


def validate_graph(
    specs: Sequence[WorkUnitSpec],
    policy: ProjectPolicy,
) -> WorkGraph:
    """Validate the DAG, ownership, scopes, and conflicts; fail closed."""
    if not specs:
        raise WorkGraphError("work graph requires at least one unit")
    units: dict[str, WorkUnitSpec] = {}
    for spec in specs:
        if not _UNIT_ID.fullmatch(spec.unit_id):
            raise WorkGraphError(f"unit id must match {_UNIT_ID.pattern}: {spec.unit_id}")
        if spec.unit_id in units:
            raise WorkGraphError(f"duplicate unit id: {spec.unit_id}")
        if not spec.mission_id or not spec.mission_version:
            raise WorkGraphError(f"unit {spec.unit_id}: mission id and version are required")
        if not spec.output_artifact.strip():
            raise WorkGraphError(f"unit {spec.unit_id}: output artifact is required")
        if spec.max_tokens < 1 or spec.timeout_seconds < 1:
            raise WorkGraphError(f"unit {spec.unit_id}: budget and timeout must be positive")
        units[spec.unit_id] = spec

    for spec in units.values():
        missing = sorted(set(spec.depends_on) - set(units))
        if missing:
            raise WorkGraphError(
                f"unit {spec.unit_id}: missing dependencies: " + ", ".join(missing)
            )
        if spec.unit_id in spec.depends_on:
            raise WorkGraphError(f"unit {spec.unit_id}: a unit cannot depend on itself")

    # Global path policy: no write scope may touch forbidden paths, ever.
    for spec in units.values():
        for pattern in spec.write_paths:
            normalized = pattern.removeprefix("./")
            if any(fnmatch.fnmatch(normalized, item) for item in policy.forbidden_paths):
                raise WorkGraphError(
                    f"unit {spec.unit_id}: write scope includes a forbidden path: {pattern}"
                )

    # Unambiguous ownership: an owned path belongs to exactly one unit.
    owners: dict[str, str] = {}
    for spec in units.values():
        for path in spec.owned_paths:
            if path in owners:
                raise WorkGraphError(
                    f"ambiguous ownership of {path}: {owners[path]} and {spec.unit_id}"
                )
            owners[path] = spec.unit_id

    order = _toposort(units)
    ancestors = _ancestors(units)
    graph = WorkGraph(units=units, order=order, ancestors=ancestors)

    # Conflicting writers must be ordered or declare a shared, explicit
    # composition strategy; otherwise the graph fails closed.
    unit_ids = list(order)
    for index, left_id in enumerate(unit_ids):
        left = units[left_id]
        for right_id in unit_ids[index + 1 :]:
            right = units[right_id]
            if graph.ordered(left_id, right_id):
                continue
            if _patterns_overlap(left.write_paths, right.write_paths):
                shared_strategy = (
                    left.composition == right.composition
                    and left.composition != DEFAULT_COMPOSITION
                )
                if not shared_strategy:
                    raise WorkGraphError(
                        f"units {left_id} and {right_id} have overlapping write "
                        "scopes and no explicit shared composition strategy; "
                        "serialize them with dependencies or declare one"
                    )
            shared = set(left.shared_resources) & set(right.shared_resources)
            if shared and not (left.read_only and right.read_only):
                raise WorkGraphError(
                    f"units {left_id} and {right_id} share semantic resource(s) "
                    f"{', '.join(sorted(shared))} without ordering; concurrent "
                    "modification is unsafe"
                )
    return graph


class LeaseManager:
    """Exclusive, expiring, auditable leases over paths and resources."""

    def __init__(self) -> None:
        self._leases: dict[str, tuple[str, int]] = {}
        self._log: list[str] = []

    def acquire(self, resource: str, unit_id: str, *, now: int, ttl_seconds: int = 3600) -> None:
        if ttl_seconds < 1:
            raise WorkGraphError("lease ttl must be positive")
        holder = self._leases.get(resource)
        if holder is not None:
            held_by, expires = holder
            if held_by != unit_id and now < expires:
                raise WorkGraphError(
                    f"resource {resource} is exclusively leased to {held_by} until {expires}"
                )
            if held_by != unit_id:
                self._log.append(
                    f"lease on {resource} held by {held_by} expired at {expires}; "
                    f"reclaimed by {unit_id} at {now}"
                )
        self._leases[resource] = (unit_id, now + ttl_seconds)
        self._log.append(f"{unit_id} acquired {resource} at {now} for {ttl_seconds}s")

    def release(self, resource: str, unit_id: str, *, now: int) -> None:
        holder = self._leases.get(resource)
        if holder is None or holder[0] != unit_id:
            raise WorkGraphError(f"{unit_id} does not hold a lease on {resource}")
        del self._leases[resource]
        self._log.append(f"{unit_id} released {resource} at {now}")

    def holder(self, resource: str, *, now: int) -> str:
        entry = self._leases.get(resource)
        if entry is None or now >= entry[1]:
            return ""
        return entry[0]

    def audit(self) -> tuple[str, ...]:
        return tuple(self._log)


class GraphExecutor:
    """Bounded scheduler and fan-in composer for a validated work graph.

    Every unit gets an isolated workspace; parallel agents never share a
    writable checkout, and no unit can merge another unit's branch — fan-in
    happens only through attested artifacts composed against a pinned base.
    """

    def __init__(
        self,
        graph: WorkGraph,
        *,
        base_sha: str,
        max_parallel: int = 4,
        total_token_budget: int = 5_000_000,
        provider_limits: Mapping[str, int] | None = None,
    ) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
            raise WorkGraphError("base_sha must be a 40-character commit SHA")
        if max_parallel < 1:
            raise WorkGraphError("max_parallel must be positive")
        self.graph = graph
        self.base_sha = base_sha
        self.max_parallel = max_parallel
        self.total_token_budget = total_token_budget
        self.provider_limits = dict(provider_limits or {})
        self.leases = LeaseManager()
        self._status: dict[str, str] = {unit_id: "pending" for unit_id in graph.order}
        self._artifacts: dict[str, str] = {}
        self._verified: dict[str, bool] = {}
        self._agents: dict[str, str] = {}
        self._tokens_spent = 0
        self._invalidations: list[str] = []
        self._final_verification: bool | None = None

    # -- scheduling --------------------------------------------------------

    def workspace(self, unit_id: str) -> str:
        """Isolated checkout/artifact directory; never shared between units."""
        self.graph.get(unit_id)
        return f"workspaces/{unit_id}"

    def status(self, unit_id: str) -> str:
        status = self._status.get(unit_id)
        if status is None:
            raise WorkGraphError(f"unknown work unit: {unit_id}")
        return status

    def ready(self) -> tuple[str, ...]:
        """Units whose dependencies are attested, in deterministic order."""
        ready = []
        for unit_id in self.graph.order:
            if self._status[unit_id] != "pending":
                continue
            spec = self.graph.get(unit_id)
            if all(self._is_attested(dep) for dep in spec.depends_on):
                ready.append(unit_id)
        return tuple(ready)

    def _is_attested(self, unit_id: str) -> bool:
        return (
            self._status.get(unit_id) == "completed"
            and self._verified.get(unit_id, False)
            and unit_id in self._artifacts
        )

    def _running(self) -> list[str]:
        return [unit_id for unit_id, status in self._status.items() if status == "running"]

    def start(self, unit_id: str, *, agent_id: str, now: int) -> str:
        """Start a unit inside bounds; returns its isolated workspace."""
        spec = self.graph.get(unit_id)
        if self._status[unit_id] != "pending":
            raise WorkGraphError(f"unit {unit_id} is not pending ({self._status[unit_id]})")
        unmet = sorted(dep for dep in spec.depends_on if not self._is_attested(dep))
        if unmet:
            raise WorkGraphError(f"unit {unit_id} has unattested dependencies: " + ", ".join(unmet))
        running = self._running()
        if len(running) >= self.max_parallel:
            raise WorkGraphError(
                f"maximum parallelism {self.max_parallel} reached; {unit_id} must wait"
            )
        if spec.provider:
            limit = self.provider_limits.get(spec.provider)
            active = sum(1 for other in running if self.graph.get(other).provider == spec.provider)
            if limit is not None and active >= limit:
                raise WorkGraphError(f"provider {spec.provider} concurrency limit {limit} reached")
        if self._tokens_spent + spec.max_tokens > self.total_token_budget:
            raise WorkGraphError(
                f"starting {unit_id} would exceed the total token budget "
                f"({self._tokens_spent} spent, {spec.max_tokens} requested, "
                f"{self.total_token_budget} allowed)"
            )
        for resource in (*spec.owned_paths, *spec.shared_resources):
            self.leases.acquire(resource, unit_id, now=now, ttl_seconds=spec.timeout_seconds)
        self._status[unit_id] = "running"
        self._agents[unit_id] = agent_id
        return self.workspace(unit_id)

    def complete(
        self,
        unit_id: str,
        *,
        artifact_digest: str,
        verification_passed: bool,
        tokens_used: int,
        now: int,
    ) -> None:
        """Record an immutable, attested output artifact for a unit."""
        spec = self.graph.get(unit_id)
        if self._status[unit_id] != "running":
            raise WorkGraphError(f"unit {unit_id} is not running")
        if not _SHA256_REF.fullmatch(artifact_digest):
            raise WorkGraphError("artifact digest must use sha256:<hex> form")
        if tokens_used < 0:
            raise WorkGraphError("tokens_used cannot be negative")
        self._tokens_spent += tokens_used
        self._status[unit_id] = "completed" if verification_passed else "failed"
        self._verified[unit_id] = verification_passed
        self._artifacts[unit_id] = artifact_digest
        for resource in (*spec.owned_paths, *spec.shared_resources):
            self.leases.release(resource, unit_id, now=now)
        if not verification_passed:
            self._block_downstream(unit_id, reason="upstream verification failed")

    def fail(self, unit_id: str, *, now: int, reason: str = "") -> None:
        spec = self.graph.get(unit_id)
        if self._status[unit_id] != "running":
            raise WorkGraphError(f"unit {unit_id} is not running")
        self._status[unit_id] = "failed"
        for resource in (*spec.owned_paths, *spec.shared_resources):
            self.leases.release(resource, unit_id, now=now)
        self._block_downstream(unit_id, reason=reason or "upstream unit failed")

    def _block_downstream(self, unit_id: str, *, reason: str) -> None:
        for other_id in self.graph.order:
            if unit_id in self.graph.ancestors[other_id] and self._status[other_id] in (
                "pending",
                "running",
            ):
                self._status[other_id] = "blocked"
                self._invalidations.append(f"{other_id} blocked: {reason} ({unit_id})")

    def invalidate_upstream_change(self, unit_id: str, *, new_digest: str) -> tuple[str, ...]:
        """An upstream artifact changed: dependents' outputs are invalid."""
        self.graph.get(unit_id)
        if not _SHA256_REF.fullmatch(new_digest):
            raise WorkGraphError("artifact digest must use sha256:<hex> form")
        previous = self._artifacts.get(unit_id)
        if previous == new_digest:
            return ()
        self._artifacts[unit_id] = new_digest
        invalidated = []
        for other_id in self.graph.order:
            if unit_id in self.graph.ancestors[other_id] and self._status[other_id] in (
                "completed",
                "running",
                "blocked",
            ):
                self._status[other_id] = "pending"
                self._verified.pop(other_id, None)
                self._artifacts.pop(other_id, None)
                invalidated.append(other_id)
                self._invalidations.append(
                    f"{other_id} invalidated: upstream {unit_id} artifact changed "
                    f"({previous} -> {new_digest})"
                )
        self._final_verification = None
        return tuple(invalidated)

    # -- fan-in ------------------------------------------------------------

    def record_final_verification(self, *, passed: bool) -> None:
        """Verification of the composed tree, not individual branches."""
        incomplete = sorted(
            unit_id
            for unit_id in self.graph.order
            if not self.graph.get(unit_id).read_only and not self._is_attested(unit_id)
        )
        if incomplete:
            raise WorkGraphError(
                "composed-tree verification requires all writing units attested; "
                "missing: " + ", ".join(incomplete)
            )
        self._final_verification = passed

    def compose(self) -> dict[str, Any]:
        """Fan-in report over attested artifacts against the pinned base.

        Composition never bypasses verification: every writing unit must be
        attested, and the recomposed tree must have passed final verification.
        Evidence from unrelated completed units is preserved in the report
        even when composition is refused.
        """
        failed = sorted(
            unit_id for unit_id in self.graph.order if self._status[unit_id] == "failed"
        )
        blocked = sorted(
            unit_id for unit_id in self.graph.order if self._status[unit_id] == "blocked"
        )
        preserved = {
            unit_id: self._artifacts[unit_id]
            for unit_id in self.graph.order
            if self._is_attested(unit_id)
        }
        if failed or blocked:
            raise WorkGraphError(
                "unsafe fan-in refused: failed units "
                + (", ".join(failed) or "none")
                + "; blocked units "
                + (", ".join(blocked) or "none")
                + "; attested evidence preserved for: "
                + (", ".join(sorted(preserved)) or "none")
            )
        writing_units = [
            unit_id for unit_id in self.graph.order if not self.graph.get(unit_id).read_only
        ]
        unattested = sorted(unit_id for unit_id in writing_units if not self._is_attested(unit_id))
        if unattested:
            raise WorkGraphError(
                "only attested artifacts may enter composition; missing: " + ", ".join(unattested)
            )
        if self._final_verification is not True:
            raise WorkGraphError(
                "composition requires passed verification of the composed tree "
                "against the pinned base"
            )
        return {
            "schemaVersion": 1,
            "baseSha": self.base_sha,
            "composedTreeVerified": True,
            "tokensSpent": self._tokens_spent,
            "invalidations": list(self._invalidations),
            "leaseAudit": list(self.leases.audit()),
            "units": [
                {
                    "unitId": unit_id,
                    "missionId": self.graph.get(unit_id).mission_id,
                    "missionVersion": self.graph.get(unit_id).mission_version,
                    "agentId": self._agents.get(unit_id, ""),
                    "workspace": self.workspace(unit_id),
                    "status": self._status[unit_id],
                    "artifactDigest": self._artifacts.get(unit_id, ""),
                    "verificationPassed": self._verified.get(unit_id, False),
                    "composition": self.graph.get(unit_id).composition,
                    "conflictDecision": (
                        "independent"
                        if self.graph.get(unit_id).composition == DEFAULT_COMPOSITION
                        else self.graph.get(unit_id).composition
                    ),
                }
                for unit_id in self.graph.order
            ],
        }
