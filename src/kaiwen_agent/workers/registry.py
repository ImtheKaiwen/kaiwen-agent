from kaiwen_agent.workers.base import Worker


class WorkerNotFoundError(LookupError):
    pass


class WorkerRegistry:
    def __init__(self) -> None:
        self._workers: dict[str, Worker] = {}

    def register(self, agent_id: str, worker: Worker) -> None:
        if agent_id in self._workers:
            raise ValueError(f"Worker already registered: {agent_id}")
        self._workers[agent_id] = worker

    def get(self, agent_id: str) -> Worker:
        try:
            return self._workers[agent_id]
        except KeyError as error:
            raise WorkerNotFoundError(f"Unknown worker: {agent_id}") from error
