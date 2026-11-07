# Speech pipeline

This provider-free example demonstrates the ephemeral STT → text handler → TTS
composition with explicit audio-processing permission and one trusted identity.

```powershell
.\.venv\Scripts\python.exe examples\speech-pipeline\main.py
```

The fake adapters do not persist audio or require provider credentials.
