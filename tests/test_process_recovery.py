from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_child(script: str, database: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, str(database)],
        check=True,
        capture_output=True,
        text=True,
    )


def test_task_and_checkpoint_recover_in_a_new_process(tmp_path: Path) -> None:
    database = tmp_path / "process-recovery.sqlite3"
    writer = """
import asyncio
import sys
from pathlib import Path

from kaiwen_agent.control import RunControl
from kaiwen_agent.persistence import SQLitePersistence
from kaiwen_agent.tasks import AgentTask, TaskStatus


async def main() -> None:
    store = SQLitePersistence(Path(sys.argv[1]))
    await store.save_task(
        AgentTask(
            id="task-process",
            name="Recover across process boundary",
            agent_id="worker",
            status=TaskStatus.RUNNING,
            attempts=1,
        )
    )
    await RunControl(checkpoints=store).checkpoint(
        "task-process", state={"completed_steps": ["plan", "write"]}
    )


asyncio.run(main())
"""
    reader = """
import asyncio
import json
import sys
from pathlib import Path

from kaiwen_agent.persistence import SQLitePersistence


async def main() -> None:
    store = SQLitePersistence(Path(sys.argv[1]))
    task = await store.get_task("task-process")
    checkpoint = await store.latest_checkpoint("task-process")
    print(json.dumps({
        "task_status": task.status.value if task else None,
        "attempts": task.attempts if task else None,
        "checkpoint": checkpoint.state if checkpoint else None,
    }, sort_keys=True))


asyncio.run(main())
"""

    _run_child(writer, database)
    recovered = json.loads(_run_child(reader, database).stdout)

    assert recovered == {
        "attempts": 1,
        "checkpoint": {"completed_steps": ["plan", "write"]},
        "task_status": "running",
    }
