"""Adaptive slot filling for dependency-aware Forge work graphs.

The scheduler is deliberately policy-neutral: it only starts units that the
validated GraphExecutor already considers ready and lets GraphExecutor enforce
provider concurrency, lease, token, and global parallelism bounds.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .work_graph import GraphExecutor, WorkGraphError

__all__ = ["DispatchDecision", "fill_safe_slots"]


@dataclass(frozen=True)
class DispatchDecision:
    unit_id: str
    status: str
    reason: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"unitId": self.unit_id, "status": self.status, "reason": self.reason}


def fill_safe_slots(
    executor: GraphExecutor,
    *,
    now: int,
    agent_for: Callable[[str], str] | Mapping[str, str],
) -> tuple[DispatchDecision, ...]:
    """Start as many currently-ready units as safe capacity permits.

    A provider-saturated or token-backpressured unit never prevents an unrelated
    ready unit from using a free slot. GraphExecutor remains the source of truth
    for all safety bounds; this helper only turns temporary refusal into an
    explicit skip and continues scanning the deterministic ready queue.
    """

    if now < 0:
        raise ValueError("now must be non-negative")

    def resolve_agent(unit_id: str) -> str:
        value = agent_for(unit_id) if callable(agent_for) else agent_for.get(unit_id, "")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"missing agent id for ready unit {unit_id}")
        return value

    decisions: list[DispatchDecision] = []
    for unit_id in executor.ready():
        agent_id = resolve_agent(unit_id)
        try:
            executor.start(unit_id, agent_id=agent_id, now=now)
        except WorkGraphError as exc:
            message = str(exc)
            temporary = (
                "maximum parallelism" in message
                or "concurrency limit" in message
                or "total token budget" in message
                or "exclusively leased" in message
            )
            if not temporary:
                raise
            decisions.append(DispatchDecision(unit_id, "waiting", message))
            if "maximum parallelism" in message:
                break
            continue
        decisions.append(DispatchDecision(unit_id, "started"))
    return tuple(decisions)
