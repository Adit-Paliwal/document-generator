import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "../api.js";
import { Spinner, useToast } from "../components/ui.jsx";

/*
 Single-page Create Project with document-driven auto-fill.

 Flow the user asked for:
   1. Upload one or more source documents (top zone).
   2. On parse, the backend LLM extracts the ingested fields
      (POST /api/extract-project-data) AND derives the extended fields
      (POST /api/projects/{id}/derive-fields) — both happen in the backend
      while a spinner shows "Extracting…".
   3. Every field on the one page is pre-filled and freely editable.
   4. Save → finalizes the draft project to "ready".

 Field target: "p" = ingested (Project columns) · "d" = derived (DerivedData).
*/

const SECTIONS = [
  {
    title: "Core Details",
    fields: [
      ["Project Name", "project_name", "text", true, "p"],
      ["Project Code", "project_code", "text", false, "p"],
      ["Business Unit / Department", "business_unit", "text", true, "p"],
      ["Business Priority & Criticality", "business_priority", "select:Critical,Highly Critical,Non-Critical", false, "p"],
      ["Type", "project_type", "select:internal,external", false, "p"],
      ["Estimated Cost (₹ Crores)", "estimated_cost_crores", "text", false, "p"],
    ],
  },
  {
    title: "Project Summary",
    fields: [
      ["Objective", "project_objective", "area", true, "p"],
      ["Current Challenges / Problem Statement", "problem_statement", "area", true, "p"],
      ["Pain Points", "pain_points", "area", false, "p"],
      ["Opportunities", "opportunities", "area", false, "p"],
      ["Business Justification", "business_justification", "area", false, "p"],
      ["Current (As-Is) Process", "as_is_processes", "area", true, "p"],
      ["Start Date", "start_date", "date", false, "p"],
      ["End Date", "end_date", "date", false, "p"],
      ["Deadline", "deadline", "date", false, "p"],
    ],
  },
  {
    title: "Project Details",
    fields: [
      ["Proposed Solution Overview", "proposed_solution", "area", true, "p"],
      ["Technical Landscape & Integrations", "technical_landscape", "area", true, "p"],
      ["Integration Requirement", "integration_requirement", "area", false, "p"],
      ["Assumptions", "assumptions", "area", false, "p"],
      ["Constraints", "constraints", "area", false, "p"],
      ["Risks & Mitigation Plans", "risks", "area", false, "p"],
    ],
  },
  {
    title: "AI-Derived Details",
    subtitle: "Auto-generated from your documents by the extraction agent. Edit anything.",
    fields: [
      ["Current Challenges", "current_challenges", "area", false, "d"],
      ["To-Be Process", "to_be_process", "area", false, "d"],
      ["Success Criteria", "success_criteria", "area", false, "d"],
      ["Business Requirements", "business_requirements", "area", false, "d"],
      ["Functional Requirements", "functional_requirements", "area", false, "d"],
      ["Non-Functional Requirements", "non_functional_requirements", "area", false, "d"],
      ["Workflow", "workflow", "area", false, "d"],
      ["Systems Involved", "systems_involved", "area", false, "d"],
      ["Data Sources & Availability", "data_sources", "area", false, "d"],
      ["Analytics / Reporting Requirements", "analytics_requirements", "area", false, "d"],
      ["Industry Benchmarks", "industry_benchmarks", "area", false, "d"],
      ["Constraints & Dependencies", "constraints_dependencies", "area", false, "d"],
    ],
  },
  {
    title: "Optional Information",
    fields: [
      ["Approval Matrix", "approval_matrix", "area", false, "p"],
      ["Future Roadmap", "future_roadmap", "area", false, "p"],
      ["Scalability Considerations", "scalability_considerations", "area", false, "p"],
      ["Innovation Objectives", "innovation_objectives", "area", false, "p"],
      ["Sustainability / ESG Impact", "sustainability_esg", "area", false, "p"],
    ],
  },
];

const ALL_FIELDS = SECTIONS.flatMap((s) => s.fields);
const REQUIRED_KEYS = ALL_FIELDS.filter(([, , , req]) => req).map(([, k]) => k);

export default function CreateProject() {
  const nav = useNavigate();
  const toast = useToast();
  const [form, setForm] = useState({ stakeholders: [{ name: "", designation: "" }] });
  const [docs, setDocs] = useState([]);
  const [pid, setPid] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [autoStatus, setAutoStatus] = useState(null);   // null | "extracting" | "deriving"
  const [autoFilled, setAutoFilled] = useState(new Set());
  const [saving, setSaving] = useState(false);
  const fileRef = useRef(null);
  const filledRef = useRef(new Set());

  const set = (k, v) =>
    setForm((f) => ({ ...f, [k]: v }));

  const onEdit = (k, v) => {
    // clear the auto-fill highlight once the user touches a field
    if (filledRef.current.has(k)) {
      filledRef.current.delete(k);
      setAutoFilled(new Set(filledRef.current));
    }
    set(k, v);
  };

  /* ── ingested / derived split (for persistence) ────────── */
  const ingestedBody = () => {
    const b = { stakeholders: form.stakeholders.filter((s) => s.name.trim()) };
    ALL_FIELDS.forEach(([, k, , , t]) => {
      if (t === "p" && (form[k] ?? "") !== "") b[k] = form[k];
    });
    return b;
  };
  const derivedBody = () => {
    const b = {};
    ALL_FIELDS.forEach(([, k, , , t]) => {
      if (t === "d" && (form[k] || "").trim()) b[k] = form[k];
    });
    return b;
  };

  /* ── draft project (created lazily, reused) ────────────── */
  // NOTE: project_code is deliberately NOT sent to the scratch draft — the
  // uniqueness check belongs at final "Create Project" (createProject's PATCH),
  // not during auto-fill, so a duplicate extracted code can't block derivation.
  async function ensureDraft(extraIngested = {}) {
    const { project_code, ...extra } = extraIngested;   // eslint-disable-line no-unused-vars
    if (pid) {
      await api.patch(`/projects/${pid}`, { ...extra, document_ids: docs.map((d) => d.document_id) });
      return pid;
    }
    const base = ingestedBody();
    delete base.project_code;
    const created = await api.post("/projects/draft", {
      ...base, ...extra,
      document_ids: docs.map((d) => d.document_id),
    });
    setPid(created.project_id);
    return created.project_id;
  }

  /* ── upload → parse → auto-fill ────────────────────────── */
  async function uploadFile(file) {
    if (!file) return;
    setUploading(true);
    let uploaded;
    try {
      const fd = new FormData();
      fd.append("file", file);
      const d = await api.post("/upload", fd);
      uploaded = { document_id: d.document_id, filename: d.filename };
      setDocs((prev) => [...prev, uploaded]);
      toast(`Parsed ${d.filename}`, "ok");
    } catch (e) {
      toast(`Upload failed: ${e.message}`, "err");
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    setUploading(false);
    if (fileRef.current) fileRef.current.value = "";
    // auto-fill right after a successful parse (user's requirement)
    await autoFill([...docs, uploaded].map((d) => d.document_id));
  }

  async function autoFill(docIds) {
    const ids = docIds || docs.map((d) => d.document_id);
    if (!ids.length) { toast("Upload a document first to auto-fill.", "err"); return; }
    const filled = new Set(filledRef.current);
    try {
      setAutoStatus("extracting");

      // Draft first (cheap, no LLM) so BOTH LLM calls can run CONCURRENTLY:
      // extraction fills the ingested fields, derivation reads the raw docs
      // directly. Serial was ~190s wall-clock; parallel is ~max(90s, 100s).
      const projectId = await ensureDraft();

      const extractP = api.post("/extract-project-data", { document_ids: ids });
      const deriveP  = api.post(`/projects/${projectId}/derive-fields`)
        .then(() => api.get(`/projects/${projectId}/data`))
        .catch((e) => { toast(`Derived fields skipped: ${e.message}`, "info"); return null; });

      // 1. Ingested fields from extraction
      const ex = await extractP;
      const extracted = ex.extracted || {};
      setForm((f) => {
        const next = { ...f };
        for (const [k, v] of Object.entries(extracted)) {
          if (v == null) continue;
          if (k === "stakeholders" && Array.isArray(v) && v.length) {
            const rows = v.filter((s) => s && (s.name || "").trim())
                          .map((s) => ({ name: s.name || "", designation: s.designation || "" }));
            if (rows.length) { next.stakeholders = rows; filled.add("stakeholders"); }
          } else if (typeof v === "string" && v.trim() && !(next[k] || "").trim()) {
            next[k] = v.trim(); filled.add(k);
          }
        }
        return next;
      });
      // Persist extracted values onto the draft (project_code excluded — see ensureDraft)
      api.patch(`/projects/${projectId}`, Object.fromEntries(
        Object.entries(extracted).filter(([k, v]) =>
          k !== "stakeholders" && k !== "project_code" && typeof v === "string" && v.trim()),
      )).catch(() => {});

      // 2. Derived fields (already running in parallel)
      setAutoStatus("deriving");
      const data = await deriveP;
      if (data) {
        setForm((f) => {
          const next = { ...f };
          for (const [k, v] of Object.entries(data.derived || {})) {
            if (typeof v === "string" && v.trim() && !(next[k] || "").trim()) {
              next[k] = v; filled.add(k);
            }
          }
          return next;
        });
      }

      filledRef.current = filled;
      setAutoFilled(new Set(filled));
      toast(`Auto-filled ${filled.size} field(s) from ${ids.length} document(s). Review and edit as needed.`, "ok");
    } catch (e) {
      toast(`Auto-fill failed: ${e.message}`, "err");
    } finally {
      setAutoStatus(null);
    }
  }

  /* ── create ────────────────────────────────────────────── */
  async function createProject() {
    const missing = REQUIRED_KEYS.filter((k) => !(form[k] || "").trim());
    if (missing.length) {
      const labels = missing.map((k) => ALL_FIELDS.find(([, key]) => key === k)[0]);
      toast(`Required: ${labels.join(", ")}`, "err");
      return;
    }
    if (!form.stakeholders.some((s) => s.name.trim())) {
      toast("Add at least one stakeholder.", "err");
      return;
    }
    setSaving(true);
    try {
      const projectId = await ensureDraft();
      await api.patch(`/projects/${projectId}`, ingestedBody());
      const der = derivedBody();
      if (Object.keys(der).length) await api.put(`/projects/${projectId}/data/derived`, der);
      const v = await api.post(`/projects/${projectId}/validate`);
      if (v.valid) await api.patch(`/projects/${projectId}`, { status: "ready" });
      toast("Project created.", "ok");
      nav(`/project/${projectId}`);
    } catch (e) {
      toast(`Create failed: ${e.message}`, "err");
    } finally {
      setSaving(false);
    }
  }

  const busy = !!autoStatus;

  return (
    <div className="mx-auto max-w-5xl px-4 py-4">
      {/* header */}
      <div className="mb-3 flex items-center gap-3">
        <button className="btn-ghost px-2.5" onClick={() => nav("/")} title="Back to dashboard">←</button>
        <h2 className="text-lg font-extrabold text-gray-800">Create New Project</h2>
      </div>

      {/* ── Auto-fill zone ── */}
      <div className="card mb-4 border-brand-200 bg-brand-50/40 p-4">
        <div className="flex items-center justify-between">
          <div>
            <div className="text-sm font-bold text-brand-700">✨ Auto-fill from documents</div>
            <div className="text-xs text-gray-500">
              Upload a BRD, proposal, or any project document — the AI reads it and fills the form below
              (including AI-derived details). Everything stays editable.
            </div>
          </div>
          <button
            className="btn-ghost" data-testid="autofill-browse"
            disabled={uploading || busy} onClick={() => fileRef.current?.click()}
          >
            {uploading ? "Parsing…" : "＋ Upload document"}
          </button>
          <input ref={fileRef} type="file" hidden accept=".pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls"
                 onChange={(e) => uploadFile(e.target.files?.[0])} />
        </div>

        {docs.length > 0 && (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {docs.map((d) => (
              <span key={d.document_id} className="flex items-center gap-1.5 rounded-full bg-white px-3 py-1 text-xs font-semibold text-brand-700 shadow-sm">
                📎 {d.filename}
              </span>
            ))}
            <button className="btn-ghost text-xs" data-testid="autofill-run" disabled={busy} onClick={() => autoFill()}>
              ↻ Re-run auto-fill
            </button>
          </div>
        )}

        {busy && (
          <div className="mt-3 flex items-center gap-3 rounded-lg bg-white px-4 py-3 text-sm text-brand-700 shadow-sm">
            <span className="h-4 w-4 animate-spin rounded-full border-2 border-brand-200 border-t-brand-500" />
            {autoStatus === "extracting"
              ? "Reading document and extracting project details…"
              : "Deriving extended requirements in the background…"}
          </div>
        )}
      </div>

      {/* ── Single-page form ── */}
      <div className="flex flex-col gap-4">
        {SECTIONS.map((sec) => (
          <div key={sec.title} className="card p-5">
            <h3 className="text-sm font-bold text-gray-700">{sec.title}</h3>
            {sec.subtitle && <p className="mb-3 mt-0.5 text-xs text-gray-400">{sec.subtitle}</p>}
            <div className={sec.subtitle ? "" : "mt-3"}>
              <FieldGrid fields={sec.fields} form={form} onEdit={onEdit} autoFilled={autoFilled} />
            </div>
            {sec.title === "Core Details" && (
              <Stakeholders form={form} setForm={setForm} highlighted={autoFilled.has("stakeholders")} />
            )}
          </div>
        ))}
      </div>

      {/* ── Sticky save bar ── */}
      <div className="sticky bottom-0 z-10 mt-4 flex items-center justify-end gap-3 rounded-xl border border-gray-200 bg-white/95 px-4 py-3 shadow-lg backdrop-blur">
        <span className="mr-auto text-xs text-gray-400">
          {autoFilled.size > 0 ? `${autoFilled.size} field(s) auto-filled — review before creating.` : "Fill the required fields, then create."}
        </span>
        <button className="btn-ghost" onClick={() => nav("/")}>Cancel</button>
        <button className="btn-brand" data-testid="create-submit" disabled={saving || busy} onClick={createProject}>
          {saving ? "Creating…" : "+ Create Project"}
        </button>
      </div>

      {saving && <Spinner label="Saving project…" />}
    </div>
  );
}

/* strict 2-column grid */
function FieldGrid({ fields, form, onEdit, autoFilled }) {
  return (
    <div className="grid grid-cols-1 gap-x-5 gap-y-3 md:grid-cols-2">
      {fields.map(([label, key, type, req]) => {
        const hl = autoFilled.has(key)
          ? "ring-2 ring-brand-200 border-brand-300 bg-brand-50/40"
          : "";
        const common = { "data-testid": `f-${key}`, value: form[key] || "", onChange: (e) => onEdit(key, e.target.value) };
        return (
          <div key={key}>
            <label className="label flex items-center gap-1.5">
              {label}{req && <span className="text-red-500">*</span>}
              {autoFilled.has(key) && <span className="rounded bg-brand-100 px-1.5 text-[9px] font-bold text-brand-600">AI</span>}
            </label>
            {type === "area" ? (
              <textarea className={`input h-20 resize-none overflow-y-auto ${hl}`} {...common} />
            ) : type === "date" ? (
              <input type="date" className={`input ${hl}`} {...common} />
            ) : type.startsWith("select:") ? (
              <select className={`input ${hl}`} {...common}>
                <option value="">— select —</option>
                {type.slice(7).split(",").map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : (
              <input className={`input ${hl}`} {...common} />
            )}
          </div>
        );
      })}
    </div>
  );
}

function Stakeholders({ form, setForm, highlighted }) {
  const rows = form.stakeholders;
  const update = (i, k, v) =>
    setForm((f) => ({ ...f, stakeholders: f.stakeholders.map((s, j) => (j === i ? { ...s, [k]: v } : s)) }));
  return (
    <div className="mt-4">
      <label className="label flex items-center gap-1.5">
        Stakeholders <span className="text-red-500">*</span>
        {highlighted && <span className="rounded bg-brand-100 px-1.5 text-[9px] font-bold text-brand-600">AI</span>}
      </label>
      {rows.map((s, i) => (
        <div key={i} className="mb-2 flex gap-2">
          <input className="input" placeholder="Name" data-testid={`stk-name-${i}`}
                 value={s.name} onChange={(e) => update(i, "name", e.target.value)} />
          <input className="input" placeholder="Designation"
                 value={s.designation} onChange={(e) => update(i, "designation", e.target.value)} />
          <button className="btn-danger px-2.5" title="Remove" disabled={rows.length === 1}
                  onClick={() => setForm((f) => ({ ...f, stakeholders: f.stakeholders.filter((_, j) => j !== i) }))}>🗑</button>
        </div>
      ))}
      <button className="btn-ghost text-xs"
              onClick={() => setForm((f) => ({ ...f, stakeholders: [...f.stakeholders, { name: "", designation: "" }] }))}>
        + Add stakeholder
      </button>
    </div>
  );
}
