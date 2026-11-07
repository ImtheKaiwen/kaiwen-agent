# Kaiwen Reusable Agent Framework — Mimari Tasarım Dokümanı

## 1. Amaç

Bu dokümanın amacı, yalnızca tek bir web sitesinde kullanılacak bir chatbot yapmak yerine, farklı projelerde tekrar tekrar kullanılabilecek genel amaçlı bir **agent framework / agent runtime / agent SDK** mimarisi tasarlamaktır.

Bu sistemin temel hedefleri şunlardır:

- Tek bir projeye bağlı olmamak.
- Web sitesi, masaüstü uygulaması, mobil uygulama, terminal, API veya başka bir istemci tarafından kullanılabilmek.
- Hem yazılı hem sesli iletişimi desteklemek.
- Live API / Realtime bağlantısını zorunlu değil, isteğe bağlı bir katman olarak sunmak.
- Bir ana ajan üzerinden alt ajanlara görev dağıtabilmek.
- Her alt ajanın kendi tool setine sahip olabilmesi.
- Public kullanıcı ajanı ve admin ajanı gibi farklı yetki alanlarını güvenli biçimde ayırabilmek.
- Tool sisteminin genişletilebilir olması.
- Uzun süren işleri task olarak takip edebilmek.
- Task'lar arasında bağımlılık kurabilmek.
- Uygun işleri paralel çalıştırabilmek.
- Fiziksel veya mantıksal kaynaklara lock koyabilmek.
- Ajanlar çalışırken kullanıcıyla konuşmanın devam edebilmesi.
- Çalışan görevlere sonradan müdahale edilebilmesi.
- Farklı LLM sağlayıcılarını destekleyebilmek.
- Aynı agent çekirdeğini farklı projelerde kütüphane olarak yeniden kullanabilmek.

Bu yapı, basit bir “LLM + tool calling” çözümünden daha kapsamlıdır.

Hedeflenen sistem kabaca şu seviyededir:

```text
Kullanıcı
   ↓
Ses / Yazı / API / Uygulama
   ↓
Conversation Layer
   ↓
Orchestrator
   ↓
Planner
   ↓
Task Graph
   ↓
Scheduler
   ↓
Specialized Agents
   ↓
Tools / External Systems
```

---

# 2. Temel Fikir

Ana prensip şudur:

> Agent çekirdeği projeye özel olmamalıdır. Projeler yalnızca agent konfigürasyonu, tool'lar, permission'lar ve domain mantığını sağlamalıdır.

Örneğin:

```text
kaiwen-agent
```

genel amaçlı framework olur.

Bunun üzerinde:

```text
kaiwen-website
```

çalışabilir.

Aynı framework daha sonra:

```text
desktop-agent
printer-agent-system
company-internal-agent
mobile-assistant
automation-agent
```

gibi farklı projelerde de kullanılabilir.

Böylece her yeni projede agent sistemi baştan yazılmaz.

---

# 3. Katmanlı Mimari

Sistemin mümkün olduğunca açık sorumluluklara ayrılması gerekir.

Önerilen ana katmanlar:

```text
Client Layer
Transport Layer
Conversation Layer
Orchestration Layer
Planning Layer
Task Layer
Scheduling Layer
Agent Layer
Tool Layer
Permission Layer
Model Layer
Persistence Layer
Observability Layer
```

Genel akış:

```text
                    CLIENTS

        ┌──────────┬──────────┬───────────┐
        │ Web Chat │ Voice UI │ Other App │
        └────┬─────┴────┬─────┴────┬──────┘
             │          │          │
             ▼          ▼          ▼
          Text API   Realtime   REST/WebSocket
             │          │          │
             └──────────┼──────────┘
                        ▼
               Conversation Agent
                        │
                        ▼
                  Orchestrator
                        │
             ┌──────────┼──────────┐
             ▼          ▼          ▼
          Planner   Task Manager  Context
             │          │
             ▼          ▼
          Task DAG   Scheduler
                        │
        ┌───────────────┼───────────────┐
        ▼               ▼               ▼
    File Agent      Cloud Agent     Printer Agent
        │               │               │
        ▼               ▼               ▼
    File Tools      Cloud Tools      Print Tools
```

---

# 4. Agent Core ile Proje Kodunun Ayrılması

En kritik tasarım kararlarından biri budur.

Framework:

```text
kaiwen-agent/
```

Proje:

```text
kaiwen-website/
```

olarak ayrı tutulmalıdır.

Önerilen framework yapısı:

```text
kaiwen-agent/
│
├── core/
│   ├── agent.py
│   ├── runtime.py
│   ├── context.py
│   ├── events.py
│   ├── exceptions.py
│   └── types.py
│
├── models/
│   ├── base.py
│   ├── openai.py
│   ├── realtime.py
│   ├── ollama.py
│   └── router.py
│
├── agents/
│   ├── base.py
│   ├── registry.py
│   ├── manager.py
│   └── worker.py
│
├── tools/
│   ├── base.py
│   ├── registry.py
│   ├── permissions.py
│   ├── middleware.py
│   └── approval.py
│
├── tasks/
│   ├── task.py
│   ├── manager.py
│   ├── scheduler.py
│   ├── graph.py
│   ├── status.py
│   ├── result.py
│   └── store.py
│
├── sessions/
│   ├── session.py
│   ├── memory.py
│   └── store.py
│
├── realtime/
│   ├── session.py
│   ├── audio.py
│   ├── events.py
│   └── adapter.py
│
├── security/
│   ├── permissions.py
│   ├── policies.py
│   ├── roles.py
│   └── approval.py
│
├── observability/
│   ├── tracing.py
│   ├── logger.py
│   ├── metrics.py
│   └── audit.py
│
├── resources/
│   ├── locks.py
│   └── manager.py
│
├── control/
│   ├── mailbox.py
│   ├── commands.py
│   └── interrupts.py
│
└── integrations/
    ├── fastapi.py
    ├── websocket.py
    ├── mcp.py
    └── cli.py
```

Site tarafı ise:

```text
kaiwen-website/
│
├── app/
│
├── agents/
│   ├── public_agent.py
│   └── admin_agent.py
│
├── tools/
│   ├── public/
│   │   ├── get_projects.py
│   │   ├── get_services.py
│   │   └── create_contact_request.py
│   │
│   └── admin/
│       ├── create_project.py
│       ├── update_project.py
│       ├── delete_project.py
│       ├── publish_project.py
│       └── upload_media.py
│
├── permissions/
│   ├── public.py
│   └── admin.py
│
└── main.py
```

Bu sayede framework siteye bağlı kalmaz.

---

# 5. Input Türünden Bağımsız Agent Runtime

Agent çekirdeğinin kullanıcı girdisinin nereden geldiğini bilmemesi gerekir.

Girdi şunlardan gelebilir:

```text
Text Box
Microphone
CLI
REST API
WebSocket
Mobile App
Desktop App
Discord
WhatsApp
Voice Device
```

Hepsi ortak bir yapıya normalize edilmelidir.

Örneğin:

```python
AgentInput(
    type="message",
    content="Son projelerimi göster",
    user_id="123",
    session_id="abc",
    metadata={}
)
```

Akış:

```text
TEXT ───────────┐
                │
VOICE ──────────┤
                ▼
API ──────── AgentInput
                │
MOBILE ─────────┤
                │
CLI ────────────┘
```

Agent runtime yalnızca `AgentInput` ile ilgilenir.

---

# 6. Live API / Realtime Katmanı Optional Olmalı

Realtime bağlantı agent'ın kendisi olmamalıdır.

Realtime yalnızca bir interface / transport / conversation adapter olarak görülmelidir.

Normal kullanım:

```python
agent = AgentRuntime(
    model=model,
    tools=tools
)

result = await agent.run(
    "Projelerimi göster"
)
```

Realtime kullanım:

```python
session = await agent.realtime()
await session.start()
```

Aynı agent:

```text
Web Chat
   │
   ├────────────┐
   │            │
   ▼            ▼
Text          Voice
   │            │
   └──────┬─────┘
          ▼
     Agent Runtime
```

Bu sayede bazı projelerde Live API hiç kullanılmayabilir.

Bazılarında yalnızca text kullanılabilir.

Bazılarında ikisi aynı anda bulunabilir.

Özellikle admin panelinde:

```text
Admin Chat Box
+
Voice / Live API
```

aynı agent'a bağlanabilir.

---

# 7. Conversation Agent

Kullanıcıyla doğrudan iletişim kuran ayrı bir agent veya conversation layer bulunması faydalıdır.

Görevleri:

- Kullanıcıyı dinlemek.
- Konuşma context'ini yönetmek.
- Basit sorulara cevap vermek.
- Gerektiğinde orchestrator'a görev göndermek.
- Aktif task durumlarını sorgulamak.
- Kullanıcının çalışan görevlere müdahale etmesini sağlamak.
- Worker agent sonuçlarını kullanıcıya doğal dille aktarmak.

Bu agent uzun süren işlerin kendisini yapmak zorunda değildir.

Örneğin:

```text
User:
"Bu klasördeki dosyaları Azure'a yükle,
sonra iki sayfa yan yana olacak şekilde yazdır."

Conversation Agent:
→ Orchestrator'a gönder
→ Kullanıcıyla konuşmaya devam et
```

---

# 8. Orchestrator

Orchestrator sistemin koordinasyon merkezidir.

Ana görevleri:

- Kullanıcı isteğinin doğrudan cevap mı yoksa görev mi olduğunu belirlemek.
- Gerekirse Planner çalıştırmak.
- Uygun agent'ları seçmek.
- Task oluşturmak.
- Dependency kurmak.
- Scheduler'a task vermek.
- Sonuçları toplamak.
- Re-planning gerektiğinde tekrar Planner çağırmak.

Basit mimari:

```text
Conversation Agent
       │
       ▼
   Orchestrator
       │
 ┌─────┼─────┐
 ▼     ▼     ▼
Plan  Task  Agents
```

---

# 9. Planner

Planner'ın görevi kullanıcı isteğini uygulanabilir alt görevlere bölmektir.

Örneğin:

```text
"Şu dosyaları bul,
Azure'a gönder,
sonra yazıcıdan iki sayfa yan yana çıkar."
```

Planner şöyle bir plan üretebilir:

```text
Task 1:
Dosyaları bul

Task 2:
Azure'a yükle
depends_on: Task 1

Task 3:
PDF baskı düzenini hazırla
depends_on: Task 1

Task 4:
Yazdır
depends_on: Task 3
```

Burada `Task 2` ve `Task 3` paralel çalışabilir.

Ancak `Task 4`, Task 3 tamamlanmadan başlayamaz.

---

# 10. Planner ile Executor Aynı Şey Olmamalı

Planner yalnızca plan üretmelidir.

Executor / Scheduler planı çalıştırmalıdır.

Bu ayrım çok önemlidir.

Yanlış yaklaşım:

```text
LLM:
"Önce bunu yapacağım...
sonra bunu yapacağım..."
```

ve state'i kendi prompt'unda tutması.

Doğru yaklaşım:

```text
Planner
   ↓
Structured Task Graph
   ↓
Task Manager
   ↓
Scheduler
   ↓
Executor
```

Gerçek state backend'de tutulmalıdır.

---

# 11. Task Kavramı

Agent ile Task aynı şey değildir.

Örneğin:

```text
PrinterAgent
```

bir uzmanlık alanıdır.

Ama:

```text
"rapor.pdf'i çift taraflı yazdır"
```

bir task'tır.

Bir agent aynı anda veya zaman içinde birçok farklı task çalıştırabilir.

Örnek:

```text
PrinterAgent
   │
   ├── Task #321
   ├── Task #322
   └── Task #323
```

---

# 12. AgentTask Veri Modeli

Önerilen temel yapı:

```python
class AgentTask:
    id: UUID

    name: str
    description: str

    status: TaskStatus

    parent_id: UUID | None
    agent_id: str

    input: dict
    output: dict | None

    dependencies: list[UUID]

    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

    progress: float | None
    error: str | None

    metadata: dict
```

---

# 13. Task Status Modeli

Temel status'lar:

```python
class TaskStatus(Enum):
    PENDING = "pending"
    READY = "ready"
    QUEUED = "queued"

    RUNNING = "running"

    WAITING_DEPENDENCY = "waiting_dependency"
    WAITING_RESOURCE = "waiting_resource"
    WAITING_USER = "waiting_user"
    WAITING_APPROVAL = "waiting_approval"

    PAUSED = "paused"

    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

UI örneği:

```text
● Ana görev

├─ ✓ Dosyalar bulundu
├─ ✓ Azure'a yüklendi
├─ ● PDF hazırlanıyor
│    └─ 72%
│
└─ ◷ Yazdır
     Waiting dependency
```

---

# 14. Parent / Child Task Yapısı

Büyük görevler alt görevlere bölünebilir.

Örneğin:

```text
Ana görev:
"Siteye yeni projeyi ekle"

    ├── Proje bilgilerini çıkar
    ├── Açıklama oluştur
    ├── Görsel hazırla
    ├── DB kaydı oluştur
    └── Yayınla
```

Database:

```text
task_id     parent_id

100         null
101         100
102         100
103         100
104         100
```

Bu yapı hem görsel takip hem de task aggregation için gereklidir.

---

# 15. Dependency Graph / DAG

Task sıralamasını LLM'in hafızasına bırakmak yerine, dependency graph kullanılmalıdır.

Örnek:

```text
                ┌────────────┐
                │ Find Files │
                └─────┬──────┘
                      │
          ┌───────────┴───────────┐
          ▼                       ▼
     Upload Azure            Prepare PDF
          │                       │
          ▼                       ▼
    Verify Upload               Print
```

Bir task:

```python
Task(
    id="print-1",
    agent="printer",
    depends_on=["prepare-pdf-1"]
)
```

şeklinde tanımlanabilir.

Scheduler yalnızca dependency'leri tamamlanan task'ları `READY` yapar.

---

# 16. Paralel Çalışma

Bağımsız task'lar paralel çalışabilir.

Örneğin:

```text
Find Files
    ↓
┌───────────────┬───────────────┐
│               │               │
▼               ▼               ▼
Upload      Prepare PDF     Generate Log
```

Bu sayede uzun görevler daha hızlı çalışabilir.

Ancak paralellik her durumda uygulanmamalıdır.

Yan etkili veya fiziksel kaynak kullanan task'lar kaynak limitlerine tabidir.

---

# 17. Agent, AgentRun ve Task Ayrımı

Bu üç kavramın birbirinden ayrılması kritik önemdedir.

## Agent

Agent'ın yetenek tanımıdır.

Örneğin:

```text
PrinterAgent
```

## AgentRun

Belirli bir agent'ın belirli bir çalışma instance'ıdır.

```text
PrinterAgent
   ├── Run #501
   ├── Run #502
   └── Run #503
```

## Task

Gerçek yapılacak iştir.

```text
"invoice.pdf'i yazdır"
```

Agent stateless veya mümkün olduğunca stateless tutulmalıdır.

State `AgentRun` içinde tutulmalıdır.

---

# 18. Agent'ı Stateful Yapmaktan Kaçınma

Şu yaklaşım sorun çıkarabilir:

```python
printer_agent.current_task = ...
```

Çünkü aynı agent aynı anda birden fazla task çalıştırmak istediğinde state çakışır.

Bunun yerine:

```python
AgentRun(
    id="run-100",
    agent="printer",
    task_id="task-200",
    context=...
)
```

kullanılmalıdır.

Her run kendi:

- context
- messages
- tool calls
- status
- output
- error
- event history

bilgisini taşımalıdır.

---

# 19. Specialized Agents

Sistem istediğiniz kadar uzman agent içerebilir.

Örneğin:

```text
File Agent
Cloud Agent
Printer Agent
Code Agent
Research Agent
Database Agent
Website Agent
Admin Agent
Deployment Agent
```

Agent registry:

```python
agents.register(FileAgent())
agents.register(AzureAgent())
agents.register(PrinterAgent())
agents.register(CodeAgent())
```

Orchestrator:

```python
await orchestrator.delegate(
    agent="printer",
    task={
        "file": "...",
        "pages_per_sheet": 2
    }
)
```

---

# 20. Agent Registry

Her agent merkezi bir registry üzerinden bulunmalıdır.

Örneğin:

```python
class AgentRegistry:
    def register(self, agent):
        ...

    def get(self, name: str):
        ...

    def list(self):
        ...
```

Böylece agent'lar hard-code edilmez.

Yeni agent eklemek framework çekirdeğini değiştirmeyi gerektirmez.

---

# 21. Tool Sistemi

Tool sistemi plugin benzeri çalışmalıdır.

Yanlış yaklaşım:

```python
if tool == "get_projects":
    ...
elif tool == "delete_project":
    ...
elif tool == "print_document":
    ...
```

Doğru yaklaşım:

```python
tools = ToolRegistry()

tools.register(GetProjectsTool())
tools.register(DeleteProjectTool())
tools.register(PrintDocumentTool())
```

---

# 22. Tool Registry

Önerilen yapı:

```python
class ToolRegistry:
    def register(self, tool):
        ...

    def get(self, name: str):
        ...

    def list_for_agent(self, agent_id: str):
        ...
```

Tool metadata:

```python
ToolMetadata(
    name="delete_project",
    description="Deletes a project",
    permissions=["projects.delete"],
    requires_approval=True,
    resource=None
)
```

---

# 23. Decorator ile Tool Tanımlama

Kullanım kolaylığı için:

```python
@tool(
    name="create_project",
    permission="admin.projects.create"
)
async def create_project(
    title: str,
    description: str
):
    ...
```

Framework bunu otomatik olarak registry'ye kaydedebilir.

---

# 24. Agent İçinde Tool Kullanımı

Örneğin PrinterAgent:

```text
PrinterAgent
 │
 ├── list_printers
 ├── get_printer_status
 ├── prepare_pdf
 ├── print_document
 └── cancel_print_job
```

FileAgent:

```text
FileAgent
 │
 ├── find_file
 ├── read_file
 ├── copy_file
 └── move_file
```

CloudAgent:

```text
CloudAgent
 │
 ├── upload_file
 ├── download_file
 ├── list_files
 └── verify_upload
```

---

# 25. Public Agent ve Admin Agent Ayrımı

Public frontend agent ile admin agent aynı tool setine sahip olmamalıdır.

Örnek:

```text
Public Agent
   │
   ├── projects.read
   ├── services.read
   └── contact.create
```

Admin Agent:

```text
Admin Agent
   │
   ├── projects.read
   ├── projects.create
   ├── projects.update
   ├── projects.delete
   ├── media.upload
   └── deploy.execute
```

Bu ayrım yalnızca prompt üzerinden yapılmamalıdır.

Gerçek backend authorization uygulanmalıdır.

---

# 26. Permission Sistemi

Güvenlik LLM'e bırakılmamalıdır.

Yanlış:

```python
if agent.name == "admin":
    allow_delete()
```

Daha doğru:

```python
@tool(
    permission="project.delete"
)
async def delete_project(project_id: str):
    ...
```

Execution:

```text
LLM
 ↓
Tool Request
 ↓
Permission Middleware
 ↓
Authorization Check
 ↓
Tool Execution
```

Yetkisi yoksa:

```text
DENIED
```

olmalıdır.

---

# 27. AgentContext

Her run ortak bir context alabilir.

Örnek:

```python
AgentContext(
    user=user,
    session=session,

    permissions=[
        "projects.read",
        "projects.write"
    ],

    metadata={
        "application": "kaiwen-web",
        "environment": "production"
    }
)
```

Tool:

```python
async def delete_project(
    ctx: AgentContext,
    project_id: str
):
    ctx.require("projects.delete")
```

---

# 28. Tool Approval Sistemi

Bazı tool'lar otomatik çalışabilir:

```text
get_projects
search_site
read_file
get_printer_status
```

Bazıları kullanıcı onayı gerektirmelidir:

```text
delete_project
publish_project
send_email
print_document
restart_server
shutdown_pc
deploy_production
```

Metadata:

```python
@tool(
    name="delete_project",
    requires_approval=True
)
```

Akış:

```text
Agent
 ↓
Tool Request
 ↓
Approval Required
 ↓
User Approval
 ↓
Tool Execute
```

---

# 29. Resource Lock

Agent paralel olabilir ancak fiziksel kaynak paralel olmayabilir.

Örneğin yazıcı:

```text
resource: printer.hp_laserjet_01
concurrency: 1
```

Scheduler:

```text
Task A → RUNNING

Task B → WAITING_RESOURCE

Task C → WAITING_RESOURCE
```

Benzer lock'lar:

```text
GPU
Microphone
Camera
Scanner
Database migration
Deployment slot
Filesystem exclusive write
```

için kullanılabilir.

---

# 30. Resource Manager

Önerilen yapı:

```python
class ResourceManager:
    async def acquire(self, resource_id, run_id):
        ...

    async def release(self, resource_id, run_id):
        ...

    def available(self, resource_id):
        ...
```

Tool metadata:

```python
@tool(
    resource="printer.hp_laserjet_01"
)
```

şeklinde olabilir.

---

# 31. Scheduler

Scheduler şu görevleri üstlenmelidir:

- READY task'ları bulmak.
- Dependency kontrol etmek.
- Resource kontrol etmek.
- Agent concurrency kontrol etmek.
- Task başlatmak.
- Timeout uygulamak.
- Retry yapmak.
- Failure durumlarını yönetmek.
- Cancellation uygulamak.

Basit algoritma:

```text
for each PENDING task:
    if dependencies completed:
        READY

for each READY task:
    if resources available:
        QUEUED

worker picks QUEUED
    ↓
RUNNING
```

---

# 32. Foreground ve Worker Run Ayrımı

Kullanıcıyla konuşma uzun task'lar yüzünden kilitlenmemelidir.

İki execution türü düşünülmelidir.

## Foreground

Kullanıcı anlık cevap bekler.

Örnek:

```text
"Saat kaç?"
```

## Worker

Uzun veya çok adımlı görev.

Örnek:

```text
"Bu 50 dosyayı incele,
uygun olanları Azure'a yükle,
ardından yazdır."
```

Mimari:

```text
Realtime Conversation
       │
       ▼
   Orchestrator
       │
       ├── Foreground
       │
       └── Worker Task
                │
                ▼
           Task Scheduler
```

---

# 33. Agent Çalışırken Kullanıcıyla Konuşmaya Devam Etme

Örnek:

```text
Task A - Upload
RUNNING

Task B - Prepare PDF
RUNNING

Task C - Print
WAITING
```

Kullanıcı bu sırada:

```text
"Hangi yazıcıyı kullanıyorsun?"
```

diye sorabilir.

Conversation Agent:

```text
get_active_tasks()
get_task_status()
get_agent_run_status()
```

gibi internal tool'larla state'i sorgular.

Sonra:

```text
"HP LaserJet kullanılıyor.
PDF hazırlama halen devam ediyor."
```

diye cevap verebilir.

Ana conversation thread worker task tamamlanana kadar bloklanmamalıdır.

---

# 34. Task Management Tool'ları

Conversation Agent'a sistem içi yönetim tool'ları verilebilir.

Örneğin:

```text
get_active_tasks()
get_task_status(task_id)
get_task_result(task_id)
pause_task(task_id)
resume_task(task_id)
cancel_task(task_id)
send_task_instruction(task_id, message)
get_agent_run_status(run_id)
```

Bu tool'lar gerçek backend state'ini sorgulamalıdır.

LLM tahmin yürütmemelidir.

---

# 35. Event-Driven Mimari

Sistemdeki önemli her şey event üretmelidir.

Örnek event'ler:

```text
agent.started
agent.finished

run.started
run.completed
run.failed

task.created
task.started
task.progress
task.completed
task.failed
task.cancelled

tool.started
tool.completed
tool.failed

agent.delegated

resource.waiting
resource.acquired
resource.released

approval.requested
approval.approved
approval.rejected

realtime.connected
realtime.disconnected
```

---

# 36. Event Bus

Önerilen event sistemi:

```python
await events.emit(
    "task.started",
    {
        "task_id": task.id,
        "agent": agent.name
    }
)
```

Event Bus tüketicileri:

```text
Logger
WebSocket
Audit
Metrics
Frontend UI
Tracing
Database Event Store
```

---

# 37. Frontend Task Tracking

Frontend WebSocket veya SSE ile event dinleyebilir.

Örnek UI:

```text
● Ana Agent düşünüyor...

├─ ✓ Projeleri taradı
├─ ● Yeni proje içeriğini hazırlıyor
├─ ○ Veritabanına kaydedilecek
└─ ○ Site yayınlanacak
```

Ayrıca:

```text
Agent: CodeAgent
Status: Running
Task: Repository analysis
Progress: 41%
Current step: Inspecting api/routes.py
```

gibi bilgiler gösterilebilir.

---

# 38. Çalışan Agent'a Soru Sormak

İki farklı durum vardır.

## Durum 1 — Görevin durumunu sormak

Örnek:

```text
"Şu anda hangi dosyayı inceliyorsun?"
```

Conversation Agent:

```text
get_agent_run_status(run_id)
```

kullanır.

Worker'ın ana execution'ını durdurmak gerekmez.

## Durum 2 — Çalışan göreve yeni talimat vermek

Örnek:

```text
"node_modules klasörünü inceleme."
```

Bu durumda task'a control message gönderilir.

---

# 39. Run Control Channel

Önerilen yapı:

```text
User
 ↓
Conversation Agent
 ↓
Task Manager
 ↓
Run Control Bus
 ↓
Worker Agent
```

Örnek:

```python
await task_manager.send_instruction(
    task_id,
    "Ignore node_modules"
)
```

Worker uygun checkpoint'te yeni talimatı okur.

---

# 40. Task Mailbox

Her AgentRun veya Task için bir mailbox bulunabilir.

```text
AgentRun #143

Inbox:
 ├── user_instruction
 ├── cancellation
 ├── pause
 ├── resume
 ├── approval
 └── system_event
```

Worker:

```python
events = await ctx.read_messages()
```

ile mailbox kontrolü yapabilir.

---

# 41. Interrupt ve Checkpoint Mantığı

Her tool çağrısında veya mantıksal adım sonunda worker bir checkpoint'e gelebilir.

Checkpoint'te:

```text
Cancellation var mı?
Pause var mı?
Yeni instruction var mı?
Approval bekleniyor mu?
Resource değişti mi?
```

kontrol edilir.

Bu sayede çalışan görev güvenli biçimde kontrol edilebilir.

---

# 42. Dynamic Re-Planning

Plan her zaman ilk haliyle tamamlanamayabilir.

Örneğin:

```text
Upload Azure
    ↓
FAILED
```

Sebep:

```text
Authentication required
```

Task Manager failure'ı Planner'a iletir.

Planner yeni plan üretir:

```text
1. Kullanıcıdan Azure login onayı al
2. Login işlemini başlat
3. Upload task'ını retry et
4. Devam eden bağımsız task'ları sürdür
```

Akış:

```text
PLAN V1
   ↓
Execution
   ↓
Problem
   ↓
Re-Planning
   ↓
PLAN V2
```

---

# 43. Retry Politikaları

Her task aynı retry davranışına sahip olmamalıdır.

Örneğin:

```text
Network timeout
→ retry

Permission denied
→ retry yapma

User rejected approval
→ cancelled / rejected

Printer offline
→ wait_resource veya wait_user
```

Task metadata:

```python
RetryPolicy(
    max_attempts=3,
    backoff="exponential",
    retry_on=["network_error", "timeout"]
)
```

---

# 44. Model Provider Abstraction

Framework yalnızca tek bir LLM sağlayıcısına bağlı olmamalıdır.

Ortak interface:

```python
class ModelProvider(Protocol):

    async def generate(
        self,
        messages,
        tools,
        context
    ) -> ModelResponse:
        ...
```

Provider'lar:

```text
OpenAIProvider
OllamaProvider
AnthropicProvider
GeminiProvider
CustomProvider
```

Agent:

```python
agent = Agent(
    model=OpenAIProvider(...)
)
```

Başka proje:

```python
agent = Agent(
    model=OllamaProvider(...)
)
```

Agent'ın geri kalanı değişmez.

---

# 45. Model Router

Farklı işler için farklı modeller kullanılabilir.

Örnek:

```text
Live / Conversation
        ↓
   Model Router
        │
   ┌────┼───────────────┐
   ▼    ▼               ▼
 Fast  Reasoning      Coding
 LLM      LLM           LLM
```

Konfigürasyon:

```python
router = ModelRouter()

router.add(
    capability="fast",
    model=FastModel()
)

router.add(
    capability="reasoning",
    model=ReasoningModel()
)

router.add(
    capability="coding",
    model=CodexModel()
)
```

Agent:

```python
await agent.delegate(
    capability="coding",
    task="Bu repo'daki bug'ı çöz"
)
```

---

# 46. Live Model'in Rolü

Live model her ağır işi kendi yapmak zorunda değildir.

Rolü:

```text
Dinle
↓
Niyeti anla
↓
Basitse cevapla
↓
Gerekirse task oluştur
↓
Uygun modele / agent'a yönlendir
↓
Sonucu kullanıcıya aktar
```

Örnek:

```text
User:
"Bu repository'deki concurrency bug'ını bul."

Live Agent
   ↓
Coding Agent / Codex
   ↓
Analysis
   ↓
Result
   ↓
Live Agent
   ↓
Voice response
```

---

# 47. Memory Katmanlarını Ayırma

Aşağıdaki state türleri birbirine karıştırılmamalıdır.

## Session Memory

Kullanıcı ne konuştu?

## Task State

Agent şu anda ne yapıyor?

## Application Data

Sitenin gerçek verisi nedir?

## Long-Term Memory

Kullanıcının uzun vadeli tercihleri nelerdir?

## Run State

Belirli worker run'ın execution bilgileri nelerdir?

Bunların storage ve lifecycle'ları farklıdır.

---

# 48. Persistence

Başlangıçta SQLite yeterli olabilir.

```text
SQLite
```

Production:

```text
PostgreSQL
```

Örnek tablolar:

```text
sessions
messages

tasks
task_dependencies
task_events

agent_runs
tool_runs

approvals

resources
resource_locks

audit_logs
```

Redis sonradan eklenebilir:

```text
Redis
 ├── task queue
 ├── realtime state
 ├── locks
 ├── pub/sub
 └── distributed scheduler state
```

---

# 49. Observability

Agent sistemi debug edilebilir olmalıdır.

Minimum takip edilmesi gerekenler:

```text
Agent run
Task tree
Tool calls
Tool arguments
Tool result
Duration
Token usage
Model used
Retries
Failures
Approvals
Permission denials
Resource waits
```

---

# 50. Tracing

Her kullanıcı isteği bir trace ID taşıyabilir.

```text
Trace #abc

Conversation Run
   ↓
Planner Run
   ↓
Task #1
   ↓
File Agent Run
   ↓
find_file()
```

Bu sayede hangi işin nerede hata verdiği bulunabilir.

---

# 51. Audit Log

Özellikle admin tool'ları için önemlidir.

Örnek:

```text
2026-09-17 14:10
user_id: 42
agent: admin_agent
tool: delete_project
project_id: 318
result: success
```

Yıkıcı işlemlerde audit log tutulmalıdır.

---

# 52. Güvenlik Prensipleri

Temel güvenlik kuralları:

1. Permission kontrolü backend'de yapılmalı.
2. LLM'e güvenilmemeli.
3. Public ve admin tool setleri ayrılmalı.
4. Yıkıcı tool'lar approval gerektirmeli.
5. Tool input schema doğrulanmalı.
6. File path erişimi sandbox veya allowlist ile sınırlandırılmalı.
7. Shell execution doğrudan açılmamalı.
8. Secrets modele verilmemeli.
9. Tool loglarında hassas değerler maskelenmeli.
10. Admin işlemleri audit edilmelidir.

---

# 53. Printer Agent Örneği

Örnek kullanıcı komutu:

```text
"Masaüstündeki raporu bul.
Azure'a gönder.
Sonra son iki sayfayı hariç tutup,
iki sayfa yan yana ve çift taraflı yazdır."
```

Planner:

```text
Task 1
Find report file
Agent: FileAgent

Task 2
Upload to Azure
Agent: CloudAgent
depends_on: Task 1

Task 3
Prepare print layout
Agent: PrinterAgent
depends_on: Task 1

Task 4
Print document
Agent: PrinterAgent
depends_on: Task 3
```

Execution:

```text
                Find File
                   │
          ┌────────┴────────┐
          ▼                 ▼
      Upload Azure      Prepare PDF
                            │
                            ▼
                          Print
```

---

# 54. Printer Tool Seti

Örnek:

```text
list_printers()
get_printer_status()
prepare_pdf()
print_document()
cancel_print_job()
get_print_queue()
```

Print tool parametreleri:

```python
print_document(
    file_path="report.pdf",
    printer="HP LaserJet",
    copies=2,
    paper_size="A4",
    orientation="landscape",
    pages_per_sheet=2,
    duplex=True,
    excluded_pages=[9, 10]
)
```

---

# 55. MCP Entegrasyonu

Bazı tool'lar doğrudan Python function olabilir.

Bazıları MCP üzerinden gelebilir.

Örnek:

```text
ToolRegistry
   │
   ├── Local Function Tools
   ├── MCP Server Tools
   ├── REST Tools
   └── Remote Service Tools
```

Böylece framework tek tool standardı üzerinden farklı kaynakları kullanabilir.

---

# 56. Tool Middleware

Tool execution öncesi ve sonrası middleware kullanılmalıdır.

Örnek sıra:

```text
Tool Request
   ↓
Schema Validation
   ↓
Permission Check
   ↓
Approval Check
   ↓
Resource Lock
   ↓
Rate Limit
   ↓
Tool Execute
   ↓
Audit
   ↓
Event Emit
```

---

# 57. Agent Tanımlarını Declarative Yapma

Agent konfigürasyonları kod veya config üzerinden tanımlanabilir.

Public:

```python
public_agent = AgentConfig(
    name="website_public",
    instructions="""
    Kaiwen web sitesinin ziyaretçi asistanısın.
    """,
    model="gpt-5.6",
    tools=[
        "projects.read",
        "services.read",
        "contact.create",
    ],
    realtime=True,
)
```

Admin:

```python
admin_agent = AgentConfig(
    name="website_admin",
    instructions="""
    Site yönetim asistanısın.
    """,
    model="gpt-5.6",
    tools=[
        "projects.*",
        "blog.*",
        "analytics.read",
        "media.*",
    ],
    realtime=True,
)
```

Desktop:

```python
desktop_agent = AgentConfig(
    name="desktop_agent",
    tools=[
        "printer.*",
        "filesystem.*",
        "system.*",
    ],
    realtime=False,
)
```

---

# 58. Framework Kullanımının Basit Olması

İyi framework'ün en önemli kriterlerinden biri dışarıdan kullanımının basit olmasıdır.

Yeni projede:

```python
from kaiwen_agent import Agent, tool

@tool
async def get_products():
    ...

agent = Agent(
    name="shop-agent",
    model="gpt-5.6",
    tools=[get_products]
)

await agent.run("Ürünleri göster")
```

Voice eklemek:

```python
agent = Agent(
    name="shop-agent",
    model="gpt-5.6",
    tools=[get_products],
    realtime=True
)
```

---

# 59. Python Package Haline Getirme

Framework ayrı repository veya package olabilir.

Development:

```bash
pip install -e ../kaiwen-agent
```

Daha sonra package:

```bash
pip install kaiwen-agent
```

veya private Git package:

```bash
pip install git+ssh://...
```

şeklinde kullanılabilir.

---

# 60. Önerilen Domain Sınıfları

Minimum çekirdek sınıflar:

```text
Agent
AgentConfig
AgentRegistry

AgentRun
RunContext

Tool
ToolRegistry
ToolMetadata

AgentTask
TaskManager
TaskGraph
TaskScheduler

ResourceManager

AgentContext
Session

EventBus
Event

ModelProvider
ModelRouter

PermissionManager
ApprovalManager

RunMailbox
ControlMessage
```

---

# 61. Örnek Agent Interface

```python
class Agent:

    def __init__(
        self,
        name,
        model,
        tools,
        instructions=None
    ):
        self.name = name
        self.model = model
        self.tools = tools
        self.instructions = instructions

    async def run(
        self,
        input,
        context
    ):
        ...
```

---

# 62. Örnek AgentRun

```python
class AgentRun:

    id: str
    agent_id: str
    task_id: str

    status: str

    context: AgentContext

    started_at: datetime
    completed_at: datetime | None

    result: dict | None
    error: str | None
```

---

# 63. Örnek TaskGraph

```python
class TaskGraph:

    def add_task(self, task):
        ...

    def add_dependency(
        self,
        task_id,
        depends_on
    ):
        ...

    def ready_tasks(self):
        ...
```

---

# 64. Örnek Scheduler

```python
class TaskScheduler:

    async def tick(self):

        ready = self.graph.ready_tasks()

        for task in ready:

            if not self.resources.available(task):
                task.status = WAITING_RESOURCE
                continue

            await self.queue.enqueue(task)
```

---

# 65. Örnek Event

```python
Event(
    name="task.progress",
    timestamp=...,
    payload={
        "task_id": "task-10",
        "progress": 0.72
    }
)
```

---

# 66. Örnek Run Control Mesajı

```python
ControlMessage(
    type="instruction",
    content="Ignore node_modules",
    created_by="user"
)
```

veya:

```python
ControlMessage(
    type="cancel"
)
```

---

# 67. Complete High-Level Architecture

```text
                                 USER
                                  │
                   ┌──────────────┴──────────────┐
                   │                             │
                  TEXT                         VOICE
                   │                         Live API
                   └──────────────┬──────────────┘
                                  ▼
                         Conversation Agent
                                  │
                                  ▼
                            Orchestrator
                                  │
               ┌──────────────────┼──────────────────┐
               │                  │                  │
               ▼                  ▼                  ▼
            Planner          Task Manager       Agent Registry
               │                  │                  │
               │             Task Graph             │
               │                  │                  │
               └──────────────────┼──────────────────┘
                                  ▼
                              Scheduler
                                  │
          ┌───────────────────────┼───────────────────────┐
          ▼                       ▼                       ▼
     File Agent              Cloud Agent             Printer Agent
          │                       │                       │
      File Tools               API Tools               Print Tools
          │                       │                       │
          └───────────────────────┼───────────────────────┘
                                  │
                                  ▼
                              Event Bus
                                  │
                  ┌───────────────┼───────────────┐
                  ▼               ▼               ▼
               Status           Logs          WebSocket
                  │                               │
                  └───────────────┬───────────────┘
                                  ▼
                              Frontend UI
```

Kontrol kanalı:

```text
                            USER
                             │
                  "Yazdırmayı durdur"
                             │
                             ▼
                    Conversation Agent
                             │
                             ▼
                       Task Manager
                             │
                             ▼
                    Run Control Bus
                             │
                             ▼
                       Printer Run
                             │
                           CANCEL
```

---

# 68. Ana Tasarım Kuralları

Bu framework geliştirilirken aşağıdaki prensiplere sadık kalınmalıdır.

## 1. Agent != Task

Agent yetenektir, task iştir.

## 2. Agent != AgentRun

Agent tanımdır, run execution instance'ıdır.

## 3. Realtime != Agent Core

Realtime yalnızca optional interface olmalıdır.

## 4. State LLM Prompt'unda Tutulmamalı

Gerçek task state backend'de tutulmalıdır.

## 5. Permission Prompt'a Bırakılmamalı

Backend authorization zorunludur.

## 6. Tool Sistemi Plugin Mantığında Olmalı

Yeni tool framework çekirdeğini değiştirmeden eklenebilmelidir.

## 7. Task Bağımlılıkları DAG ile Yönetilmeli

LLM'in “sıra hatırlamasına” güvenilmemelidir.

## 8. Uzun İşler Worker Olarak Çalışmalı

Conversation responsive kalmalıdır.

## 9. Worker'a Müdahale Edilebilmeli

Pause, cancel, instruction ve approval desteklenmelidir.

## 10. Event Sistemi İlk Günden Olmalı

Takip edilebilirlik sonradan eklenmemelidir.

---

# 69. Minimum Viable Version

İlk versiyonda her şeyi yapmak yerine aşağıdaki sıra uygulanabilir.

## Faz 1 — Core

- Agent
- AgentRun
- AgentContext
- Tool
- ToolRegistry
- ModelProvider

## Faz 2 — Task System

- AgentTask
- TaskStatus
- TaskManager
- TaskGraph
- Scheduler

## Faz 3 — Specialized Agents

- FileAgent
- WebsiteAgent
- PrinterAgent

## Faz 4 — Permissions

- PermissionManager
- Public/Admin separation
- Approval system

## Faz 5 — Realtime

- Text adapter
- Live API adapter
- Shared Conversation Agent

## Faz 6 — Event Tracking

- EventBus
- WebSocket
- Task UI
- Logs

## Faz 7 — Advanced Execution

- Parallel workers
- Resource locks
- Retry
- Dynamic replanning
- Mailbox
- Pause / resume / cancel

## Faz 8 — Reusability

- Package
- Config API
- Documentation
- Examples
- Plugin/MCP support

---

# 70. Önerilen İlk Gerçek Kullanım

Kaiwen web sitesi bu framework'ün ilk production kullanım alanı olabilir.

Public taraf:

```text
PublicAgent

Tools:
- get_projects
- get_project_detail
- get_services
- create_contact_request
```

Admin taraf:

```text
AdminAgent

Tools:
- create_project
- update_project
- delete_project
- upload_media
- publish_project
- unpublish_project
- read_analytics
```

Admin tarafında hem:

```text
Text
```

hem de:

```text
Live Voice
```

aynı agent runtime'a bağlanabilir.

---

# 71. Sonraki Kullanım Alanları

Aynı framework daha sonra:

```text
Desktop Assistant
Printer Assistant
Local Voice Assistant
Development Agent
Company Internal Agent
IoT Assistant
Server Management Agent
Customer Support Agent
Personal Automation Agent
```

projelerinde kullanılabilir.

Yalnızca:

```text
Agent config
Tools
Permissions
Model config
```

değişir.

---

# 72. Sonuç

Kurulmak istenen sistemin en doğru tanımı:

> Yeniden kullanılabilir, çok ajanlı, tool tabanlı, task graph destekli, gerçek zamanlı veya yazılı olarak kullanılabilen, event-driven ve izin kontrollü bir agent runtime/framework.

Bu yapının merkezinde yalnızca bir LLM bulunmaz.

Sistem şu parçalardan oluşur:

```text
Conversation
Orchestration
Planning
Task Management
Scheduling
Agents
Tools
Permissions
Resources
Events
Persistence
Observability
Realtime
```

Özellikle şu dört ayrımın doğru yapılması uzun vadede mimarinin sağlam kalmasını sağlar:

```text
Agent
AgentRun
Task
Tool
```

Bunlar birbirine karıştırılmamalıdır.

En kritik omurga:

```text
Agent Registry
Tool Registry
Planner
Task DAG
Scheduler
Agent Runs
Event Bus
Permission System
Resource Manager
Run Control Channel
```

olmalıdır.

Bu yapı doğru kurulduğunda sistem yalnızca “soruyu cevaplayan bir AI” olmaktan çıkar.

Şuna dönüşür:

> Kullanıcıyla konuşmaya devam ederken, birden fazla uzman ajanı koordine eden, işleri sıraya koyan, bağımlılıkları takip eden, paralel görevleri çalıştıran, gerektiğinde kullanıcıdan onay alan, çalışan görevlere müdahale edilebilen ve farklı uygulamalara gömülebilen genel amaçlı bir agent platformu.

---

# 73. Memory Architecture

Bu framework'te hafıza tek bir alan olarak ele alınmamalıdır.

Farklı hafıza türleri farklı amaçlara hizmet eder ve farklı yaşam döngülerine sahiptir.

Önerilen yapı:

```text
Memory System
│
├── Working Memory
├── Short-Term Memory
├── Long-Term Memory
├── Semantic Memory
└── Memory Policy
```

Bu ayrımın amacı şudur:

- Çalışan bir agent run'ın geçici state'i ile
- O anki konuşmanın context'i ile
- Kullanıcının kalıcı tercihleri ile
- Geçmiş bilgilerin semantik retrieval sistemi

birbirine karıştırılmamalıdır.

---

# 74. Working Memory

Working Memory yalnızca aktif run veya aktif reasoning süreci sırasında kullanılan geçici hafızadır.

Örnek içerikler:

```text
Şu anda hangi task çalışıyor?
Hangi tool sonucu geldi?
Planner hangi adımı seçti?
Bir sonraki dependency nedir?
Hangi resource bekleniyor?
```

Örnek:

```python
WorkingMemory(
    run_id="run-123",
    values={
        "current_file": "report.pdf",
        "selected_printer": "HP-LaserJet-01",
        "current_step": "prepare_pdf"
    }
)
```

Bu bilgi task veya run tamamlandığında büyük ölçüde silinebilir.

Working Memory kalıcı kullanıcı hafızası değildir.

---

# 75. Short-Term Memory

Short-Term Memory session veya yakın konuşma geçmişini temsil eder.

Örnek:

```text
Kullanıcı biraz önce hangi projeden bahsetti?
"Onu sil" derken hangi nesneyi kastediyor?
Az önce hangi printer seçildi?
Bu konuşmada hangi parametreler konuşuldu?
```

Örnek:

```python
ShortTermMemory(
    session_id="session-abc",
    messages=[...],
    temporary_facts={...}
)
```

Short-Term Memory genellikle:

```text
session süresi
+
yakın konuşma geçmişi
```

ile sınırlıdır.

---

# 76. Long-Term Memory

Long-Term Memory kullanıcının veya sistemin daha uzun vadede hatırlaması gereken bilgileri içerir.

Örnekler:

```text
Varsayılan yazıcı tercihi
Tercih edilen dil
Sık kullanılan proje
Varsayılan çıktı formatı
Kullanıcının belirli bir workflow tercihi
```

Long-Term Memory her konuşulan şeyi kaydetmemelidir.

Kontrollü yazma politikası uygulanmalıdır.

Örnek:

```python
LongTermMemoryItem(
    key="default_printer",
    value="HP-LaserJet-01",
    source="user_explicit_preference",
    confidence=1.0
)
```

---

# 77. Semantic Memory

Semantic Memory geçmiş bilgilerin embedding / retrieval yöntemiyle aranmasını sağlar.

Örnek kullanıcı isteği:

```text
"Geçen ay yazıcı ayarlarını nasıl yapmıştık?"
```

Agent:

```python
results = await memory.search(
    query="printer configuration"
)
```

ile geçmiş ilgili kayıtları getirebilir.

Bu katman için başlangıçta:

```text
PostgreSQL + pgvector
```

yeterlidir.

Daha ileri sistemlerde ayrı vector database kullanılabilir.

---

# 78. MemoryManager

Memory katmanlarının tek bir ortak interface üzerinden erişilmesi önerilir.

```python
class MemoryManager:

    async def remember(self, ...):
        ...

    async def recall(self, ...):
        ...

    async def search(self, ...):
        ...

    async def forget(self, ...):
        ...

    async def summarize_session(self, ...):
        ...
```

AgentContext:

```python
AgentContext(
    user=user,
    session=session,
    memory=memory_manager,
    permissions=permissions,
    metadata=metadata
)
```

şeklinde memory sistemine erişebilir.

---

# 79. Memory ile Task State Ayrımı

Bu ayrım kritik önemdedir.

Yanlış:

```text
"Print task %62 tamamlandı"
```

bilgisini long-term memory'ye yazmak.

Doğru:

```text
Task Store
→ print task %62
```

Long-term memory için uygun örnek:

```text
"Kullanıcı varsayılan olarak çift taraflı baskı tercih ediyor."
```

Özet:

```text
Task State
= sistem şu anda ne yapıyor?

Memory
= sistem geçmişten ne hatırlıyor?
```

---

# 80. Memory Policy

Memory sisteminde retention ve privacy policy bulunmalıdır.

Örnek klasör:

```text
memory/
├── working/
│   └── run_memory.py
│
├── short_term/
│   └── session_memory.py
│
├── long_term/
│   └── user_memory.py
│
├── semantic/
│   ├── embeddings.py
│   └── retrieval.py
│
├── policy/
│   ├── retention.py
│   ├── privacy.py
│   └── write_rules.py
│
└── manager.py
```

Memory'ye yazılabilecek bilgi türleri kurallarla kontrol edilmelidir.

---

# 81. UI Agent Neden Ayrı Olmalı?

Backend agent ile UI agent'ın sorumlulukları farklıdır.

Backend Agent:

```text
Karar verir
Plan yapar
Tool seçer
Yetki kontrol eder
Server tool çalıştırır
Memory kullanır
Task üretir
```

UI Agent:

```text
Tarayıcıdaki güvenli UI aksiyonlarını yürütür
React state ile etkileşir
Route değiştirir
Modal açar
Form alanını doldurur
UI sonucu backend'e raporlar
```

Bu nedenle UI tarafı ayrı bir runtime olarak tasarlanmalıdır.

Ancak kritik karar şudur:

> UI Agent kendi başına bağımsız karar veren bir ana LLM olmamalıdır.

Backend karar verir.

UI Agent güvenli bir client executor gibi davranır.

---

# 82. Backend Agent + UI Agent Modeli

Önerilen mimari:

```text
                    USER
                     │
                     ▼
              Conversation Agent
                     │
                     ▼
                 Backend
                 Orchestrator
                     │
          ┌──────────┴───────────┐
          ▼                      ▼
    Server Tool               UI Action
          │                      │
          ▼                      ▼
      Database               UI Dispatcher
                                 │
                                 ▼
                             WebSocket
                                 │
                                 ▼
                           Browser UI Agent
                                 │
                                 ▼
                               React
```

Backend her zaman authority olmalıdır.

---

# 83. UI Agent Ayrı Bir Proje / Package Olmalı mı?

Evet.

UI Agent tarafını ayrı bir reusable package yapmak en doğru yaklaşımdır.

Önerilen yapı:

```text
kaiwen-agent-core
```

Backend agent framework.

ve:

```text
kaiwen-ui-agent
```

Frontend UI runtime.

Böylece farklı React projelerinde aynı UI agent package kullanılabilir.

Örneğin:

```text
Project A
└── kaiwen-ui-agent

Project B
└── kaiwen-ui-agent

Admin Panel
└── kaiwen-ui-agent
```

---

# 84. UI Agent İçin TypeScript Kullanımı

UI Agent tarafında ana dil olarak **TypeScript** önerilir.

Sebep:

- React projelerinde doğal uyum sağlar.
- Action payload'ları type-safe olur.
- Tool schema'ları compile-time kontrol edilebilir.
- Büyük projelerde refactor güvenliği sağlar.
- Backend ile paylaşılan schema'ları güvenli tutar.
- JavaScript projeleri de derlenmiş package'i kullanabilir.

Dolayısıyla package:

```text
TypeScript ile yazılır
```

ama build sonrası:

```text
JavaScript olarak dağıtılır
+
Type definitions yayınlanır
```

Böylece hem TypeScript hem JavaScript projelerinde kullanılabilir.

Örnek package çıktısı:

```text
dist/
├── index.js
├── index.d.ts
├── runtime.js
├── actions.js
└── adapters/
```

---

# 85. UI Agent Package Yapısı

Önerilen proje:

```text
kaiwen-ui-agent/
│
├── src/
│   ├── core/
│   │   ├── runtime.ts
│   │   ├── context.ts
│   │   ├── events.ts
│   │   └── types.ts
│   │
│   ├── actions/
│   │   ├── registry.ts
│   │   ├── base.ts
│   │   └── validation.ts
│   │
│   ├── transport/
│   │   ├── websocket.ts
│   │   ├── http.ts
│   │   └── reconnect.ts
│   │
│   ├── react/
│   │   ├── provider.tsx
│   │   ├── hooks.ts
│   │   └── context.tsx
│   │
│   ├── security/
│   │   ├── allowlist.ts
│   │   └── guards.ts
│   │
│   └── adapters/
│       ├── react-router.ts
│       └── generic.ts
│
├── package.json
├── tsconfig.json
└── README.md
```

---

# 86. UI Agent Runtime

UI runtime backend'den action alır.

Örnek:

```json
{
  "type": "client_action",
  "id": "action-82",
  "name": "open_project",
  "args": {
    "project_id": 123
  }
}
```

UI Runtime:

```ts
await runtime.execute(message)
```

der.

Registry:

```ts
const action = registry.get(message.name)
```

ile ilgili action handler'ı bulur.

---

# 87. UI Action Registry

Örnek:

```ts
const registry = new UIActionRegistry();

registry.register("navigate", navigateAction);
registry.register("open_project", openProjectAction);
registry.register("open_modal", openModalAction);
registry.register("set_form_value", setFormValueAction);
```

Backend arbitrary JavaScript göndermez.

Yalnızca önceden tanımlı semantic action isimlerini çağırır.

Bu güvenlik için kritiktir.

---

# 88. Semantic UI Actions

Raw DOM kontrolü yerine semantic action kullanmak daha doğrudur.

Tercih edilmeyen yaklaşım:

```text
click("#button-23")
scroll(412)
document.querySelector(...)
```

Tercih edilen yaklaşım:

```text
open_project(project_id)
open_contact_form()
go_to_admin_projects()
select_project_category(category_id)
show_project_preview(project_id)
```

Bu sayede React DOM yapısı değişse bile agent API sabit kalabilir.

---

# 89. Navigation Action

Örnek backend çağrısı:

```json
{
  "type": "client_action",
  "name": "navigate",
  "args": {
    "path": "/projects"
  }
}
```

React:

```ts
registry.register("navigate", async ({ path }) => {
  router.navigate(path);
});
```

Ancak route allowlist ile kontrol edilmelidir.

```ts
if (!allowedRoutes.includes(path)) {
  throw new Error("Route not allowed");
}
```

---

# 90. React Router Adapter

UI agent doğrudan React Router'a hard-code edilmemelidir.

Adapter kullanılmalıdır.

```ts
interface NavigationAdapter {
  navigate(path: string): Promise<void> | void;
}
```

React Router implementation:

```ts
class ReactRouterAdapter implements NavigationAdapter {
  constructor(private navigateFn: NavigateFunction) {}

  navigate(path: string) {
    this.navigateFn(path);
  }
}
```

Böylece başka router sistemleri de desteklenebilir.

---

# 91. Form Interaction

UI agent form state'e semantic şekilde erişmelidir.

Örnek:

```json
{
  "name": "set_project_title",
  "args": {
    "value": "Yeni Proje"
  }
}
```

React:

```ts
registry.register(
  "set_project_title",
  async ({ value }) => {
    projectFormStore.setTitle(value);
  }
);
```

Raw DOM input manipülasyonu yerine store veya controlled component API tercih edilmelidir.

---

# 92. Modal ve Component Action'ları

Örnek:

```text
open_project_create_modal()
close_project_create_modal()
show_delete_confirmation(project_id)
show_notification(message)
```

Bunlar UI agent'ın güvenli ve kontrollü action'larıdır.

---

# 93. UI Action Result

Frontend action tamamlandıktan sonra backend'e sonuç dönmelidir.

Örnek:

```json
{
  "type": "client_action_result",
  "id": "action-82",
  "status": "completed",
  "result": {
    "route": "/projects/123"
  }
}
```

Failure:

```json
{
  "type": "client_action_result",
  "id": "action-82",
  "status": "failed",
  "error": "Project not found"
}
```

Backend bunu Task Manager'a işler.

---

# 94. UI Action da Task Olabilir

UI action'lar backend task modeline dahil edilebilir.

Örnek:

```text
Task #100
Name: Open project page
Executor: UI Agent
Status: completed
```

Bu sayede task graph içerisinde:

```text
Create Project
    ↓
Save Project
    ↓
Open Project Detail UI
```

gibi backend + frontend karma workflow'lar kurulabilir.

---

# 95. UI Agent ve Task DAG

Örnek kullanıcı komutu:

```text
"Yeni projeyi oluştur ve sonra proje detay sayfasını aç."
```

Planner:

```text
Task 1:
Create project
Executor: Backend Agent

Task 2:
Open project detail
Executor: UI Agent
depends_on: Task 1
```

Akış:

```text
Create Project
      │
      ▼
Return project_id
      │
      ▼
UI Task
      │
      ▼
open_project(project_id)
```

---

# 96. UI Agent ile Backend Agent Arasındaki Protokol

Önerilen ortak message türleri:

```text
client.connected
client.capabilities
client_action
client_action_result
client_action_error
client_state
client_event
client.disconnected
```

Örnek capability mesajı:

```json
{
  "type": "client.capabilities",
  "actions": [
    "navigate",
    "open_project",
    "open_modal",
    "set_project_title"
  ]
}
```

Backend hangi action'ların mevcut olduğunu bilir.

---

# 97. Capability Discovery

Her frontend aynı action'lara sahip olmayabilir.

Bu nedenle UI runtime bağlandığında capabilities göndermelidir.

Örnek:

```text
Website A
actions:
- navigate
- open_project
- contact_form

Admin Panel
actions:
- navigate
- open_project
- edit_project
- publish_project
```

Backend mevcut client capabilities'e göre UI task üretebilir.

---

# 98. Client Context

UI agent backend'e mevcut UI state'in özetini gönderebilir.

Örnek:

```json
{
  "type": "client_state",
  "route": "/admin/projects",
  "active_modal": null,
  "selected_project_id": 42
}
```

Bu state agent'ın konuşma context'ine gerektiğinde eklenebilir.

Ancak tüm DOM ağacı backend'e gönderilmemelidir.

Semantic UI state tercih edilmelidir.

---

# 99. UI Agent Güvenliği

Frontend agent'ın arbitrary code çalıştırmasına izin verilmemelidir.

Kesinlikle kaçınılması gerekenler:

```text
eval()
new Function()
arbitrary JavaScript execution
raw shell access
unrestricted DOM scripting
```

Backend yalnızca allowlist'teki action isimlerini çağırabilmelidir.

Frontend de backend'den gelen action'ı doğrulamalıdır.

---

# 100. UI Permission Katmanı

Backend permission kontrolüne ek olarak frontend de client-side guard uygulayabilir.

Örnek:

```ts
registry.register(
  "open_admin_panel",
  requireRole("admin", async () => {
    router.navigate("/admin");
  })
);
```

Ancak güvenliğin ana authority'si backend olmalıdır.

Frontend kontrolü ikinci savunma katmanıdır.

---

# 101. UI Agent ile Public/Admin Ayrımı

Public frontend:

```text
Allowed UI Actions:
- navigate_public
- open_project
- open_contact_form
```

Admin frontend:

```text
Allowed UI Actions:
- navigate_admin
- open_project_editor
- open_publish_dialog
- open_media_manager
```

Aynı UI Agent package kullanılır.

Sadece registry ve capability seti farklıdır.

---

# 102. React Integration API

Package kullanımının kolay olması için React Provider sağlanabilir.

Örnek:

```tsx
<UIAgentProvider
  transport={transport}
  actions={actions}
>
  <App />
</UIAgentProvider>
```

Hook:

```ts
const uiAgent = useUIAgent();
```

Action register:

```ts
uiAgent.registerAction(
  "open_project",
  async ({ projectId }) => {
    navigate(`/projects/${projectId}`);
  }
);
```

---

# 103. UI Agent Transport

İlk tercih WebSocket olabilir.

Sebep:

- Backend anlık action gönderebilir.
- Frontend anlık result dönebilir.
- Task progress event'leri taşınabilir.
- Live API session ile aynı backend event sistemi kullanılabilir.

Mimari:

```text
React
  │
UI Agent Runtime
  │
WebSocket
  │
Backend UI Dispatcher
  │
Task Manager
```

Fallback olarak HTTP de desteklenebilir.

---

# 104. UI Dispatcher

Backend tarafında client action'ları yöneten ayrı bir servis önerilir.

```python
class UIActionDispatcher:

    async def dispatch(
        self,
        client_id,
        action,
        args
    ):
        ...
```

Görevleri:

- İlgili client session'ı bulmak.
- Capability kontrolü yapmak.
- Action mesajı göndermek.
- Timeout yönetmek.
- Result beklemek.
- Task status güncellemek.

---

# 105. Client Session

Her açık frontend ayrı bir client session olabilir.

Örnek:

```text
user_id: 42

client-session-1
→ laptop browser

client-session-2
→ phone browser
```

Backend hangi UI'ya action göndereceğini bilmelidir.

Varsayılan:

```text
active client
```

veya:

```text
conversation'ın bağlı olduğu client
```

kullanılabilir.

---

# 106. Birden Fazla UI Client

İleride kullanıcı aynı anda:

```text
Laptop
Telefon
Tablet
```

üzerinden bağlı olabilir.

UI task şu bilgiyi taşıyabilir:

```python
UITask(
    target_client_id="client-123",
    action="open_project"
)
```

Bu yüzden client_id baştan tasarıma eklenmelidir.

---

# 107. UI Agent'ın Kendi LLM'i Olmalı mı?

İlk versiyonda hayır.

Backend LLM:

```text
Ne yapılacağına karar verir.
```

UI Agent:

```text
Nasıl uygulanacağını deterministic action handler üzerinden yürütür.
```

Bu daha güvenli ve öngörülebilirdir.

İleride çok karmaşık UI otomasyonu gerekirse ayrı bir UI reasoning agent eklenebilir.

Ancak o da doğrudan DOM'a sınırsız erişmemelidir.

---

# 108. UI Agent İçin Önerilen İsimlendirme

Backend package:

```text
kaiwen-agent
```

Frontend package:

```text
@kaiwen/ui-agent
```

veya:

```text
@imthekaiwen/agent-ui
```

olabilir.

Monorepo tercih edilirse:

```text
kaiwen-agent-platform/
│
├── packages/
│   ├── agent-core/
│   ├── ui-agent/
│   ├── protocol/
│   └── schemas/
│
├── apps/
│   ├── website/
│   └── admin/
│
└── examples/
```

---

# 109. Shared Protocol Package

Backend Python, frontend TypeScript olacağı için ortak schema konusu önemlidir.

Önerilen ayrı package / schema alanı:

```text
protocol/
├── client_action.schema.json
├── client_action_result.schema.json
├── task_event.schema.json
└── client_state.schema.json
```

Backend:

```text
Pydantic
```

Frontend:

```text
TypeScript types
```

aynı JSON Schema'dan üretilebilir.

Bu sayede protokol drift'i azaltılır.

---

# 110. Backend + Frontend Type Safety

Örneğin ortak action:

```json
{
  "name": "open_project",
  "args": {
    "project_id": 123
  }
}
```

Backend Pydantic:

```python
class OpenProjectArgs(BaseModel):
    project_id: int
```

Frontend TypeScript:

```ts
type OpenProjectArgs = {
  project_id: number;
};
```

Mümkünse bunlar aynı schema kaynağından generate edilmelidir.

---

# 111. UI Agent Development Roadmap

## Faz 1

- TypeScript package
- WebSocket transport
- Action registry
- navigate
- open_modal
- close_modal
- client_action_result

## Faz 2

- React Provider
- React hooks
- Router adapter
- Form actions
- Capability discovery

## Faz 3

- Client state
- Task integration
- UI action timeout
- Retry
- Error handling

## Faz 4

- Multiple clients
- Permission guards
- Audit
- Telemetry

## Faz 5

- UI Agent SDK documentation
- Example React app
- Admin panel integration
- Public site integration

---

# 112. Güncellenmiş Genel Mimari

```text
                                  USER
                                   │
                     ┌─────────────┴─────────────┐
                     │                           │
                    TEXT                       VOICE
                     │                        Live API
                     └─────────────┬─────────────┘
                                   ▼
                          Conversation Agent
                                   │
                                   ▼
                              Orchestrator
                                   │
             ┌─────────────────────┼─────────────────────┐
             │                     │                     │
             ▼                     ▼                     ▼
          Planner               Memory              Task Manager
             │          ┌──────────┼──────────┐          │
             │          ▼          ▼          ▼          │
             │      Working     Short      Long-Term     │
             │                            + Semantic      │
             │                     │                     │
             └─────────────────────┼─────────────────────┘
                                   ▼
                               Scheduler
                                   │
                 ┌─────────────────┼─────────────────┐
                 ▼                 ▼                 ▼
            File Agent       Website Agent      Printer Agent
                 │                 │                 │
                 ▼                 ▼                 ▼
            Server Tools      Server Tools       Print Tools
                                   │
                                   │
                            UI Action Request
                                   │
                                   ▼
                             UI Dispatcher
                                   │
                              WebSocket
                                   │
                                   ▼
                        @kaiwen/ui-agent
                                   │
                     ┌─────────────┼─────────────┐
                     ▼             ▼             ▼
                  Router         Modals        UI State
                     │             │             │
                     └─────────────┼─────────────┘
                                   ▼
                                 React
```

---

# 113. Güncellenmiş Repository Stratejisi

Önerilen ilk yapı:

```text
kaiwen-agent-platform/
│
├── backend/
│   └── kaiwen_agent/
│       ├── agents/
│       ├── tools/
│       ├── tasks/
│       ├── memory/
│       ├── models/
│       ├── security/
│       ├── resources/
│       └── integrations/
│
├── packages/
│   ├── ui-agent/
│   │   ├── src/
│   │   └── package.json
│   │
│   └── protocol/
│       ├── schemas/
│       └── generated/
│
├── examples/
│   ├── react-basic/
│   ├── react-admin/
│   └── python-agent/
│
└── docs/
```

İleride bunlar ayrı repository'lere bölünebilir.

İlk geliştirme sırasında monorepo daha pratiktir.

---

# 114. Nihai Tasarım Kararı

Bu sistemde iki ayrı runtime bulunmalıdır:

```text
Backend Agent Runtime
```

ve:

```text
Frontend UI Agent Runtime
```

Backend Agent Runtime:

```text
zeka
planning
memory
permissions
tasks
tools
server actions
```

üzerinden sorumludur.

UI Agent Runtime:

```text
browser-side semantic actions
React integration
client capability
client state
action result
```

üzerinden sorumludur.

Aralarındaki ilişki:

```text
Backend decides
UI executes
UI reports
Backend updates task state
```

şeklinde olmalıdır.

Bu ayrım hem güvenlik hem yeniden kullanılabilirlik hem de uzun vadeli bakım açısından en temiz mimaridir.
