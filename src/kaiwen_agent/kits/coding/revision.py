from __future__ import annotations

from kaiwen_agent.kits.coding.planner import ObjectivePlanner
from kaiwen_agent.workflow import WorkflowPlan, WorkflowRevisionPlanner, WorkflowRevisionRequest


class BoundedCodingRevisionPlanner(WorkflowRevisionPlanner):
    """Maps observed workflow failure evidence into one newly validated plan proposal."""

    def __init__(self, planner: ObjectivePlanner) -> None:
        self.planner = planner

    async def revise(self, request: WorkflowRevisionRequest) -> WorkflowPlan | None:
        context = {
            "cycle": request.cycle,
            "reason": request.reason.value,
            "task_failures": [item.model_dump(mode="json") for item in request.task_failures],
            "quality_result": (
                request.quality_result.model_dump(mode="json")
                if request.quality_result is not None
                else None
            ),
            "prior_plan": request.prior_plan.model_dump(mode="json"),
        }
        return await self.planner.plan(request.prior_plan.objective, context=context)
