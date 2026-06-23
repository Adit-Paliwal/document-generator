# IntelliDraft — Architecture Visual Guides

---

## 1. SYSTEM LAYERS & DEPENDENCIES

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                    FRONTEND LAYER                           ┃
┃  ┌─────────────┬──────────────┬──────────────┐             ┃
┃  │   React     │  Vue.js      │  Vanilla JS  │             ┃
┃  │  (future)   │  (future)    │  (current)   │             ┃
┃  └──────┬──────┴──────┬───────┴──────┬───────┘             ┃
┃         │             │              │                     ┃
┃         └─────────────┼──────────────┘                     ┃
┃                       │ HTTP/REST (JSON)                  ┃
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                       ▼                                     ┃
┃          API GATEWAY (Flask/Gunicorn)                      ┃
┃  ┌──────────────────────────────────────┐                 ┃
┃  │  45 REST Endpoints                   │                 ┃
┃  │  ├─ /api/projects (CRUD)             │                 ┃
┃  │  ├─ /api/generate/* (generation)     │                 ┃
┃  │  ├─ /api/*/section (editing)         │                 ┃
┃  │  ├─ /api/*/snapshot (versioning) ⭐  │                 ┃
┃  │  └─ /api/chat/* (chat studio)        │                 ┃
┃  └──────────────────────────────────────┘                 ┃
┃         │                    │                             ┃
┃         ├────────┬───────────┤                             ┃
┃         │        │           │                             ┃
┏━━━━━━━━━┃━━━━━━━┃━━━━━━━━━━┃━━━━━━━━━━━━━━━━━━━━┓
┃         ▼        ▼           ▼                     ┃
┃  ┌────────────────────────────────────────┐     ┃
┃  │  Business Logic Layer                  │     ┃
┃  │  (generation_service.py)               │     ┃
┃  │  ├─ start_job()          [async]       │     ┃
┃  │  ├─ get_job()            ⭐ [fast]     │     ┃
┃  │  ├─ update_section()     [versioned]   │     ┃
┃  │  ├─ regenerate_section() [ai regen]    │     ┃
┃  │  └─ create_snapshot()    [new] ⭐       │     ┃
┃  │  └─ restore_snapshot()   [new] ⭐       │     ┃
┃  └────────────────────────────────────────┘     ┃
┃         │                    │         │        ┃
┗━━━━━━━━━┃━━━━━━━━━━━━━━━━━━┃━━━━━━━━┃━━━━━━┛
          │                    │         │
      ┌───▼───┐           ┌───▼────┐  ┌─▼─────┐
      │ DB    │           │ Cache  │  │ Tasks │
      │       │           │        │  │       │
      │SQLite │──────────▶│ Redis  │  │Celery │
      │/Postgres          │(TTL:1h)   │       │
      │       │           │        │  │       │
      │Tables │◀──────────│        │  │Worker │
      └───────┘           └────────┘  └───────┘
```

---

## 2. REQUEST LIFECYCLE: SECTION EDIT + PREVIEW

```
CLIENT (Browser)
   │
   │ 1. Click heading in preview (iframe)
   │    postMessage: {section_id: "..."}
   │
   ▼
PARENT WINDOW EVENT LISTENER
   │
   │ 2. openSectionEditor(section_id)
   │
   ▼
GET /api/generate/{job_id}/section/{id}
   │
   ├─ Query DB (50ms)
   │  └─ SELECT section, versions WHERE section_id = ?
   │     └─ Uses index on section_id ⭐
   │
   ▼ Return {section_id, title, versions:[{...},...]}
   │
   │ 3. Render edit drawer
   │    Load content from latest accepted version
   │    or current_version if no accepted
   │
   ▼
USER EDITS in textarea (offline)
   │
   │ 4. Click "Save"
   │
   ▼
PATCH /api/generate/{job_id}/section/{id}
Body: {content: "New markdown content"}
   │
   ├─ Validate content not empty (1ms)
   │
   ├─ Find next version number (0ms)
   │
   ├─ INSERT SectionVersion record (10ms)
   │  ├─ trigger_type = "manual_edit" ⭐
   │  ├─ is_accepted = True
   │  └─ content = "New markdown..."
   │
   ├─ UPDATE Section (5ms)
   │  ├─ current_version = 6
   │  ├─ version_hash = MD5("{id}:6")[:16] ⭐
   │  └─ updated_at = now()
   │
   ├─ INVALIDATE cache (20ms)
   │  └─ Redis SCAN+DEL preview:{job_id}:*
   │
   ▼ Return {version: {version_number: 6, ...}}
   │
   │ 5. UI updates
   │    ├─ Toast: "Section saved v6" ✓
   │    ├─ Update nav: word count, status
   │    └─ Close edit drawer
   │
   ├──────────────────────────────┐
   │ 6. [If preview open]         │
   │    Call loadPreview()        │
   │                              │
   │ GET /api/generate/{id}/preview/html
   │   │
   │   ├─ Check Redis cache (5ms)
   │   │  Key: preview:{job_id}:{version_hash}
   │   │  └─ version_hash now from Section.version_hash ⭐
   │   │
   │   ├─ Cache miss (we just invalidated)
   │   │
   │   ├─ Submit Celery task (async)
   │   │  convert_docx_task(job_id)
   │   │
   │   └─ Return {status: "pending", task_id: "xyz"}
   │
   │ 7. Show spinner "Converting document..."
   │
   │ 8. Poll GET /api/generate/{id}/preview/status?task_id=xyz
   │    (every 2 seconds)
   │
   │    Celery Worker:
   │    ├─ Export job to DOCX (100ms)
   │    ├─ Run LibreOffice headless (2000ms) ⚠️
   │    ├─ Inline CSS + images (95ms)
   │    ├─ Cache in Redis (50ms)
   │    └─ Return HTML
   │
   │ 9. status = "ready", HTML ready ✅
   │
   │ 10. Render in iframe
   │    ├─ Create Blob URL
   │    ├─ Apply sandbox attribute
   │    ├─ Inject click handlers ⭐
   │    └─ User can click sections again
   │
   └──────────────────────────────┘

TIMELINE (user experience):
├─ Save edit: 50ms (instant) ✅
├─ Preview convert: 2500ms (spinner shown)
└─ Total: 2.5s perceived load

WITHOUT optimization:
├─ Save edit: 85ms
├─ Preview: 2550ms (added hash recalc)
└─ Improvement: 50ms × many edits/day = savings add up
```

---

## 3. GENERATE WORKFLOW (Document Creation)

```
PROJECT DASHBOARD
   │
   │ User selects project
   │ User picks document type (e.g., "BRD")
   │ User clicks "Generate"
   │
   ▼
POST /api/generate/project/{project_id}
   │
   ├─ Load Project from DB (10ms)
   │  └─ SELECT * FROM projects WHERE project_id = ?
   │
   ├─ Fetch all document_ids (if any)
   │
   ├─ Validate required fields present (5ms)
   │  └─ problem_statement, objective, proposed_solution, etc.
   │
   ├─ Assemble user_inputs (10ms)
   │  └─ Project name, business priority, stakeholders, etc.
   │
   ├─ Load LLM context (100-300ms per document)
   │  ├─ For each attached document:
   │  │  └─ Fetch from Blob storage + parse metadata
   │  └─ Concatenate (capped at 60k chars)
   │
   ▼ Subtotal: ~300-400ms (in Flask request thread)
   │
   │ ┌─────────────────────────────────────────┐
   │ │ return HTTP 201 (IMMEDIATE, non-blocking)│
   │ │ {job_id, status, sections:[...]}        │
   │ └─────────────────────────────────────────┘
   │
   │ [Meanwhile, in BACKGROUND...]
   │
   ▼ (If ASYNC_GENERATION=true)
   │
   _run_generation_job(job_id)  [Daemon thread]
   │
   ├─ Mark job.status = "in_progress" (5ms)
   │
   ├─ For EACH SECTION (sequential):
   │  │
   │  ├─ Mark section.status = "generating" (3ms)
   │  │
   │  ├─ Call LLM (MAIN TIME COST) (10-30s per section!) ⚠️
   │  │  ├─ LLM: "Generate Executive Summary..."
   │  │  ├─ Context: document content + previous sections
   │  │  ├─ Temperature, max_tokens, etc.
   │  │  └─ Retry up to 3x if failure
   │  │
   │  ├─ INSERT SectionVersion (10ms)
   │  │  ├─ version_number = 1
   │  │  ├─ trigger_type = "ai_generation" ⭐
   │  │  ├─ content = "Markdown from LLM"
   │  │  ├─ generation_model = "gpt-5" | "gemini"
   │  │  └─ word_count = len(content.split())
   │  │
   │  ├─ UPDATE Section (5ms)
   │  │  ├─ current_version = 1
   │  │  ├─ version_hash = MD5("{id}:1") ⭐
   │  │  ├─ status = "completed"
   │  │  └─ updated_at = now()
   │  │
   │  ├─ COMMIT (10ms)
   │  │
   │  └─ Client polls GET /api/generate/{job_id}
   │     → Updates progress bar every 2s
   │     → Shows "Generating section 3/10..."
   │
   ├─────────────────────────────────────────
   │ Total: Section 1 (30s) + 2 (30s) + 3-10 (120s) = ~3 min
   │ = Typical BRD with 10 sections
   ├─────────────────────────────────────────
   │
   ├─ After ALL sections done:
   │  │
   │  ├─ Mark job.status = "completed" (5ms)
   │  ├─ job.completed_at = now()
   │  └─ Update linked Project.status = "completed"
   │
   ▼ DONE
   
CLIENT observes job.status → "completed"
   │
   ├─ Green checkmark ✓
   ├─ Enable "Preview" button
   ├─ Enable "Export" button
   ├─ Show section nav with content
   │
   ▼
User can now:
├─ View preview (LibreOffice HTML)
├─ Edit sections (see Flow #2)
├─ Regenerate sections with comments
├─ Save versions (snapshots) ⭐
├─ Restore to previous versions ⭐
└─ Export as DOCX/PDF
```

---

## 4. VERSIONING & SNAPSHOT SYSTEM ⭐ [NEW]

```
SECTION VERSIONS
================

Section: "Executive Summary"

       ┌─ Version 1 (AI generated)
       │  ├─ trigger_type: "ai_generation" ⭐
       │  ├─ is_accepted: true
       │  ├─ content: "This is the original..."
       │  └─ created_at: 2026-06-23 10:00:00
       │
       ├─ Version 2 (AI regenerated after comment)
       │  ├─ trigger_type: "ai_regeneration" ⭐
       │  ├─ trigger_comment_id: "comment123"
       │  ├─ is_accepted: true
       │  ├─ content: "Revised based on feedback..."
       │  └─ created_at: 2026-06-23 10:15:00
       │
       └─ Version 3 (Manual edit from user)
          ├─ trigger_type: "manual_edit" ⭐
          ├─ is_accepted: true (auto-accepted)
          ├─ content: "User hand-edited text..."
          └─ created_at: 2026-06-23 10:45:00

current_version = 3  ← The one used in final document
version_hash = "a1b2c3d4"  ← Cached MD5 for preview caching

═══════════════════════════════════════════════════

DOCUMENT SNAPSHOTS ⭐ [NEW TABLE]
=================================

DocumentSnapshot captures ALL accepted versions at one point in time.
Use for:
- Checkpoints before major edits
- Rollback if something breaks
- Version history navigation
- Review agent integration (future)

Snapshot #1: "Initial generation"
├─ created_at: 2026-06-23 10:00:00
├─ trigger_type: "auto" (created by system)
├─ label: "Initial generation"
└─ section_refs: [
     {section_id: "sec1", version_id: "v1_3", version_number: 1},
     {section_id: "sec2", version_id: "v2_2", version_number: 1},
     {section_id: "sec3", version_id: "v3_1", version_number: 1},
     ...
   ]

Snapshot #2: "After legal review" ⭐ [Manual]
├─ created_at: 2026-06-23 11:30:00
├─ trigger_type: "manual"
├─ label: "After legal review"
└─ section_refs: [
     {section_id: "sec1", version_id: "v1_3", version_number: 3},
     {section_id: "sec2", version_id: "v2_2", version_number: 2},
     {section_id: "sec3", version_id: "v3_4", version_number: 4},  ← updated
     ...
   ]

Snapshot #3: "Final approved" ⭐ [Manual]
├─ created_at: 2026-06-23 14:00:00
├─ trigger_type: "manual"
├─ label: "Final approved"
└─ section_refs: [...]

═══════════════════════════════════════════════════

RESTORE WORKFLOW:

User clicks [Restore] on Snapshot #2

FOR EACH section_ref in snapshot.section_refs:
  ├─ Get Section
  ├─ Find SectionVersion matching {section_id, version_id}
  ├─ Set is_accepted=True
  ├─ Set section.current_version = ref.version_number
  └─ COMMIT

Result:
├─ Document rolled back to Snapshot #2 state
├─ All 10 sections' "accepted version" now point to saved state
├─ Next export → includes rolled-back content
└─ Preview cache invalidated → re-converts with old content
```

---

## 5. CACHING STRATEGY

```
PREVIEW HTML CACHE (Redis)
===========================

Cache Entry:
┌────────────────────────────────────────────────┐
│ Key: preview:{job_id}:{version_hash}           │
│ ├─ job_id: "a1b2c3d4"                          │
│ ├─ version_hash: "x9y8z7w6"                    │
│ │  └─ MD5("{sec1}:{v3}|{sec2}:{v2}|...")[:16] │
│ │  └─ Computed ONCE per section edit           │
│ │  └─ Now cached in Section.version_hash ⭐   │
│ │                                              │
│ Value: <html>...</html>  (2-5MB)               │
│ ├─ Self-contained (no external resources)      │
│ ├─ CSS inlined                                 │
│ └─ Images as base64                            │
│                                                │
│ TTL: 3600 seconds (1 hour)                    │
│ ├─ Auto-expire after 1 hour of inactivity     │
│ └─ Manual eviction: LRU when memory full      │
└────────────────────────────────────────────────┘

CACHE HIT SCENARIO:
┌─────────────────────────────────────────────┐
│ User clicks Preview (2nd time)               │
├─────────────────────────────────────────────┤
│ GET /api/generate/{job_id}/preview/html     │
│   │                                          │
│   ├─ Redis GET preview:a1b2c3d4:x9y8z7w6    │
│   │  └─ 5ms ✅ Found!                        │
│   │                                          │
│   └─ Return { status: "ready", html: "..." }│
│      └─ HTTP 200                            │
│                                              │
│ Frontend renders in iframe (100ms)           │
│ TOTAL: 105ms 🚀 (vs 2500ms on first load)  │
└─────────────────────────────────────────────┘

CACHE MISS SCENARIO:
┌─────────────────────────────────────────────┐
│ User edits section (version_hash changes)    │
├─────────────────────────────────────────────┤
│ PATCH /api/generate/{job_id}/section/{id}   │
│   └─ Invalidate cache:                       │
│      Redis SCAN+DEL preview:a1b2c3d4:*      │
│      (deletes all cached HTML for this job) │
│                                              │
│ GET /api/generate/{job_id}/preview/html     │
│   │                                          │
│   ├─ Redis GET preview:a1b2c3d4:NEW_HASH    │
│   │  └─ 5ms, Not found (cache miss)         │
│   │                                          │
│   ├─ Submit Celery task                     │
│   │  convert_docx_task.apply_async(...)     │
│   │                                          │
│   └─ Return { status: "pending", task_id }  │
│      └─ HTTP 202 (Accepted)                 │
│                                              │
│ [Celery Worker convertsLibreOffice...]      │
│   ├─ Run: soffice --headless --convert...   │
│   ├─ Time: 2000ms (variable)                │
│   └─ Cache result in Redis                  │
│                                              │
│ Client polls /preview/status?task_id=...    │
│ (every 2s for up to 60s)                    │
│   └─ Wait for task.state = "SUCCESS"        │
│                                              │
│ TOTAL: 2500-3000ms 🕐                       │
│ (acceptable because async, spinner shown)   │
└─────────────────────────────────────────────┘

CACHE STATISTICS (typical day):
├─ Requests: 500 /preview calls
├─ Hit rate: ~65% (many users see same doc)
├─ Hit cost: 500 × 0.65 × 5ms = 1.6 seconds
├─ Miss cost: 500 × 0.35 × 2500ms = 437 seconds
├─ Total preview time: 438.6 seconds/day
├─ Without cache: 500 × 2500ms = 1250 seconds
└─ Savings: 1250 - 438.6 = 811 seconds/day (65%)

PEAK LOAD (10 concurrent users):
├─ Naive: 10 users → 10 LibreOffice processes
├─ With coalescing: 10 → 1 process (future)
└─ Savings: 9 × 2400ms = 21.6 seconds = HUGE
```

---

## 6. DATABASE QUERY OPTIMIZATION

```
❌ BEFORE: N+1 Query Problem
═══════════════════════════

GET /api/generate/{job_id}
  │
  └─ get_job(job_id)
     │
     ├─ Query 1: SELECT * FROM generation_jobs WHERE job_id = ? (20ms)
     │
     ├─ Lazy load sections:
     │  ├─ Query 2-11: SELECT * FROM sections WHERE job_id = ? (10ms × 10 sections)
     │  │
     │  └─ Lazy load versions:
     │     └─ Query 12-61: SELECT * FROM section_versions WHERE section_id = ?
     │        (50ms × 10 sections × 5 versions each)
     │
     └─ Total: 1 + 10 + 50 = 61 QUERIES (358ms!)

Result:
- Every version loaded
- Response: 350KB JSON (all versions)
- Time: 358ms
- Database thrashing ⚠️


✅ AFTER: Optimized with Include Flag
═════════════════════════════════════

GET /api/generate/{job_id}
  │
  └─ get_job(job_id, include_all_versions=False)
     │ [DEFAULT FAST PATH]
     │
     ├─ Query 1: SELECT * FROM generation_jobs WHERE job_id = ? (20ms)
     │
     ├─ Join sections:
     │  └─ Query 2-11: SELECT * FROM sections WHERE job_id = ? (10ms)
     │
     ├─ For EACH section, get current version only:
     │  └─ Extract from versions array (already loaded by lazy load)
     │     OR Query separately if needed (10ms × 10)
     │
     └─ Total: 1 + 10 = 11 QUERIES (45ms!) ✅
        └─ Clears versions array before serialization
        └─ Response: 25KB JSON (only current)

If user explicitly wants full history:
GET /api/generate/{job_id}?include_versions=all
  └─ get_job(job_id, include_all_versions=True)
     └─ Returns all versions (358ms)
        └─ Used only for version comparison UI
        └─ Explicitly requested, user accepts latency

Improvement:
├─ Default (90% of calls): 45ms (8× faster)
├─ Explicit (10% of calls): 358ms (same as before)
└─ Average: 45×0.9 + 358×0.1 = 85.8ms (4× improvement)
```

---

## 7. PAGINATION ON LIST ENDPOINTS

```
❌ BEFORE
═════════

GET /api/projects
  │
  ├─ Query: SELECT * FROM projects ORDER BY created_at DESC
  │  └─ 10,000 projects (typical for year of usage)
  │
  ├─ Database: 5000ms (loading 10k rows)
  ├─ Network: 5MB (JSON response)
  ├─ Browser: OOM if JS tries to render all ⚠️
  │
  └─ Total: 5+ seconds blocked

User experience:
├─ Page freezes for 5 seconds
├─ Browser memory spikes 50-100MB
└─ Mobile → crash


✅ AFTER: Pagination
═════════════════════

GET /api/projects?page=1&per_page=50
  │
  ├─ Query: SELECT * FROM projects
  │         ORDER BY created_at DESC
  │         LIMIT 50 OFFSET 0
  │  └─ 50 projects
  │
  ├─ Database: 50ms (OFFSET/LIMIT fast)
  ├─ Network: 50KB (JSON response)
  ├─ Browser: Instant render (100ms)
  │
  └─ Total: 150ms (instant)

Response shape:
{
  "projects": [{...}, {...}, ...],  // 50 items
  "total": 10000,                    // full count
  "page": 1,                         // current page
  "per_page": 50,                    // items per page
  "pages": 200,                      // total pages
  "count": 50                        // items in response
}

Frontend pagination:
├─ Load page 1 (first 50)
├─ Load page 2 (next 50) on scroll
├─ Load page 3, 4, ... as needed
└─ Can jump to page X directly

Improvement:
├─ Load time: 5000ms → 150ms (33× faster) ✅
├─ Memory: 100MB → 2MB (50× less)
├─ Network: 5MB → 50KB (100× smaller)
└─ Mobile: Now works smoothly
```

---

## 8. REQUEST FLOW DIAGRAM: FULL CYCLE

```
              ┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
              ┃   USER INTERACTS            ┃
              ┗━━━━━━━━━┬━━━━━━━━━━━━━━━━━━┛
                        │
        ┌───────────────┼───────────────┐
        │               │               │
        ▼               ▼               ▼
    [GENERATE]     [EDIT]           [SHARE]
        │               │               │
        │               │               │
    ┌───▼─────────┐ ┌──▼──────────┐ ┌─▼──────────┐
    │ POST /gen   │ │ PATCH /sec  │ │ GET /export│
    │ body: {}    │ │ body: text  │ │ ?format=   │
    └───┬─────────┘ └──┬──────────┘ └─┬──────────┘
        │              │              │
        │              │              └──────────────┐
        │              │                             │
        │ ┌────────────▼──────────────┐              │
        │ │ GENERATION SERVICE        │              │
        │ │ ├─ start_job()            │              │
        │ │ ├─ get_job()   ⭐ FAST   │              │
        │ │ ├─ update_section() ⭐   │              │
        │ │ └─ [Async LLM in bg]     │              │
        │ └────────────┬──────────────┘              │
        │              │                             │
        │              │ ┌─────────────────┐         │
        │              │ │ UPDATE           │         │
        │              │ │ ├─ version_hash  │         │
        │              │ │ ├─ current_ver   │         │
        │              │ │ └─ invalidate    │         │
        │              │ └────────┬─────────┘         │
        │              │          │                  │
        │ ┌────────────▼──────────▼──────────────┐   │
        │ │ DATABASE (PostgreSQL/SQLite)         │   │
        │ │ ├─ SectionVersion (trigger_type) ⭐  │   │
        │ │ ├─ Section (version_hash) ⭐        │   │
        │ │ ├─ DocumentSnapshot ⭐              │   │
        │ │ └─ GenerationJob                    │   │
        │ └────────────┬──────────────┬─────────┘   │
        │              │              │             │
        │              │     ┌────────▼──────┐      │
        │              │     │ REDIS CACHE    │      │
        │              │     │ ├─ Invalidate  │      │
        │              │     │ ├─ TTL: 1hr    │      │
        │              │     │ └─ Hit: 5ms    │      │
        │              │     └────────┬──────┘      │
        │              │              │             │
        └──────────┐   └──────────┐   └───┬─────────┘
                   │              │       │
        ┌──────────▼──────────────▼─┐ ┌──▼──────────┐
        │ RESPONSE TO CLIENT        │ │ EXPORT      │
        │ ├─ {job_id, status}       │ │ ├─ DOCX     │
        │ ├─ {version_number}       │ │ ├─ PDF      │
        │ └─ {snapshot_id}          │ │ └─ Markdown │
        └──────────────────────────┘ └─┬────────────┘
                  │                    │
        ┌─────────┴────────────────────┴─────┐
        │        FRONTEND UPDATES             │
        │ ├─ Toast notification ✓             │
        │ ├─ Reload section nav               │
        │ ├─ Update preview (if open)         │
        │ └─ Refresh word counts              │
        └─────────────────────────────────────┘
```

---

**End of Architecture Diagrams**

For detailed descriptions and performance metrics, see: **ARCHITECTURE_OPTIMIZED.md**
