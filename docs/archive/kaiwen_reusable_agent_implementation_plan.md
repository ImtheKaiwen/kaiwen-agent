# Kaiwen Reusable Agent Platform — Uygulama ve Entegrasyon Planı

Bu plan, `kaiwen_reusable_agent_framework_architecture.md` içindeki mimariyi uygulanabilir geliştirme sırasına dönüştürür. Portfolio roadmap’indeki Faz 14–16 bu planla değiştirilmiştir. Portfolio projesi agent framework’ünün sahibi olmayacak; framework’ün ilk gerçek entegrasyonu olacaktır.

## 1. Karar Özeti

Üç bağımsız dağıtım birimi oluşturulacaktır:

```text
kaiwen-agent-platform (ayrı repository)
├── backend/                 Python agent runtime
│   └── kaiwen_agent/
├── packages/
│   ├── protocol/            Canonical JSON Schema + generated TypeScript types
│   └── ui-agent/            Headless UI runtime + React entegrasyonu + Dynamic Island
├── examples/
└── docs/

kaiwen-portfolio (bu repository)
├── apps/api                 Domain agent tanımları ve site tool adapter’ları
└── apps/web                 UI action kayıtları ve marka teması
```

Önerilen yayın adları:

- PyPI dağıtımı: `kaiwen-agent`, Python import adı: `kaiwen_agent`
- npm UI paketi: `@imthekaiwen/agent-ui`
- npm protokol paketi: `@imthekaiwen/agent-protocol`

Paket adlarının registry uygunluğu yayın öncesinde ayrıca doğrulanacaktır.

## 2. Sorumluluk Sınırları

### `kaiwen-agent` içine girecekler

- `Agent`, `AgentConfig`, `AgentRun`, `AgentInput`, `AgentResult`
- `ModelProvider` protokolü ve provider router
- Tool tanımı, registry, schema üretimi ve execution pipeline
- Permission, approval, idempotency ve policy mekanizması
- Event bus ve standart event tipleri
- Session, memory ve persistence portları
- Task, DAG, scheduler, retry, mailbox ve resource lock
- OpenAI text adapter’ı ve opsiyonel Live/Realtime adapter’ı
- FastAPI, WebSocket/SSE, CLI ve MCP adapter’ları
- Framework’e ait tracing, metrics ve audit portları

### Portfolio API içinde kalacaklar

- `read_projects`, `get_project`, `search_projects`
- `get_services`, `create_lead`, `build_quote_draft`
- İleride admin için create/update/archive/publish project tool’ları
- SQLAlchemy modelleri ve Kaiwen repository/service katmanları
- Public ve admin agent konfigürasyonları/promptları
- Siteye özel artifact dönüştürme ve route’lar
- Kimlik doğrulama ve gerçek kullanıcı/rol çözümleme

### `@imthekaiwen/agent-ui` içine girecekler

- UI action registry ve schema validation
- HTTP, SSE ve WebSocket transport’ları
- Reconnect, timeout, cancellation ve action result akışı
- Capability discovery ve semantic client state
- React Provider ve hook’lar
- Navigation/form/modal adapter interface’leri
- Headless Dynamic Island state machine
- Opsiyonel, tema edilebilir React Dynamic Island bileşeni

### Portfolio web içinde kalacaklar

- Kaiwen renkleri, logo, metinler ve görsel tema
- Siteye özel semantic UI action handler’ları
- Next.js router adapter’ı
- Public ve admin action allowlist’leri
- Ürün kartının siteye özel görünümü

## 3. Temel Teknik Kararlar

1. Python core tamamen `async` tasarlanacak. Sync framework kodu çekirdeğe taşınmayacak.
2. Core; FastAPI, SQLAlchemy, PostgreSQL veya OpenAI sınıflarına doğrudan bağımlı olmayacak.
3. Storage ve transport entegrasyonları port/adapter olacak.
4. Canonical istemci protokolü version’lı JSON Schema olacaktır.
5. Backend karar authority’sidir. UI runtime LLM çalıştırmaz; yalnızca allowlist’teki semantic action’ları yürütür.
6. Public ve admin agent yalnızca farklı promptlar değil, farklı permission/tool setleri kullanır.
7. Yazma ve dış etki oluşturan tool’larda idempotency anahtarı zorunludur.
8. Publish, delete, deploy, telefon araması ve dış mesaj gönderme ilk sürümlerde approval gerektirir.
9. Realtime/Live katmanı optional transport’tur; agent core değildir.
10. Dynamic Island bir sohbet geçmişi sayfası değil, event-driven agent yüzeyidir.
11. OpenAI Agents SDK; OpenAI adapter’ında kısa ömürlü handoff/tracing için değerlendirilecek, fakat kalıcı task DAG, permission ve scheduler state’inin sahibi olmayacaktır. Bu state provider-neutral core ve uygulama store’larında kalacaktır.

## 4. Ortak Protokol

Her mesaj şu envelope ile taşınacaktır:

```json
{
  "schema_version": "1.0",
  "id": "evt_...",
  "type": "run.status",
  "timestamp": "2026-09-17T20:00:00Z",
  "trace_id": "trace_...",
  "session_id": "session_...",
  "payload": {}
}
```

İlk protokol tipleri:

```text
conversation.input
conversation.delta
conversation.completed
run.started
run.status
run.completed
run.failed
tool.started
tool.completed
tool.failed
approval.requested
approval.resolved
artifact.created
client.connected
client.capabilities
client.state
client_action.requested
client_action.result
client_action.failed
```

Artifact tipleri ilk günden generic olacaktır:

```text
project_card
task_progress
approval_card
form_draft
notification
link
```

Bu sayede Dynamic Island yalnızca düz metin değil, ürün kartı, task ilerlemesi, onay sorusu ve taslak form gösterebilir.

## 5. Python API Taslağı

Hedeflenen ilk kullanıcı API’si:

```python
from kaiwen_agent import Agent, AgentContext, tool
from kaiwen_agent.models.openai import OpenAIResponsesProvider

@tool(
    name="projects.read",
    permissions={"projects.read"},
    side_effect="none",
)
async def read_projects(ctx: AgentContext, category: str | None = None):
    return await ctx.services.projects.list(category=category)

agent = Agent(
    name="portfolio-public",
    model=OpenAIResponsesProvider(model="gpt-5.6-luna"),
    tools=[read_projects],
    permissions={"projects.read"},
)

result = await agent.run(input, context=context)
```

Tool metadata minimum alanları:

```text
name
description
input_schema
output_schema
permissions
side_effect: none | reversible | destructive
requires_approval
idempotent
timeout
retry_policy
resource_keys
```

Tool execution sırası:

```text
Schema validation
→ Permission check
→ Approval check
→ Idempotency check
→ Resource lock
→ Rate limit
→ Execute
→ Audit redaction
→ Event emit
```

## 6. UI Package API Taslağı

```tsx
import {
  AgentUIProvider,
  DynamicIsland,
  createActionRegistry,
} from "@imthekaiwen/agent-ui/react";

const actions = createActionRegistry()
  .register("navigate_public", navigatePublicSchema, navigatePublic)
  .register("open_project", openProjectSchema, openProject);

<AgentUIProvider transport={transport} actions={actions}>
  <DynamicIsland theme={kaiwenTheme} renderArtifact={renderArtifact} />
</AgentUIProvider>
```

Dynamic Island state machine:

```text
idle
→ listening | composing
→ thinking
→ tool_activity
→ waiting_approval | waiting_user
→ result
→ compact
→ idle
```

Görsel bileşen runtime’dan ayrılacaktır. Başka bir proje isterse kendi UI’ını yazıp yalnızca headless hook’ları kullanabilir.

## 7. Geliştirme Fazları

### Faz A — Ayrı repository ve sözleşmeler

- `kaiwen-agent-platform` repository’sini oluştur.
- Python, npm ve protocol workspace’lerini hazırla.
- JSON Schema envelope ve ilk event/action tiplerini yaz.
- Schema’dan TypeScript tip üretimini kur.
- Python Pydantic modellerinin aynı contract testlerini geçmesini sağla.
- CI: lint, type check, unit test, package build.

Çıkış kriteri: Python ve TypeScript aynı fixture mesajlarını kabul/reddeder.

### Faz B — Python Core 0.1

- `AgentInput`, `AgentContext`, `AgentConfig`, `AgentRun`, `AgentResult`
- `Tool`, decorator ve `ToolRegistry`
- `ModelProvider` ve `ModelResponse`
- In-memory session/event store
- Basit foreground run loop
- Fake provider ile deterministik testler

Çıkış kriteri: OpenAI veya FastAPI olmadan çalışan örnek agent.

### Faz C — OpenAI text adapter ve güvenli tool pipeline

- Responses API adapter’ı
- Function calling ve structured outputs
- Permission middleware
- Approval ve idempotency altyapısı
- Tool timeout/retry sınıflandırması
- Token/model/tool usage event’leri
- Provider contract testleri

Çıkış kriteri: Aynı core fake provider ve OpenAI provider ile çalışır; izinsiz tool execute edilemez.

### Faz D — Event ve persistence portları

- `SessionStore`, `RunStore`, `EventStore`, `AuditStore` portları
- SQLite referans adapter’ı
- PostgreSQL adapter örneği
- SSE event adapter’ı
- Trace ID zinciri ve secret redaction

Çıkış kriteri: Bir run yeniden başlatma sonrası okunabilir ve event sırası doğrulanabilir.

### Faz E — Task sistemi

- `AgentTask`, status state machine ve `TaskGraph`
- DAG cycle validation
- Scheduler ve worker interface
- Dependency, retry ve cancellation
- Mailbox/checkpoint
- Resource locks ve concurrency sınırları

Çıkış kriteri: Bağımlı ve paralel task testleri; iptal ve retry senaryoları deterministik çalışır.

### Faz F — UI Agent 0.1

- TypeScript headless runtime
- Action registry ve Zod/JSON Schema validation
- HTTP + SSE transport
- Action result ve capability discovery
- React Provider/hooks
- Dynamic Island state machine ve tema API’si
- Artifact renderer registry

Çıkış kriteri: Örnek React uygulaması nav, modal, artifact ve status event’lerini çalıştırır.

### Faz G — Portfolio public agent migration

- Mevcut altı site tool’unu framework tool decorator’ına geçir.
- SQLAlchemy session yerine `AgentContext.services` adapter’ı kullan.
- Mevcut conversation tablolarını framework store adapter’ına bağla.
- `/api/agent/chat` geriye uyumlu tutulurken event stream endpoint’i ekle.
- Mevcut Dynamic Island’ı npm paketine taşı ve Kaiwen theme adapter’ını sitede bırak.
- Ürün kartlarını `project_card` artifact olarak döndür.

Çıkış kriteri: Public davranış değişmeden yeni paketler üzerinden çalışır; admin tool’larına erişim testi kesin olarak reddedilir.

### 18 Eylül 2026 sıra ve yüzey kararı

Public portfolio ve admin artık aynı interaction yüzeyi olarak ele alınmayacaktır:

- Public kAI yalnızca text + SSE kullanır; ürün/hizmet keşfi, brief, lead ve güvenli site
  bağlantıları için çalışan tanıtım yüzeyidir.
- Public Dynamic Island yalnızca son cevabı gösterir; geçmiş konuşmalar backend conversation
  store içinde korunur fakat ziyaretçiye tam chat ekranı olarak sunulmaz.
- Realtime/Live yalnızca authenticated admin web, ileride mobile admin ve phone channel için
  geliştirilecektir.
- Ham audio UI event bus veya public agent endpoint’inden geçirilmez.
- Long-lived API key hiçbir frontend yüzeyine verilmez; session oluşturma, sideband tool
  authority, permission, approval ve audit backend kontrolünde kalır.

Admin fazından önce uygulanacak sıra:

1. Faz G.1 — Public Dynamic Island, Markdown, artifact ve navigation tool stabilizasyonu.
2. Faz R1 — Ayrı platform repository’sinde provider-neutral Realtime Core ve fake provider.
3. Faz R2 — Optional OpenAI Live adapter; WebRTC media ve backend sideband ayrımı.
4. Faz R3 — Text/voice ortak conversation bridge, transcript ve task delegation.
5. Faz H + R4 — Sidebar tabanlı yeni admin bilgi mimarisi, Admin Agent ve Admin Realtime UI.
6. Faz I/R5 sonrası — memory policy; ardından analytics, yapılacaklar, timeline ve worker task’ları.
7. Telephony yalnızca core/admin akışı kararlı olduktan sonra ayrı channel ve approval policy ile.

Bu sıra, `realtimelive_api_entegrasyonu.md` içindeki Realtime'ın execution engine değil
conversation channel olduğu sınırı esas alır.

### Faz H — Admin agent, önce taslak güvenliği

- Ayrı `portfolio-admin` agent tanımı
- `project.create_draft`, `project.update_draft`, `project.preview`
- `project.publish`, `project.archive`, `media.delete` için approval
- Her yazma işleminde admin session, CSRF, permission ve audit
- UI actions: editörü aç, alanları doldur, preview aç, approval göster
- İlk sürümde agent doğrudan production publish yapmaz; açık onay ister.

Çıkış kriteri: “Yeni mobil uygulama taslağı oluştur” komutu draft üretir; publish yalnızca doğrulanmış admin onayıyla olur.

### Faz I — Memory

- Working memory: run ömrü
- Short-term memory: session mesajları ve referanslar
- Long-term memory: yalnızca açık kullanıcı tercihi/policy onaylı kayıt
- Session summarization ve retention
- Semantic memory daha sonra PostgreSQL + pgvector adapter’ı
- Unutma/silme API’si ve admin görünürlüğü

Çıkış kriteri: Task state long-term memory’ye sızmaz; kullanıcı tercihleri kaynak ve confidence bilgisiyle tutulur.

### Faz J — Live voice

- Live/Realtime adapter’ı core dışı optional extra yap.
- Tarayıcı için WebRTC session endpoint’i.
- Backend sideband üzerinden tool authority ve event bridge.
- Text ve voice aynı session/run store’u kullanır.
- Interrupt, turn detection ve transcript event’leri.

Çıkış kriteri: Admin textten voice’a geçince aynı konuşma ve permission context’i korunur.

### Faz K — Günlük rapor ve telefon araması

- Scheduled report task ve günlük özet üretimi
- Bildirim/lead/analytics tool’ları read-only rapor agent’ına verilir
- Telefon için ayrı telephony/SIP adapter’ı
- Açık opt-in, timezone ve aranabilir saat politikası
- Arama öncesi approval/policy kontrolü
- Transcript, özet, aksiyon maddeleri ve retention
- Başarısız çağrı retry ve fallback notification

Çıkış kriteri: Sistem izinsiz arama yapamaz; her arama trace/audit kaydı ve sonradan okunabilir özet üretir.

### Faz L — Yayınlama ve hardening

- Python wheel/sdist ve npm ESM/CJS/types build
- Semantic versioning ve changelog
- Package signing/provenance
- Compatibility matrix
- Evals: tool selection, permission bypass, prompt injection, approval
- Load, reconnect ve failure recovery testleri
- Dokümantasyon ve örnek projeler

## 8. Mevcut Kodun Migration Haritası

| Mevcut parça | Hedef | İşlem |
|---|---|---|
| `apps/api/app/agent/provider.py` | `kaiwen_agent.models.base` | Async, provider-neutral protokole dönüştür |
| `apps/api/app/agent/agent.py` | `kaiwen_agent.models.openai` | OpenAI adapter’ına taşı; site promptunu çıkar |
| `apps/api/app/agent/tools/registry.py` | Core registry + portfolio registration | Generic registry’yi çıkar, altı domain tool’u sitede bırak |
| `apps/api/app/agent/tools/*.py` | Portfolio tool adapters | Service/repository üzerinden çalışmaya devam et |
| `apps/api/app/agent/service.py` | Portfolio application adapter | Runtime çağrısı, store bağlama ve artifact mapping |
| `apps/api/app/agent/models.py` | PostgreSQL store adapter | Mevcut veriyi koruyarak genişlet |
| `apps/web/src/components/dynamic-island.tsx` | `@imthekaiwen/agent-ui/react` + site theme | State/transportu pakete, Kaiwen görünümünü siteye ayır |
| `/api/agent/chat` | Compatibility HTTP adapter | Korunur; yanında event stream eklenir |

## 9. Portfolio İçin Agent Tanımları

### Public agent

Permissions:

```text
projects.read
services.read
leads.create
quotes.draft
ui.public.navigate
ui.public.open_project
ui.public.open_contact
```

Kesinlikle sahip olmayacağı permissions:

```text
projects.write
projects.publish
admin.read
analytics.private.read
deploy.execute
```

### Admin agent

İlk sürüm permissions:

```text
projects.read
projects.draft.create
projects.draft.update
projects.preview
analytics.read
leads.read
messages.read
ui.admin.navigate
ui.admin.open_editor
```

Approval gerektirenler:

```text
projects.publish
projects.archive
media.delete
external.email.send
deploy.execute
outbound_call.start
```

## 10. Test Stratejisi

- Core unit testlerinde gerçek ağ ve gerçek LLM kullanılmaz.
- Provider adapter contract testleri kaydedilmiş fixture/fake client kullanır.
- Ayrı, opt-in smoke test gerçek API anahtarıyla çalışır.
- Permission bypass ve tool injection testleri zorunludur.
- DAG cycle, cancellation, retry ve idempotency property testleri eklenir.
- Protocol fixture’ları Python ve TypeScript paketlerinde ortaktır.
- Dynamic Island için reducer/state-machine testleri ve erişilebilirlik testleri yazılır.
- Portfolio entegrasyon testi public agent’ın admin action çalıştıramadığını doğrular.

## 11. İlk Sprint — Yapılacak Kesin İşler

İlk sprint yalnızca framework temelini kurar:

1. Ayrı `kaiwen-agent-platform` repository/workspace oluştur.
2. `protocol` v1 envelope, event, artifact ve client action schema’larını ekle.
3. Python core modelleri ve `ToolRegistry` oluştur.
4. Fake provider ile foreground agent loop yaz.
5. OpenAI Responses adapter’ını ayrı extra olarak ekle.
6. In-memory event/session store yaz.
7. TypeScript protocol type generation kur.
8. UI action registry’nin headless temelini oluştur.
9. Python/TypeScript contract testlerini CI’da çalıştır.
10. Portfolio’daki mevcut agent koduna dokunmadan örnek adapter yaz.

Bu sprintte henüz task scheduler, semantic memory, Live API, admin yazma tool’ları veya telefon entegrasyonu yapılmayacaktır. Önce paket sınırları ve protokol kararlı hale getirilecektir.

## 12. Yayın Sırası

```text
0.1.0  Core + tools + OpenAI text adapter
0.2.0  Permissions + approvals + events + persistence ports
0.3.0  Task DAG + scheduler + control channel
0.4.0  UI runtime + React + Dynamic Island
0.5.0  Portfolio public integration
0.6.0  Admin draft agent
0.7.0  Memory
0.8.0  Live voice
0.9.0  Scheduled reports + telephony adapter
1.0.0  Hardened APIs, docs, evals and stable protocol
```

## 13. Şimdi Yapılmaması Gerekenler

- Portfolio kodunu doğrudan “framework” diye publish etmek.
- İlk sürümde planner, scheduler, memory, voice ve UI paketini aynı anda bitirmeye çalışmak.
- UI Agent’a bağımsız LLM veya arbitrary DOM/JavaScript yetkisi vermek.
- Public/admin ayrımını yalnızca prompt ile yapmak.
- Publish/delete/deploy/call tool’larını approval olmadan açmak.
- Framework core’una SQLAlchemy/FastAPI/Next.js bağımlılığı eklemek.
- Her event’i ve her konuşmayı süresiz saklamak.
- Telefon görüşmelerini kullanıcı izni ve retention politikası olmadan kaydetmek.

## 14. İlk Mimari Teslimat

Bir sonraki uygulama adımı, mevcut portfolio repository’sinde daha fazla agent özelliği eklemek değil; ayrı `kaiwen-agent-platform` çalışma alanında Faz A ve Faz B’yi oluşturmaktır. Portfolio entegrasyonu, core API contract testleri kararlı olduktan sonra Faz G’de yapılacaktır.

### Uygulama durumu — 17 Eylül 2026

- [x] Portfolio dışında kardeş `kaiwen-agent-platform` repository’si oluşturuldu.
- [x] Python, npm protocol ve UI package çalışma alanları ayrıldı.
- [x] Version’lı event envelope, artifact ve client action JSON Schema’ları eklendi.
- [x] Python ve TypeScript aynı geçerli/geçersiz event fixture’larıyla doğrulandı.
- [x] Async Python core, fake provider, tool decorator/registry ve foreground run loop eklendi.
- [x] Deny-by-default tool lookup ve agent/context permission kesişimi test edildi.
- [x] In-memory run store ve event sink eklendi.
- [x] Provider gerektirmeyen örnek agent ve CI kontrolleri eklendi.
- [ ] Faz C: OpenAI Responses adapter’ı ve tamamlanmış güvenli tool execution pipeline.
- [ ] Faz D: kalıcı event/session adapter’ları ve transport katmanı.
