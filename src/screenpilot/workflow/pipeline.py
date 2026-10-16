"""Chained post-capture actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable

from screenpilot.core.errors import WorkflowError
from screenpilot.workflow.actions import Action, ActionSkipped, WorkflowContext, builtin_actions

log = logging.getLogger(__name__)

EVENT_WORKFLOW_FINISHED = "workflow.finished"


class StepStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class StepResult:
    action_id: str
    status: StepStatus
    message: str = ""


class ActionRegistry:
    def __init__(self, actions: Iterable[Action] | None = None) -> None:
        self._actions: dict[str, Action] = {}
        for action in actions if actions is not None else builtin_actions():
            self.register(action)

    def register(self, action: Action) -> None:
        self._actions[action.id] = action

    def get(self, action_id: str) -> Action | None:
        return self._actions.get(action_id)

    @property
    def ids(self) -> list[str]:
        return list(self._actions)


class Workflow:
    def __init__(self, steps: list[dict[str, Any]], *, stop_on_error: bool = False) -> None:
        for step in steps:
            if not isinstance(step, dict) or not isinstance(step.get("id"), str):
                raise WorkflowError(f"Invalid workflow step: {step!r}")
        self.steps = steps
        self.stop_on_error = stop_on_error

    def run(self, ctx: WorkflowContext, registry: ActionRegistry) -> list[StepResult]:
        results: list[StepResult] = []
        for step in self.steps:
            action_id = step["id"]
            params = {k: v for k, v in step.items() if k != "id"}
            action = registry.get(action_id)
            if action is None:
                results.append(StepResult(action_id, StepStatus.FAILED, "unknown action"))
                log.error("Unknown workflow action %s", action_id)
                if self.stop_on_error:
                    break
                continue
            try:
                action.run(ctx, params)
                results.append(StepResult(action_id, StepStatus.OK))
            except ActionSkipped as skip:
                results.append(StepResult(action_id, StepStatus.SKIPPED, str(skip)))
            except Exception as exc:  # noqa: BLE001 - one failing step must not lose the capture
                log.exception("Workflow action %s failed", action_id)
                results.append(StepResult(action_id, StepStatus.FAILED, str(exc)))
                if self.stop_on_error:
                    break
        ctx.bus.publish(EVENT_WORKFLOW_FINISHED, results=results, path=ctx.saved_path, result=ctx.result)
        return results
