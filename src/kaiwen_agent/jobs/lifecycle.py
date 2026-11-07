from __future__ import annotations

from kaiwen_agent.jobs.models import AgentJob, JobStatus
from kaiwen_agent.lifecycle import WakeTrigger, WakeTriggerType, WakeupManager
from kaiwen_agent.types import utc_now


class LifecycleJobCompletionNotifier:
    """Schedules a durable wakeup for the agent that requested a terminal job."""

    def __init__(self, *, wakeups: WakeupManager, priority: int = 70) -> None:
        if not -100 <= priority <= 100:
            raise ValueError("priority must be between -100 and 100")
        self.wakeups = wakeups
        self.priority = priority

    async def notify(self, job: AgentJob) -> None:
        if job.status not in {JobStatus.COMPLETED, JobStatus.FAILED}:
            return
        trigger_type = (
            WakeTriggerType.JOB_COMPLETED
            if job.status == JobStatus.COMPLETED
            else WakeTriggerType.JOB_FAILED
        )
        await self.wakeups.schedule(
            agent_id=job.requested_by_agent_id,
            trigger=WakeTrigger(
                type=trigger_type,
                reason=f"Background job {job.display_id} {job.status.value}",
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                conversation_id=job.conversation_id,
                metadata={
                    "trace_id": job.trace_id,
                    "job_id": job.id,
                    "result_ref": job.result_ref,
                    "error_code": job.error_code,
                },
            ),
            scheduled_at=utc_now(),
            replace_key=f"job-result:{job.id}",
            priority=self.priority,
        )
