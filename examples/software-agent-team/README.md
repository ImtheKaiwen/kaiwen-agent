# Software agent team

This provider-free example delegates one bounded read-only review to the
least-privileged registered team member:

```powershell
.\.venv\Scripts\python.exe examples\software-agent-team\main.py
```

Teams are optional composition over `SubagentDelegator`, `JobManager`, and the Task
Runtime. Team definitions do not grant permissions and workers receive no private
history unless the host explicitly supplies bounded references.
