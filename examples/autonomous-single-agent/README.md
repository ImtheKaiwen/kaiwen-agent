# Autonomous single-agent workflow

This provider-free example compiles a finite workflow into the existing durable Task
Runtime. It uses a temporary SQLite database and requires no API key:

```powershell
.\.venv\Scripts\python.exe examples\autonomous-single-agent\main.py
```

The plan is trusted example input. Production applications must validate untrusted
plans, compose product policy, and route side effects through durable intents,
approval, and idempotency.
