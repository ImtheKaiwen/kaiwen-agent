# Realtime / Live API Entegrasyonu — Kaiwen Agent Platform

## 1. Amaç

Bu dokümanın amacı, mevcut **Kaiwen Agent Platform** mimarisine Realtime / Live API desteğinin nasıl eklenmesi gerektiğini detaylı biçimde tanımlamaktır.

Bu entegrasyonun amacı yalnızca mikrofondan ses alıp modele göndermek değildir. Hedeflenen yapı; admin panelinde ve mobil yönetim uygulamasında sesli kullanım, aynı agent'ın text ile de kullanılabilmesi, çoklu tenant ve çoklu eşzamanlı session desteği, telefon görüşmeleri, event tabanlı aramalar, tool ve task çalıştırma, güvenlik, performans ve yatay ölçekleme ihtiyaçlarını birlikte karşılamalıdır.

Temel prensip:

> **Realtime / Live API konuşma ve etkileşim kanalıdır; sistemin ana execution engine'i değildir.**

Ana execution engine mevcut platformdaki şu katmanlar olmaya devam etmelidir:

```text
Agent Runtime
Task Runtime
Tool Runtime
Scheduler
Workers
Permissions
Approval
Memory
Event System
UI Runtime
```

---

## 2. Mevcut Mimarinin Korunması

Realtime entegrasyonu mevcut mimariyi değiştirmek için değil, onu yeni bir interaction channel ile genişletmek için yapılmalıdır.

Korunması gereken parçalar:

```text
Agent
AgentRun
AgentContext

ToolRegistry
ToolExecutor
Permission System
Approval System
Idempotency
Retry / Timeout

TaskGraph
TaskScheduler
WorkerRegistry
ResourceManager
RunControl
Mailbox
Checkpoints

EventSink
EventStore
Audit

UI Agent
Semantic Client Actions
Protocol Package
```

Realtime entegrasyonu bu sistemlerin yerine geçmemelidir. Özellikle tool execution, task scheduling, authorization, approval, idempotency, worker execution ve UI action registry yeniden yazılmamalıdır.

---

## 3. Realtime'ı Bir Channel Runtime Olarak Modellemek

Uzun vadede yalnızca web sesli konuşma düşünülmemelidir. Sistem farklı interaction channel'larını destekleyebilmelidir.

Önerilen abstraction:

```text
Channel Runtime
```

Örnek kanallar:

```text
WebTextChannel
WebRealtimeChannel
MobileRealtimeChannel
PhoneChannel
CLIChannel
FutureSlackChannel
FutureWhatsAppChannel
```

Genel mimari:

```text
                Conversation / Interaction Layer

        ┌──────────┬──────────────┬──────────┬──────────┐
        │ Web Text │ Web Realtime │ Mobile   │ Phone    │
        └────┬─────┴──────┬───────┴────┬─────┴────┬─────┘
             │            │            │          │
             ▼            ▼            ▼          ▼
       TextChannel   RealtimeChannel          PhoneChannel
             │            │                       │
             └────────────┴───────────┬───────────┘
                                      ▼
                            Conversation Runtime
                                      │
                                      ▼
                                 Agent Core
```

Bu yapı sayesinde Live API tek bir projeye veya tek bir UI'a bağlı kalmaz.

---

## 4. Realtime'ın Temel Sorumlulukları

Realtime subsystem yalnızca aşağıdaki sorumlulukları taşımalıdır:

```text
Session lifecycle
Audio input
Audio output
Text input
Turn detection
VAD
Interruption / barge-in
Transcript handling
Provider event handling
Realtime connection state
Conversation bridge
Realtime tool bridge
```

Realtime subsystem'in doğrudan sorumluluğu olmaması gereken şeyler:

```text
Database update
Email sending
Project deletion
Deployment
Printer execution
Complex business logic
Long-running workflow
Task ordering
Authorization
Persistent memory ownership
```

Bunlar mevcut runtime katmanlarına ait olmalıdır.

---

## 5. Önerilen Backend Klasör Yapısı

```text
backend/src/kaiwen_agent/
│
├── realtime/
│   ├── __init__.py
│   ├── base.py
│   ├── manager.py
│   ├── session.py
│   ├── bridge.py
│   ├── config.py
│   ├── events.py
│   ├── objective.py
│   ├── policy.py
│   ├── permissions.py
│   └── providers/
│       ├── __init__.py
│       └── openai.py
│
├── channels/
│   ├── __init__.py
│   ├── base.py
│   ├── web_text.py
│   ├── web_realtime.py
│   ├── mobile.py
│   └── phone.py
│
├── telephony/
│   ├── __init__.py
│   ├── base.py
│   ├── service.py
│   └── providers/
│       ├── twilio.py
│       └── sip.py
│
├── triggers/
│   ├── engine.py
│   ├── rules.py
│   └── policy.py
│
└── notifications/
    ├── router.py
    ├── models.py
    └── policy.py
```

---

## 6. Realtime Provider Arayüzü

Mevcut `ModelProvider` text request/response kullanımına uygundur. Realtime için ayrı interface kullanılmalıdır.

```python
class RealtimeProvider(Protocol):

    async def create_session(
        self,
        config: RealtimeSessionConfig,
    ) -> "RealtimeConnection":
        ...
```

Connection:

```python
class RealtimeConnection(Protocol):

    async def send_text(self, text: str) -> None:
        ...

    async def send_audio(self, audio: bytes) -> None:
        ...

    async def interrupt(self) -> None:
        ...

    async def close(self) -> None:
        ...

    def events(self) -> AsyncIterator["RealtimeProviderEvent"]:
        ...
```

Avantajı:

```text
Realtime Runtime
      │
      ▼
RealtimeProvider
      │
 ┌────┼─────────────┐
 ▼    ▼             ▼
OpenAI Future     Local
       Provider   Provider
```

Core OpenAI SDK'ya bağımlı olmaz.

---

## 7. OpenAI Realtime Adapter

OpenAI entegrasyonu provider adapter olarak kalmalıdır.

```text
realtime/
├── base.py
└── providers/
    └── openai.py
```

Adapter'ın görevi:

```text
Provider-specific connection açmak
Provider event'lerini normalize etmek
Audio stream göndermek
Audio response almak
Provider tool call event'lerini bridge etmek
Usage bilgisi üretmek
Provider session id tutmak
```

Olmalıdır.

---

## 8. Realtime Session ve Conversation Ayrımı

Bu ayrım kritik önemdedir:

```text
RealtimeSession != Conversation
```

Bir conversation uzun süre yaşayabilir. Realtime session ise bağlantı bazlı ve geçici olabilir.

```text
conversation_123

├── realtime_session_1
│   └── admin web
│
├── realtime_session_2
│   └── reconnect
│
└── realtime_session_3
    └── mobile
```

Realtime bağlantısı kopsa bile conversation kaybolmamalıdır.

---

## 9. RealtimeSessionRecord

```python
class RealtimeSessionRecord(BaseModel):
    id: str
    conversation_id: str
    tenant_id: str
    user_id: str
    client_id: str | None

    channel: Literal[
        "web_voice",
        "mobile_voice",
        "phone",
    ]

    provider: str
    provider_session_id: str | None = None

    status: Literal[
        "created",
        "connecting",
        "active",
        "interrupted",
        "reconnecting",
        "closing",
        "closed",
        "failed",
    ]

    created_at: datetime
    connected_at: datetime | None
    disconnected_at: datetime | None
    metadata: dict[str, Any]
```

---

## 10. Realtime Session Lifecycle

```text
CREATED
   ↓
CONNECTING
   ↓
ACTIVE
   ↓
CLOSING
   ↓
CLOSED
```

Ek durumlar:

```text
INTERRUPTED
RECONNECTING
FAILED
```

Örnek:

```text
ACTIVE
  ↓ network lost
RECONNECTING
  ↓ success
ACTIVE
```

---

## 11. Realtime Session Manager

```python
class RealtimeSessionManager:

    async def create_session(...): ...
    async def connect(...): ...
    async def disconnect(...): ...
    async def reconnect(...): ...
    async def get_session(...): ...
    async def list_active(...): ...
```

Manager şu alanları yönetebilir:

```text
provider connection
session metadata
tenant limits
user limits
connection ownership
```

---

## 12. AgentContext'in Genişletilmesi

Ürünleşme düşünülüyorsa `tenant_id` ve `user_id` ilk günden context'e eklenmelidir.

```python
AgentContext(
    tenant_id="tenant_123",
    user_id="user_42",
    session_id="session_abc",
    trace_id="trace_xyz",
    permissions=...,
    services=...,
    metadata=...,
)
```

`tenant_id` tool input'tan alınmamalıdır. Trusted context'ten gelmelidir.

---

## 13. Multi-Tenant Hazırlık

İlk kullanım yalnızca admin panelinde olsa bile ürünleşme düşünülüyorsa multi-tenant izolasyon başlangıçtan tasarlanmalıdır.

Her kritik entity tenant ile ilişkilendirilmelidir:

```text
Conversation
RealtimeSession
Task
ToolRun
AuditRecord
Memory
Lead
Email
Call
```

Temel kimlik alanları:

```text
tenant_id
user_id
conversation_id
session_id
```

---

## 14. Database Tenant Isolation

Persistence sorguları tenant-scoped olmalıdır.

```sql
SELECT *
FROM leads
WHERE tenant_id = ?
AND id = ?
```

Tenant izolasyonu mümkün olduğunca database boundary'de uygulanmalıdır.

---

## 15. Realtime Concurrency Limits

Birden fazla session aynı anda çalışabilir. Bu nedenle limitler katmanlı olmalıdır:

```text
Global limit
Tenant limit
User limit
Agent limit
Provider limit
```

```python
RealtimeLimits(
    max_global_sessions=500,
    max_sessions_per_tenant=20,
    max_sessions_per_user=3,
)
```

SaaS kullanımında tenant planına göre değişebilir.

---

## 16. Realtime Agent'ın Tool Yüzeyi

Realtime modele bütün tool registry verilmemelidir. Sistem büyüdükçe bu tool overload, prompt büyümesi, latency ve permission riskleri doğurur.

Realtime agent'a yüksek seviyeli bridge tool'lar verilmelidir:

```text
tasks.delegate
tasks.status
tasks.cancel
tasks.instruct

memory.recall

ui.dispatch

context.get

lead.get_summary
```

Gerekirse birkaç hızlı domain tool eklenebilir.

---

## 17. Foreground ve Background Tool Ayrımı

```python
ToolMetadata(
    execution="foreground"
)
```

ve:

```python
ToolMetadata(
    execution="background_task"
)
```

Foreground:

```text
get_time
get_current_page
lead.get_summary
get_task_status
```

Background:

```text
analyze_repository
upload_many_files
generate_report
print_document
send_bulk_email
```

Realtime konuşma background tool'u doğrudan beklememelidir.

---

## 18. Uzun Görevler Realtime Session'ı Bloklamamalı

Yanlış:

```text
Realtime Agent
 ↓
20 saniye bekle
 ↓
sonucu söyle
```

Doğru:

```text
Realtime Agent
 ↓
tasks.delegate()
 ↓
Task Runtime
 ↓
return task_id
```

Agent hemen:

```text
"Analizi başlattım."
```

diyebilir.

---

## 19. Realtime → Task Bridge

```python
async def delegate_task(
    context: AgentContext,
    request: str,
    preferred_agent: str | None = None,
) -> TaskReference:
    ...
```

Örnek sonuç:

```json
{
  "task_id": "task_481",
  "status": "queued"
}
```

---

## 20. Task Completion → Realtime Notification

Worker tamamlandığında:

```text
task.completed
```

event'i oluşur.

Realtime bridge aktif conversation varsa event'i değerlendirir. Ancak her completion konuşmayı bölmemelidir.

```text
Task completion
     ↓
Notification Policy
     ↓
Notify now?
Queue?
Silent?
```

---

## 21. Event Notification Policy

```python
class RealtimeNotificationPolicy:

    async def should_notify(
        self,
        event,
        session,
        objective,
    ) -> bool:
        ...
```

Kriterler:

```text
User currently speaking?
Agent currently speaking?
Event priority?
User waiting for this task?
Event belongs to this conversation?
Objective-related?
```

---

## 22. Interruption / Barge-In

Sesli kullanımda interruption temel özellik olmalıdır.

```text
Agent speaking
  ↓ user speech detected
INTERRUPTED
  ↓
LISTENING
```

Kullanıcı agent konuşurken yeni komut verebilmelidir.

---

## 23. Realtime UI State

Frontend tarafında en az şu state'ler desteklenmelidir:

```text
idle
connecting
listening
user_speaking
thinking
speaking
interrupted
tool_activity
waiting_approval
waiting_user
error
```

Mevcut UI Agent Dynamic Island yapısı buna genişletilebilir.

---

## 24. VAD ve Turn Detection

Turn detection provider-specific olabilir. Core yalnızca config bilmeli.

```python
TurnDetectionConfig(
    mode="server_vad"
)
```

İleride:

```text
semantic_vad
manual_push_to_talk
custom_local_vad
```

desteklenebilir.

---

## 25. Text ve Voice Aynı Conversation'ı Kullanmalı

Admin panelinde kullanıcı bazen sesle bazen text ile yazabilir.

```text
Voice:
"Son projeyi aç."

Text:
"Bunu yayınla."
```

İki giriş de aynı `conversation_id` kullanmalıdır.

---

## 26. Realtime History ve Memory Ayrımı

```text
Realtime provider history
= aktif connection context

Short-term memory
= conversation-level application context

Long-term memory
= kalıcı kullanıcı bilgileri
```

Provider session kapanınca sistem conversation bilgisini kaybetmemelidir.

---

## 27. Transcript Normalizasyonu

```python
ConversationMessage(
    id="msg_123",
    role="user",
    modality="audio",
    text="Son projeyi aç",
    conversation_id="conversation_123",
)
```

Text mesaj da aynı yapı ile tutulmalıdır.

---

## 28. Ham Audio Saklama Politikası

Varsayılan olarak ham audio DB'ye yazılmamalıdır.

Tercih edilen:

```text
Final transcript
Turn metadata
Duration
Timestamp
Provider usage
```

Ham audio yalnızca açık ürün gereksinimi varsa saklanmalıdır.

---

## 29. Event Normalizasyonu

Provider event'leri doğrudan core event sistemine sızdırılmamalıdır.

Önerilen framework event'leri:

```text
realtime.session.created
realtime.session.connected
realtime.session.disconnected
realtime.session.failed

realtime.turn.started
realtime.turn.completed
realtime.interrupted

realtime.transcript.completed

realtime.objective.completed

call.started
call.answered
call.completed
call.failed
```

---

## 30. High-Frequency Event'leri Ayırma

```text
EPHEMERAL
DURABLE
```

Ephemeral:

```text
audio.delta
partial transcript
VU meter
streaming token
```

Durable:

```text
final transcript
task.completed
tool.completed
approval.requested
call.completed
objective.completed
```

---

## 31. Audio Global EventBus Üzerinden Geçmemeli

Ham audio stream:

```text
Realtime Transport
```

üzerinden gitmelidir.

Global EventBus:

```text
control
metadata
task events
tool events
session state
```

için kullanılmalıdır.

---

## 32. Admin Realtime Agent

```text
AdminRealtimeAgent
```

Özellikler:

```text
Interactive
Long-lived session
UI actions available
Admin tool access
Broad but permission-controlled capabilities
Text + voice shared conversation
```

---

## 33. Mobile Admin Agent

Mobil uygulama aynı backend Realtime runtime'ını kullanabilir.

Channel:

```text
mobile_voice
```

Aynı agent config kullanılabilir; client capability seti farklı olabilir.

---

## 34. UI Agent ile Realtime Entegrasyonu

Realtime agent tarayıcıyı direkt kontrol etmemelidir.

```text
Realtime Agent
      ↓
UI task / ui.dispatch
      ↓
UI Dispatcher
      ↓
@imthekaiwen/agent-ui
      ↓
Semantic Action
      ↓
React
```

---

## 35. Telefon Görüşmesi Desteği

Telefon konuşması ayrı bir channel olarak modellenmelidir:

```text
PhoneChannel
```

Telephony katmanı ayrı olmalıdır:

```text
Phone Provider
      ↓
Telephony Gateway
      ↓
Realtime Runtime
      ↓
Conversation Agent
```

---

## 36. Telephony Gateway

```python
class TelephonyGateway(Protocol):

    async def start_call(
        self,
        phone_number: str,
        metadata: dict,
    ) -> CallReference:
        ...

    async def hangup(
        self,
        call_id: str,
    ) -> None:
        ...

    async def get_status(
        self,
        call_id: str,
    ) -> CallStatus:
        ...
```

Provider implementations:

```text
TwilioTelephonyGateway
SIPTelephonyGateway
CustomPBXGateway
```

---

## 37. Call Tool

Telefon araması bir side-effect tool olarak modellenmelidir.

```python
@tool(
    name="call.start",
    permissions={"calls.start"},
    requires_approval=True,
    side_effect="reversible",
)
async def start_call(...):
    ...
```

Gerçek side-effect classification ürün gereksinimine göre ayrıca belirlenebilir.

---

## 38. Realtime Telefon Görüşmesinin Amacı

Her otomatik aramada bir `ConversationObjective` olmalıdır.

```python
ConversationObjective(
    type="notify_and_collect_decision",
    goal="Notify owner about a new project lead",
    completion_conditions=[
        "owner informed",
        "owner decision captured",
    ],
    allowed_actions=[
        "lead.read",
        "email.send_template",
        "task.create",
    ],
)
```

---

## 39. Conversation Objective Tipleri

```text
notification_only
notify_and_collect_decision
approval_request
status_update
incident_alert
follow_up
collect_information
execute_guided_workflow
```

---

## 40. Persona ve Objective Ayrımı

```text
Persona
= nasıl konuşur?

Objective
= neden konuşur?
```

Aynı persona farklı objective'lerle kullanılabilir.

---

## 41. Interactive ve Objective-Driven Mode

```text
RealtimeMode.INTERACTIVE
RealtimeMode.OBJECTIVE_DRIVEN
```

Interactive:

```text
Admin panel
Mobile admin
Free conversation
```

Objective-driven:

```text
Outbound phone call
Approval call
Critical alert call
```

---

## 42. Objective State Machine

```text
CALL_STARTED
   ↓
INFORMING
   ↓
WAITING_DECISION
   ↓
ACTION_REQUESTED
   ↓
ACTION_COMPLETED
   ↓
SUMMARY
   ↓
CALL_END
```

Bu agent'ın amaçsızca sohbet etmesini önler.

---

## 43. Yeni İş Teklifi Senaryosu

```text
Website Contact Form
        ↓
LeadService
        ↓
lead.created
        ↓
Trigger Engine
        ↓
Notification Policy
        ↓
Call owner?
        ↓
TelephonyGateway.start_call()
        ↓
RealtimeSession
        ↓
NotificationCallAgent
```

Konuşma örneği:

```text
Agent:
"Yeni bir iş teklifi geldi. Ahmet Yılmaz e-ticaret sitesi için iletişime geçmiş. Detayları anlatayım mı?"

User:
"Evet."

Agent → lead.get_summary

User:
"Standart teklif mailini gönder. Yarın takip görevi oluştur."

Agent → email.send_template
Agent → task.create

Agent:
"Mail gönderildi ve yarın için takip görevi oluşturuldu."
```

Objective tamamlanır.

---

## 44. Email Realtime Özelliği Değildir

Email bir capability/tool olarak modellenmelidir.

```text
Voice
Text
Phone
Mobile
   ↓
email.send_template
```

Ayrı `RealtimeEmailTool` veya `PhoneEmailTool` yazılmamalıdır.

---

## 45. Email Service Yapısı

```text
email/
├── service.py
├── templates.py
├── provider.py
└── tools.py
```

Tool:

```text
email.send_template
```

Örnek:

```python
SendEmailInput(
    recipient_id="lead_123",
    template="proposal_followup",
    variables={
        "project_name": "...",
        "price": "...",
    },
)
```

---

## 46. Email Provider Abstraction

```text
EmailService
     ↓
EmailProvider
     │
 ┌───┼──────────────┐
 ▼   ▼              ▼
SMTP Gmail        Resend
```

Realtime sistemi mail provider'ı bilmemelidir.

---

## 47. Trigger Engine

Yeni iş teklifi gibi olaylarda agent'ın kullanıcıyı araması Realtime modelin kendi kararı olmamalıdır.

```text
Domain Event
    ↓
Trigger Engine
    ↓
Notification Policy
    ↓
Channel Router
```

---

## 48. Domain Events

```text
lead.created
lead.updated
proposal.requested
deployment.failed
deployment.completed
payment.received
security.alert
task.requires_attention
```

---

## 49. Notification Policy

```text
High priority
→ Phone

Medium priority
→ Push notification

Low priority
→ Dashboard
```

---

## 50. Agent Kendi Kendine Telefon Açmamalı

Call başlatma şu katmanlar tarafından kontrol edilmelidir:

```text
Trigger Policy
Approval Policy
Tool Permission
```

---

## 51. CallContext

```python
CallContext(
    call_id="call_123",
    tenant_id="tenant_1",
    user_id="owner_12",
    direction="outbound",
    reason="new_lead",
    related_entity={
        "type": "lead",
        "id": "lead_888",
    },
    objective=objective,
    permissions={
        "lead.read",
        "email.send",
        "task.create",
    },
)
```

---

## 52. Dynamic Instructions

Telefon görüşmesi context'e göre prompt almalıdır.

```text
You are the administrative assistant.

Channel: phone.

Reason for call:
A new project inquiry was received.

Objective:
Inform the owner and collect the owner's desired next action.

Related lead:
Lead ID: lead_888

Allowed actions:
- read lead summary
- send approved email template
- create follow-up task

Do not contact the lead unless explicitly instructed.
```

---

## 53. Minimum Necessary Context

Prompt'a gereksiz kişisel veri doldurulmamalıdır.

Başlangıçta çoğu durumda:

```text
Name
Company
Short request summary
```

yeterlidir. Contact bilgileri gerekirse tool ile alınmalıdır.

---

## 54. Session-Specific Permission Scope

Admin agent geniş yetkiye sahip olabilir. Objective-driven phone session dar tool scope ile çalışmalıdır.

```text
lead.read
email.send_template
task.create
```

---

## 55. Effective Permission Formülü

```text
effective permissions
=
user permissions
∩
agent permissions
∩
channel permissions
∩
objective permissions
```

---

## 56. Approval Seviyeleri

```text
LOW
MEDIUM
HIGH
CRITICAL
```

Örnek:

```text
lead.read → LOW
email.send_template → MEDIUM
project.publish → MEDIUM / HIGH
deployment.production → HIGH
database.delete → CRITICAL
```

---

## 57. Voice Approval Politikası

```text
LOW
→ automatic

MEDIUM
→ explicit voice confirmation

HIGH
→ authenticated app approval

CRITICAL
→ authenticated approval + additional verification
```

---

## 58. Telefon Kanalında Identity

Telefon çağrısını biri cevapladı diye kullanıcı otomatik authenticated kabul edilmemelidir.

Hassas işlemlerde:

```text
trusted phone
+
active account session
+
app confirmation
```

veya PIN gibi ek doğrulama kullanılabilir.

---

## 59. Connection Recovery

```text
Network lost
 ↓
Realtime disconnected
 ↓
Worker tasks continue
 ↓
Reconnect
 ↓
Conversation context restored
```

Realtime bağlantısı transient, conversation ve task state persistent olmalıdır.

---

## 60. Reconnect Sonrası Task Durumu

Voice connection koptuğunda worker task'lar iptal edilmemelidir.

Reconnect sonrası agent task store'dan gerçek durumu okuyabilmelidir.

---

## 61. Session Affinity ve Multi-Instance

Production'da Realtime connection belirli backend node'a bağlı olabilir.

```text
rt_session_123
→ node_4
```

Bu mapping Redis gibi shared state'te tutulabilir.

```text
Task completed
 ↓
Redis Pub/Sub
 ↓
node_4
 ↓
Realtime connection
```

---

## 62. PostgreSQL + Redis Önerisi

Local:

```text
SQLite
```

Production:

```text
PostgreSQL + Redis
```

PostgreSQL:

```text
conversations
messages
tasks
tool runs
memory
audit
calls
realtime session metadata
```

Redis:

```text
active session registry
presence
pub/sub
rate limits
distributed locks
connection routing
ephemeral state
```

---

## 63. Local Session Object

Runtime RAM içinde connection object tutulabilir:

```python
active_connections: dict[str, RealtimeConnection]
```

Ama source of truth olmamalıdır.

---

## 64. Backpressure

Realtime sistem yüksek event üretir. Her şey persistence'a yazılmamalıdır.

```text
partial transcript → drop allowed
final transcript → durable
task.completed → durable
audio chunk → stream-only
```

Queue capacity ve drop policy tanımlanmalıdır.

---

## 65. Rate Limiting

```text
Realtime sessions per tenant
Realtime sessions per user
Tool calls per minute
Outbound calls per day
Email sends per hour
Task concurrency
```

kontrol edilmelidir.

---

## 66. Tenant Bazlı Planlama

```python
TenantLimits(
    realtime_sessions=5,
    concurrent_tasks=20,
    outbound_calls_per_day=50,
)
```

---

## 67. Observability

Minimum Realtime metrikleri:

```text
session_id
conversation_id
tenant_id
user_id
channel
connected_at
disconnected_at
turn_count
interrupt_count
tool_call_count
delegated_task_count
audio_input_duration
audio_output_duration
provider_usage
errors
reconnect_count
```

---

## 68. Call Observability

```text
call_id
direction
dial_time
answer_time
duration
objective
objective_completed
hangup_reason
actions_executed
```

---

## 69. Audit

Side-effect tool kullanımı audit edilmelidir.

```text
user_id: owner_12
channel: phone
call_id: call_123
action: email.send_template
target: lead_888
approval: voice_confirmed
result: success
```

---

## 70. Secret Güvenliği

Long-lived API key frontend'e gitmemelidir. Provider SDK call'ları server-side kalmalıdır. Admin web tarafında gerekiyorsa kısa ömürlü session credential kullanılabilir.

---

## 71. Web Admin Transport

İki genel yaklaşım vardır:

```text
Browser → WebRTC → Provider
```

veya:

```text
Browser → Backend → Realtime Provider
```

Kaiwen Agent Platform açısından backend authority korunmalıdır. Tool permission, task runtime, memory, audit ve UI dispatch backend'de kalmalıdır.

---

## 72. Realtime ve UI Transport Ayrımı

UI Agent event transport'u HTTP/SSE veya WebSocket olabilir. Voice transport düşük latency için WebRTC/WebSocket olabilir. İkisini zorla aynı transport yapmaya gerek yoktur.

```text
Voice Transport → low latency
UI / Task Events → SSE / WebSocket
```

---

## 73. Shared Conversation Bridge

```text
Web Text
     │
     ▼
Conversation Service
     ▲
     │
Realtime Bridge
```

Bridge transcript, user input, task state, memory ve tool results arasında bağlantı kurar.

---

## 74. Realtime Bridge

```python
class RealtimeBridge:

    async def on_user_turn(...): ...
    async def on_tool_call(...): ...
    async def on_task_event(...): ...
    async def on_interrupt(...): ...
    async def on_disconnect(...): ...
```

---

## 75. Realtime Agent ve Worker Agent Ayrımı

Realtime agent conversation agent'tır. Worker agent execution agent'tır.

```text
Realtime Admin Agent
       ↓
tasks.delegate
       ↓
Code Worker
       ↓
Repository analysis
```

Realtime agent CodeWorker'a dönüşmemelidir.

---

## 76. Handoff Kullanımı

Handoff conversation ownership change için kullanılabilir.

```text
Sales Voice Agent
→ Billing Voice Agent
```

Ama:

```text
Admin Agent
→ Printer Worker
```

handoff değil task delegation olmalıdır.

---

## 77. Admin Realtime Kullanım Örneği

```text
User:
"Son projeyi aç ve analytics'i göster."

Realtime Agent
   ↓
project.get_latest
   ↓
ui.dispatch(open_project)
   ↓
analytics.get_summary
   ↓
Realtime response
```

---

## 78. Admin + Background Task Örneği

```text
User:
"Bu repository'yi tara, güvenlik sorunu varsa söyle."

Realtime Agent
   ↓
tasks.delegate
   ↓
SecurityWorker
   ↓
Task running
```

Agent hemen kullanıcıya taramanın başladığını söyler. Task tamamlandığında notification policy uygun görürse sonuç konuşma kanalına döner.

---

## 79. Phone Call + Tool Örneği

```text
Phone Agent:
"Yeni bir teklif geldi. Detayları anlatayım mı?"

User:
"Evet."

Agent → lead.get_summary

User:
"Standart teklifi mail at."

Agent → email.send_template

User:
"Yarın da hatırlat."

Agent → task.create_followup
```

Bütün capability'ler channel bağımsızdır.

---

## 80. Realtime Plugin Değil, Realtime Subsystem

```text
Realtime = interaction subsystem
Tools = capability subsystem
Tasks = durable execution subsystem
Memory = context subsystem
UI Agent = browser execution subsystem
Telephony = phone transport subsystem
```

Bu sınırlar korunmalıdır.

---

## 81. Önerilen Product-Side Yapı

```text
app/
├── agents/
│   ├── admin_realtime.py
│   └── lead_notification_call.py
│
├── tools/
│   ├── leads.py
│   ├── email.py
│   ├── calls.py
│   └── projects.py
│
├── email/
│   ├── service.py
│   ├── templates.py
│   └── provider.py
│
├── notification_rules/
│   └── rules.py
│
└── triggers/
    └── lead_created.py
```

---

## 82. Realtime Provider Package Bağımlılığı

Optional dependency kullanılabilir:

```text
kaiwen-agent[openai-realtime]
```

Core package provider bağımsız kalmalıdır.

---

## 83. Realtime Configuration

```python
RealtimeConfig(
    provider="openai",
    mode="interactive",
    language="tr",
    turn_detection="server_vad",
    interruption=True,
    transcription=True,
    max_session_minutes=60,
    task_delegation=True,
)
```

---

## 84. Objective-Driven Call Config

```python
RealtimeConfig(
    provider="openai",
    mode="objective_driven",
    channel="phone",
    interruption=True,
    max_session_minutes=10,
    task_delegation=True,
    require_objective=True,
)
```

---

## 85. Realtime Policy

```python
class RealtimePolicy:
    max_session_duration: int
    allow_background_tasks: bool
    allow_ui_actions: bool
    allow_phone_side_effects: bool
    approval_mode: str
```

---

## 86. Session Tool Scope

Session oluşturulurken tool seti daraltılabilir:

```python
session_tools = {
    "lead.read",
    "email.send_template",
    "task.create",
}
```

---

## 87. Capability Discovery

Web veya mobile client bağlandığında `client.capabilities` gönderebilir.

```json
{
  "actions": [
    "open_project",
    "open_modal",
    "show_notification"
  ]
}
```

---

## 88. Client ID

Her frontend connection `client_id` almalıdır.

```text
user_42
├── client_web_1
└── client_mobile_1
```

---

## 89. Birden Fazla Aktif Client

```python
UITask(
    target_client_id="client_mobile_1",
    action="open_lead",
)
```

şeklinde hedeflenebilir.

---

## 90. Realtime Session Ownership

Her realtime session bir node tarafından sahiplenilir.

```text
session_id → node_id
```

Bu mapping production'da shared registry'de tutulmalıdır.

---

## 91. Distributed Events

```text
Task Runtime
 ↓
Distributed Event Broker
 ↓
Realtime Node
```

Redis Pub/Sub ilk aşamada yeterli olabilir.

---

## 92. Graceful Shutdown

Backend kapanırken:

```text
Stop accepting new sessions
Notify / close active connections
Persist final transcript
Update session status
Release resources
```

uygulanmalıdır.

---

## 93. Provider Failure

Provider hata verirse `RealtimeSession → FAILED` olur. Conversation, tasks ve memory korunur. Kullanıcı text fallback'e geçirilebilir.

---

## 94. Fallback Strategy

```text
Realtime unavailable
      ↓
Text mode fallback
```

Admin panel tamamen kullanılamaz hale gelmemelidir.

---

## 95. Call Failure

Telefon araması:

```text
no_answer
busy
failed
rejected
```

sonuçlarını üretmelidir. Notification policy gerekirse phone failed → push notification fallback yapabilir.

---

## 96. Session Timeout

```text
web admin → longer session
phone → shorter session
objective call → objective complete → close
```

Her session sonsuza kadar açık kalmamalıdır.

---

## 97. Idle Timeout

Telefon görüşmesinde belirli sessizlik süresinden sonra follow-up veya hangup policy uygulanabilir.

---

## 98. Testing

### Unit

```text
RealtimeSession state transitions
Permission scope
Objective state machine
Notification policy
Provider event normalization
```

### Integration

```text
Realtime → task delegate
Task completion → realtime notification
Realtime → UI action
Realtime → tool approval
```

### Load

```text
100 concurrent sessions
500 concurrent sessions
multiple tenants
```

### Failure

```text
provider disconnect
Redis unavailable
task timeout
client reconnect
call no-answer
```

---

## 99. Fake Realtime Provider

Provider-free test için:

```python
FakeRealtimeProvider
```

oluşturulmalıdır. Gerçek OpenAI connection unit testlerde zorunlu olmamalıdır.

---

## 100. Load Testing

Ölçülmesi gerekenler:

```text
Concurrent sessions
Audio latency
Tool latency
Task delegation latency
Reconnect success
Memory consumption
CPU usage
Event throughput
Redis throughput
```

---

## 101. Security Testing

```text
cross-tenant session access
unauthorized tool call
forged client action
replayed approval
replayed tool request
invalid session credential
```

test edilmelidir.

---

## 102. Idempotency

Realtime konuşmada aynı command tekrar gelebilir. Network retry, provider retry veya duplicate tool call durumlarında side-effect tool iki kez çalışmamalıdır.

---

## 103. Tool Timeout

Foreground tool kısa timeout kullanabilir. Uzun süren işlem task runtime'a delegate edilmelidir.

---

## 104. Retry

Network tool retryable olabilir. Destructive tool automatic retry konusunda dikkatli olunmalıdır. Idempotency olmadan retry yapılmamalıdır.

---

## 105. Memory Entegrasyonu

```text
Realtime transcript
      ↓
Conversation Runtime
      ↓
Memory Manager
```

Realtime agent memory sistemini doğrudan sahiplenmemelidir.

---

## 106. Memory Write Policy

Realtime model doğrudan long-term DB yazmamalıdır.

```text
Realtime transcript
 ↓
Memory Candidate
 ↓
Memory Policy
 ↓
Store / Ignore
```

---

## 107. Privacy

Voice transcript ve call metadata hassas olabilir. Tenant ve kullanıcı bazlı retention policy tanımlanmalıdır.

Örnek:

```text
Raw audio → not stored
Transcript → 30 days
Call metadata → 180 days
Audit → 1 year
```

---

## 108. Tool Permission ve Memory

Memory'den gelen bilgi permission bypass etmemelidir. Her side-effect tool normal permission kontrolünden geçmelidir.

---

## 109. Phone Agent Tool Scope

Örnek lead notification call:

```text
Allowed:
lead.read
email.send_template
task.create_followup

Denied:
server.deploy
project.delete
database.write
filesystem.raw
```

---

## 110. Admin Agent Tool Scope

Admin Realtime Agent daha geniş capability set kullanabilir; backend permission sistemi yine uygulanır.

---

## 111. Realtime Agent Stateless Definition

Agent definition shared olabilir; session state her kullanıcı ve connection için ayrıdır.

```text
AgentDefinition
      │
 ┌────┼────┐
 ▼    ▼    ▼
RT1  RT2  RT3
```

---

## 112. Provider State Isolation

Provider session state yalnızca ilgili realtime session'a ait olmalıdır. Session'lar arasında paylaşılmamalıdır.

---

## 113. Performance İlkeleri

```text
Audio → stream
Partial transcripts → memory-only
Final transcript → durable
Tool execution → async
Long work → task runtime
High-frequency events → ephemeral
Connection routing → Redis
Persistent state → Postgres
```

---

## 114. Latency İlkeleri

Voice deneyiminde fast path ve slow path ayrımı yapılmalıdır.

Fast:

```text
simple answer
quick read tool
status query
UI action
```

Slow:

```text
repository analysis
large upload
report generation
bulk mail
```

Slow işler delegate edilmelidir.

---

## 115. Fast Path

```text
User speech
 ↓
Realtime
 ↓
Quick tool
 ↓
Realtime response
```

---

## 116. Slow Path

```text
User speech
 ↓
Realtime
 ↓
tasks.delegate
 ↓
Immediate acknowledgement
 ↓
Background task
 ↓
task.completed event
 ↓
Realtime notification
```

---

## 117. Realtime + UI + Task Birlikte

```text
"Son projeyi aç, son analytics'i getir ve performans raporu hazırla."
```

Plan:

```text
Task 1: open_project → UI
Task 2: analytics.read → foreground
Task 3: generate performance report → background task
```

Realtime:

```text
"Projeyi açtım. Son analytics'i gösteriyorum. Raporu da hazırlamaya başladım."
```

---

## 118. Realtime + Phone + Email Birlikte

```text
lead.created
 ↓
Trigger Engine
 ↓
PhoneChannel
 ↓
Realtime Agent
 ↓
Objective
 ↓
User decision
 ↓
email.send_template
 ↓
task.create_followup
 ↓
Objective completed
```

---

## 119. Güncellenmiş High-Level Mimari

```text
                             DOMAIN EVENTS
                                  │
                                  ▼
                            Trigger Engine
                                  │
                                  ▼
                        Notification Policy
                                  │
                                  ▼
                           Channel Router
                 ┌────────────────┼────────────────┐
                 ▼                ▼                ▼
               Web             Mobile           Phone
                 │                │                │
                 └────────────────┼────────────────┘
                                  ▼
                        Realtime Runtime
                                  │
                                  ▼
                      Conversation Runtime
                                  │
         ┌────────────────────────┼───────────────────────┐
         ▼                        ▼                       ▼
      Memory                 Orchestrator             UI Runtime
                                  │
                                  ▼
                            Task Runtime
                                  │
                      ┌───────────┼────────────┐
                      ▼           ▼            ▼
                    Email       Leads       Printer
                    Tools       Tools        Tools
```

---

## 120. Realtime Entegrasyonunun Sınırı

Realtime subsystem:

```text
Voice / text streaming
Connection
Session
Turn detection
Interruption
Conversation objective
Provider event handling
Realtime bridge
```

ile ilgilenmelidir.

Email, lead, printer, database ve deployment logic Realtime modülünde olmamalıdır.

---

## 121. Uygulama Sırası

### Faz R1 — Realtime Core

```text
RealtimeProvider
RealtimeConnection
RealtimeSessionRecord
RealtimeSessionManager
Provider event normalization
FakeRealtimeProvider
```

### Faz R2 — OpenAI Adapter

```text
OpenAI realtime provider
audio input
audio output
text
interrupt
transcript
usage
```

### Faz R3 — Conversation Bridge

```text
conversation_id
text + voice unified history
RealtimeBridge
task delegation
task status
```

### Faz R4 — Admin Web

```text
AdminRealtimeAgent
Web channel
UI Agent integration
Dynamic Island states
```

### Faz R5 — Memory

```text
short-term context
memory recall
memory write policy
```

### Faz R6 — Multi-Tenant

```text
tenant_id
tenant-scoped persistence
session limits
rate limits
distributed registry
```

### Faz R7 — Production Scale

```text
PostgreSQL
Redis
distributed event broker
session routing
backpressure
metrics
```

### Faz R8 — Telephony

```text
TelephonyGateway
PhoneChannel
CallContext
Phone provider
outbound calls
```

### Faz R9 — Objective Calls

```text
ConversationObjective
Objective state machine
Trigger Engine
Notification Policy
lead.created flow
```

### Faz R10 — Security Hardening

```text
approval levels
identity verification
cross-tenant tests
load tests
audit
retention
```

---

## 122. Sonuç

Bu mimaride Realtime / Live API, Agent Runtime'ın yerine geçen bir sistem değildir.

Doğru rolü:

```text
Conversation Channel
```

Sistemin ana parçaları:

```text
Realtime Runtime
Conversation Runtime
Agent Runtime
Task Runtime
Tool Runtime
UI Runtime
Memory Runtime
Telephony Runtime
Trigger Runtime
```

olarak ayrılır.

En kritik tasarım ilkeleri:

```text
Realtime != Task Engine
Realtime != Tool System
Realtime != UI Agent
Realtime != Memory
Realtime != Telephony
Realtime = düşük gecikmeli conversation runtime
```

Telefon arama, email gönderme, UI değiştirme veya uzun görev çalıştırma; tool, task ve channel katmanları üzerinden gerçekleştirilir.

Bu sınırlar korunursa sistem:

```text
bugün:
kişisel admin voice assistant

yarın:
mobile admin assistant

sonra:
phone notification agent

ileride:
multi-tenant agent platform
```

haline güvenli ve kontrollü biçimde ölçeklenebilir.
