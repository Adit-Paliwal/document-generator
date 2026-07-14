import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, mdToHtml, timeAgo } from "../api.js";
import { Modal, Spinner, StatusChip, useToast } from "../components/ui.jsx";

const DOC_TYPES = ["BRD", "RFP", "SOW", "Proposal", "TechSpec", "Scope", "NDPR", "NFA", "NIT", "BOQ", "ARB"];
const TABS = ["Data", "Generation", "Preview"];

export default function ProjectPage() {
  const { projectId } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const [proj, setProj] = useState(null);
  const [tabIdx, setTabIdx] = useState(0);
  const [docType, setDocType] = useState("BRD");
  const [docs, setDocs] = useState({});          // UPPER type → job record
  const [sidebarOpen, setSidebarOpen] = useState(true);

  const refreshDocs = useCallback(async () => {
    try {
      const d = await api.get(`/projects/${projectId}/documents`);
      const map = {};
      (d.documents || []).forEach((x) => { map[(x.document_type || "").toUpperCase()] = x; });
      setDocs(map);
      return map;
    } catch { return {}; }
  }, [projectId]);

  useEffect(() => {
    api.get(`/projects/${projectId}`).then((p) => {
      setProj(p);
      if (p.document_type) setDocType(p.document_type.toUpperCase());
    }).catch((e) => toast(`Project load failed: ${e.message}`, "err"));
    refreshDocs();
  }, [projectId, refreshDocs, toast]);

  if (!proj) return <Spinner label="Loading project…" />;

  const tab = TABS[tabIdx];
  const currentJob = docs[docType];

  return (
    <div className="mx-auto max-w-7xl px-4 py-3">
      {/* ── Project ribbon: purple gradient fading left → right (Figma) ── */}
      <div className="mb-3 flex items-center justify-between rounded-xl bg-gradient-to-r from-brand-100 via-brand-50 to-white px-4 py-3">
        <div className="flex items-center gap-3">
          <button className="btn-ghost px-2.5" onClick={() => nav("/")} title="Back to dashboard">←</button>
          <div>
            <div className="text-base font-extrabold text-gray-800">{proj.project_name || "Untitled project"}</div>
            <div className="text-[11px] text-gray-500">
              {proj.project_code && <span className="mr-2 font-semibold">{proj.project_code}</span>}
              ID: {proj.project_id.slice(0, 8)}… · Created {timeAgo(proj.created_at)}
            </div>
          </div>
          <StatusChip status={proj.status} />
        </div>

        {/* Prev / Next tab navigation + Go-to-Review (Figma msg#293) */}
        <div className="flex items-center gap-2 rounded-lg bg-white/70 px-2 py-1.5 shadow-sm">
          <button className="btn-ghost px-2.5" disabled={tabIdx === 0} onClick={() => setTabIdx(tabIdx - 1)} title="Previous">‹</button>
          <span className="w-24 text-center text-sm font-bold text-brand-600">{tab}</span>
          <button className="btn-ghost px-2.5" disabled={tabIdx === TABS.length - 1} onClick={() => setTabIdx(tabIdx + 1)} title="Next">›</button>
          <button className="btn-brand" onClick={() => nav("/?tab=review")}>Go to Review</button>
        </div>
      </div>

      {tab === "Data" ? (
        <DataTab projectId={projectId} />
      ) : (
        <div className="flex gap-3">
          {/* Collapsible documents sidebar (Generation + Preview only, per Figma) */}
          <div className={`shrink-0 transition-all ${sidebarOpen ? "w-52" : "w-9"}`}>
            <div className="card overflow-hidden">
              <button
                className="flex w-full items-center justify-between border-b border-gray-100 px-3 py-2 text-xs font-bold text-gray-500 hover:bg-gray-50"
                onClick={() => setSidebarOpen(!sidebarOpen)}
              >
                {sidebarOpen && <span>DOCUMENTS</span>}
                <span>{sidebarOpen ? "«" : "»"}</span>
              </button>
              {sidebarOpen && DOC_TYPES.map((t) => {
                const d = docs[t];
                return (
                  <button
                    key={t} data-testid={`doc-${t}`}
                    onClick={() => setDocType(t)}
                    className={`flex w-full items-center justify-between px-3 py-2 text-sm hover:bg-brand-50 ${
                      docType === t ? "bg-brand-50 font-bold text-brand-600" : "text-gray-600"
                    }`}
                  >
                    <span>{t}</span>
                    <span title={d ? `${d.status} · review ${d.review_status}` : "not generated"}>
                      {!d ? "＋" : d.status === "completed" ? "✓" : d.status === "failed" ? "⚠" : "…"}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="min-w-0 flex-1">
            {tab === "Generation" ? (
              <GenerationTab projectId={projectId} projName={proj.project_name} docType={docType}
                             job={currentJob} refreshDocs={refreshDocs}
                             onGenerated={() => setTabIdx(TABS.indexOf("Preview"))} />
            ) : (
              <PreviewTab projectId={projectId} docType={docType} job={currentJob} refreshDocs={refreshDocs} />
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/* ═══════════════ Data tab — ingested + derived, editable ═══════════════ */

function DataTab({ projectId }) {
  const toast = useToast();
  const [data, setData] = useState(null);
  const [edit, setEdit] = useState(false);
  const [draft, setDraft] = useState({});
  const [saving, setSaving] = useState(false);

  const load = useCallback(() => {
    api.get(`/projects/${projectId}/data`).then(setData).catch(() => {});
  }, [projectId]);
  useEffect(() => { load(); }, [load]);

  if (!data) return <Spinner label="Loading data…" />;

  async function save() {
    setSaving(true);
    try {
      const ing = {}, der = {};
      Object.entries(draft).forEach(([k, v]) => {
        if (k in (data.ingested || {})) ing[k] = v;
        else der[k] = v;
      });
      if (Object.keys(ing).length) await api.put(`/projects/${projectId}/data/ingested`, ing);
      if (Object.keys(der).length) await api.put(`/projects/${projectId}/data/derived`, der);
      toast("Data saved.", "ok");
      setEdit(false); setDraft({}); load();
    } catch (e) {
      toast(`Save failed: ${e.message}`, "err");
    } finally { setSaving(false); }
  }

  const Section = ({ title, obj, skip = [] }) => (
    <div className="card p-4">
      <h3 className="mb-3 text-sm font-bold text-gray-700">{title}</h3>
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {Object.entries(obj || {}).filter(([k, v]) => !skip.includes(k) && (typeof v === "string" || Array.isArray(v))).map(([k, v]) => (
          <div key={k}>
            <label className="label">{k.replace(/_/g, " ")}</label>
            {edit && typeof v === "string" ? (
              <textarea
                className="input h-16 resize-none"
                defaultValue={draft[k] ?? v}
                onChange={(e) => setDraft((d) => ({ ...d, [k]: e.target.value }))}
              />
            ) : (
              <div className="field-value min-h-9">
                {Array.isArray(v) ? v.map((s) => `${s.name} (${s.designation})`).join(", ") || "—" : v || "—"}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-end gap-2">
        {edit ? (
          <>
            <button className="btn-ghost" onClick={() => { setEdit(false); setDraft({}); }}>Cancel</button>
            <button className="btn-brand" disabled={saving} onClick={save}>{saving ? "Saving…" : "💾 Save data"}</button>
          </>
        ) : (
          <button className="btn-ghost" data-testid="data-edit" onClick={() => setEdit(true)}>✎ Edit data</button>
        )}
      </div>
      <Section title="Project Data (ingested)" obj={data.ingested} />
      <Section title="AI-Derived / Extended Fields" obj={data.derived} />
    </div>
  );
}

/* ═══════════════ Generation tab — chat + generate ═══════════════ */

function GenerationTab({ projectId, projName, docType, job, refreshDocs, onGenerated }) {
  const toast = useToast();
  const [msgs, setMsgs] = useState([]);
  const [input, setInput] = useState("");
  const [sessionId, setSessionId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [genStatus, setGenStatus] = useState(null);   // {done,total} while polling
  const boxRef = useRef(null);
  const pollRef = useRef(null);

  useEffect(() => {
    setMsgs([]); setSessionId(null); setGenStatus(null);
    clearInterval(pollRef.current);
    const key = `chat_${projectId}_${docType}`;
    const stored = localStorage.getItem(key);
    api.post("/chat/init", {
      project_id: projectId, document_type: docType.toLowerCase(),
      project_name: projName, session_id: stored || undefined,
    }).then((d) => {
      setSessionId(d.session_id);
      localStorage.setItem(key, d.session_id);
      if (d.content) setMsgs([{ role: "assistant", content: d.content }]);
    }).catch((e) => toast(`Chat init failed: ${e.message}`, "err"));
    return () => clearInterval(pollRef.current);
  }, [projectId, docType, projName, toast]);

  useEffect(() => { boxRef.current?.scrollTo(0, boxRef.current.scrollHeight); }, [msgs, genStatus]);

  function pollJob(jobId) {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const j = await api.get(`/generate/${jobId}`);
        setGenStatus({ done: j.completed_sections, total: j.total_sections, status: j.status });
        if (j.status === "completed" || j.status === "failed") {
          clearInterval(pollRef.current);
          setGenStatus(null);
          // Refresh the doc states FIRST so the Preview tab has the finished job,
          // then auto-switch to Preview on success.
          await refreshDocs();
          setMsgs((m) => [...m, {
            role: "assistant",
            content: j.status === "completed"
              ? `✅ ${docType} generated — ${j.total_sections} sections. Opening the Preview tab…`
              : `❌ Generation failed: ${j.error || "unknown error"}`,
          }]);
          if (j.status === "completed") onGenerated?.();
        }
      } catch { /* keep polling */ }
    }, 2500);
  }

  async function generateNow() {
    setBusy(true);
    setMsgs((m) => [...m, { role: "user", content: `Generate the ${docType}` }]);
    try {
      const d = await api.post(`/generate/project/${projectId}`, { document_type: docType });
      if (d.already_complete) {
        setMsgs((m) => [...m, { role: "assistant", content: "This document is already generated and up to date — see the Preview tab." }]);
        refreshDocs();
      } else {
        setMsgs((m) => [...m, { role: "assistant", content: `🚀 Generation started — ${d.total_sections} sections queued. The model is running…` }]);
        setGenStatus({ done: 0, total: d.total_sections, status: "in_progress" });
        pollJob(d.job_id);
      }
    } catch (e) {
      setMsgs((m) => [...m, { role: "assistant", content: `Error: ${e.message}` }]);
    } finally { setBusy(false); }
  }

  async function send() {
    const text = input.trim();
    if (!text || !sessionId || busy) return;
    setInput("");
    setMsgs((m) => [...m, { role: "user", content: text }]);
    setBusy(true);
    try {
      const d = await api.post("/chat/message", {
        session_id: sessionId, message: text,
        project_id: projectId, document_type: docType.toLowerCase(),
      });
      setMsgs((m) => [...m, { role: "assistant", content: d.content || "(no response)" }]);
      if (d.data?.job_id) pollJob(d.data.job_id);
      refreshDocs();
    } catch (e) {
      setMsgs((m) => [...m, { role: "assistant", content: `Error: ${e.message}` }]);
    } finally { setBusy(false); }
  }

  return (
    <div className="card flex h-[calc(100vh-190px)] flex-col">
      <div className="flex items-center justify-between border-b border-gray-100 px-4 py-2.5">
        <span className="text-sm font-bold text-gray-700">💬 {docType} — Generation chat</span>
        <div className="flex items-center gap-2">
          {job?.status === "completed" && <StatusChip status="completed" />}
          <button className="btn-brand" data-testid="generate-now" disabled={busy || !!genStatus} onClick={generateNow}>
            ⚡ Generate {docType}
          </button>
        </div>
      </div>

      <div ref={boxRef} className="flex-1 overflow-y-auto px-4 py-3">
        {msgs.map((m, i) => (
          <div key={i} className={`mb-2 flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
            <div className={`max-w-[75%] whitespace-pre-wrap rounded-2xl px-3.5 py-2 text-sm ${
              m.role === "user" ? "bg-brand-500 text-white" : "bg-gray-100 text-gray-800"
            }`}>{m.content}</div>
          </div>
        ))}
        {genStatus && (
          <div className="mx-auto my-3 w-72 rounded-xl border border-brand-200 bg-brand-50 px-4 py-3 text-center">
            <div className="text-xs font-bold text-brand-700">🤖 Model is running…</div>
            <div className="mt-2 h-2 overflow-hidden rounded bg-white">
              <div className="h-full bg-brand-400 transition-all"
                   style={{ width: `${genStatus.total ? (genStatus.done / genStatus.total) * 100 : 5}%` }} />
            </div>
            <div className="mt-1 text-[11px] text-brand-600">{genStatus.done}/{genStatus.total} sections</div>
          </div>
        )}
      </div>

      <div className="flex gap-2 border-t border-gray-100 p-3">
        <input
          className="input" data-testid="chat-input"
          placeholder={`Describe what you want in the ${docType}, or type "generate"…`}
          value={input} onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
        />
        <button className="btn-brand" onClick={send} disabled={busy || !sessionId}>➤</button>
      </div>
    </div>
  );
}

/* ═══════════════ Preview tab — word-like doc, edit, versions, invite ═══════════════ */

function PreviewTab({ projectId, docType, job, refreshDocs }) {
  const toast = useToast();
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [editSec, setEditSec] = useState(null);      // {section_id, title, content}
  const [versionsOpen, setVersionsOpen] = useState(false);
  const [snapshots, setSnapshots] = useState([]);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [savedAt, setSavedAt] = useState(null);
  const [validation, setValidation] = useState(null);   // report from /validate
  const [validating, setValidating] = useState(false);

  const jobId = job?.job_id;

  const load = useCallback(async () => {
    if (!jobId) { setPreview(null); return; }
    setLoading(true);
    try { setPreview(await api.get(`/generate/${jobId}/preview`)); }
    catch (e) { toast(`Preview failed: ${e.message}`, "err"); }
    finally { setLoading(false); }
  }, [jobId, toast]);
  // NB: wrap the async load — passing it directly as the effect would return a
  // Promise, which React would try to call as the cleanup ("destroy is not a
  // function") on unmount and break the Preview tab.
  useEffect(() => { load(); }, [load]);

  if (!jobId) {
    return (
      <div className="card px-4 py-14 text-center text-sm text-gray-400">
        {docType} hasn't been generated yet — go to the <b>Generation</b> tab and click ⚡ Generate.
      </div>
    );
  }

  async function saveSection() {
    try {
      await api.patch(`/sections/${editSec.section_id}`, { content: editSec.content });
      toast("Section saved — new version created.", "ok");
      setSavedAt(new Date());
      setEditSec(null);
      load();
    } catch (e) { toast(`Save failed: ${e.message}`, "err"); }
  }

  async function saveVersion() {
    const label = prompt("Version label:", `Checkpoint ${new Date().toLocaleString()}`);
    if (label === null) return;
    try {
      await api.post(`/generate/${jobId}/snapshot`, { label });
      toast("Version checkpoint saved.", "ok");
    } catch (e) { toast(`Snapshot failed: ${e.message}`, "err"); }
  }

  async function openVersions() {
    try {
      const d = await api.get(`/generate/${jobId}/snapshots`);
      setSnapshots(d.snapshots || []);
      setVersionsOpen(true);
    } catch (e) { toast(e.message, "err"); }
  }

  async function restore(snap) {
    if (!confirm(`Restore version "${snap.label}"?`)) return;
    try {
      await api.post(`/generate/${jobId}/snapshot/${snap.snapshot_id}/restore`);
      toast("Version restored.", "ok");
      setVersionsOpen(false);
      load();
    } catch (e) { toast(`Restore failed: ${e.message}`, "err"); }
  }

  function exportDoc(fmt) {
    window.open(`/api/generate/${jobId}/export?format=${fmt}`, "_blank");
  }

  async function runValidation() {
    setValidating(true);
    try {
      const report = await api.post(`/generate/${jobId}/validate`);
      setValidation(report);
    } catch (e) {
      toast(`Validation failed: ${e.message}`, "err");
    } finally {
      setValidating(false);
    }
  }

  return (
    <div>
      {/* toolbar: Edit · Save · autosave note · versions · invite · export */}
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <span className="text-sm font-bold text-gray-700">📄 {docType} preview</span>
        <button className="btn-ghost px-2.5" title="Version history" onClick={openVersions}>🕐</button>
        {savedAt && <span className="text-xs font-semibold text-emerald-600">✓ saved {savedAt.toLocaleTimeString()}</span>}
        <div className="flex-1" />
        <button className="btn-ghost" title="Save a version checkpoint" onClick={saveVersion}>📌 Save version</button>
        <button className="btn-ghost" title="Quality score + source-document traceability"
                data-testid="validate-doc" disabled={validating} onClick={runValidation}>
          {validating ? "⏳ Validating…" : "🛡 Validate"}
        </button>
        <button className="btn-ghost" title="Invite people to review" data-testid="invite-review" onClick={() => setInviteOpen(true)}>👥 Invite for review</button>
        <button className="btn-ghost" title="Export as Word" onClick={() => exportDoc("docx")}>⬇ Word</button>
        <button className="btn-ghost" title="Export as PDF" onClick={() => exportDoc("pdf")}>⬇ PDF</button>
      </div>

      {loading ? (
        <Spinner label="Assembling preview…" />
      ) : (
        <div className="max-h-[calc(100vh-240px)] overflow-y-auto rounded-xl bg-gray-200/70 p-5">
          <div className="doc-page">
            {(preview?.sections || []).map((s) => (
              <div key={s.section_id} className="group relative mb-2 rounded px-1 hover:bg-brand-50/50">
                <button
                  className="absolute -right-1 top-1 hidden rounded bg-brand-500 px-2 py-0.5 text-[11px] font-bold text-white group-hover:block"
                  onClick={() => setEditSec({ section_id: s.section_id, title: s.title, content: s.content })}
                >✎ Edit</button>
                <div dangerouslySetInnerHTML={{ __html: mdToHtml(`## ${s.title}\n${s.content || "_(empty)_"}`) }} />
              </div>
            ))}
            {(preview?.sections || []).length === 0 && (
              <p className="text-center text-gray-400">No content yet.</p>
            )}
          </div>
        </div>
      )}

      {/* section editor */}
      <Modal open={!!editSec} onClose={() => setEditSec(null)} title={`Edit — ${editSec?.title}`} wide>
        <textarea
          className="input h-80 font-mono text-xs"
          value={editSec?.content || ""}
          onChange={(e) => setEditSec((s) => ({ ...s, content: e.target.value }))}
        />
        <div className="mt-3 flex justify-end gap-2">
          <button className="btn-ghost" onClick={() => setEditSec(null)}>Cancel</button>
          <button className="btn-brand" onClick={saveSection}>💾 Save</button>
        </div>
      </Modal>

      {/* version history */}
      <Modal open={versionsOpen} onClose={() => setVersionsOpen(false)} title="🕐 Version history">
        {snapshots.length === 0 && <p className="py-4 text-center text-sm text-gray-400">No saved versions yet.</p>}
        {snapshots.map((s) => (
          <div key={s.snapshot_id} className="flex items-center justify-between border-b border-gray-100 py-2.5">
            <div>
              <div className="text-sm font-semibold text-gray-800">{s.label}</div>
              <div className="text-[11px] text-gray-400">{timeAgo(s.created_at)} · {s.trigger_type}</div>
            </div>
            <button className="btn-ghost text-xs" onClick={() => restore(s)}>↩ Restore</button>
          </div>
        ))}
      </Modal>

      <InviteModal open={inviteOpen} onClose={() => setInviteOpen(false)} jobId={jobId} refreshDocs={refreshDocs} />

      {/* validation report */}
      <Modal open={!!validation} onClose={() => setValidation(null)} title="🛡 Document validation report" wide>
        {validation && <ValidationReport report={validation} />}
      </Modal>
    </div>
  );
}

function ValidationReport({ report }) {
  const scoreColor = report.score >= 80 ? "text-emerald-600" : report.score >= 60 ? "text-amber-600" : "text-red-600";
  const metricLabels = {
    correctness: "Correctness / grounding", completeness: "Completeness",
    format: "Format compliance", edge_cases: "Edge cases", robustness: "Robustness",
  };
  return (
    <div className="text-sm">
      {/* score header */}
      <div className="mb-4 flex items-center gap-4">
        <div className={`text-4xl font-extrabold ${scoreColor}`}>{report.score}</div>
        <div>
          <div className={`text-sm font-bold ${report.passed ? "text-emerald-600" : "text-red-600"}`}>
            {report.passed ? "✓ PASS" : "✕ FAIL"} <span className="text-gray-400 font-normal">(bar: ≥ 80, no critical findings)</span>
          </div>
          <div className="text-xs text-gray-500">{report.document_type} · {(report.provenance || []).filter(p => p.grounded).length}/{(report.provenance || []).length} sections traced to attached documents</div>
        </div>
      </div>

      {/* metric bars */}
      <div className="mb-4 grid grid-cols-1 gap-1.5">
        {Object.entries(report.metrics || {}).map(([k, v]) => (
          <div key={k} className="flex items-center gap-2">
            <span className="w-44 text-xs font-semibold text-gray-500">{metricLabels[k] || k}</span>
            <div className="h-2 flex-1 overflow-hidden rounded bg-gray-100">
              <div className={`h-full ${v >= 80 ? "bg-emerald-400" : v >= 60 ? "bg-amber-400" : "bg-red-400"}`}
                   style={{ width: `${v}%` }} />
            </div>
            <span className="w-9 text-right text-xs text-gray-500">{Math.round(v)}</span>
          </div>
        ))}
      </div>

      {/* source documents */}
      {(report.source_documents || []).length > 0 && (
        <div className="mb-4">
          <div className="label">Attached source documents</div>
          {report.source_documents.map((s, i) => (
            <div key={i} className="flex items-center gap-2 text-xs text-gray-600">
              📎 <b>{s.name}</b> <span className="text-gray-400 break-all">{s.path}</span>
            </div>
          ))}
        </div>
      )}

      {/* provenance table */}
      {(report.provenance || []).length > 0 && (
        <div className="mb-4">
          <div className="label">Section provenance — where each section's content came from</div>
          <div className="max-h-56 overflow-y-auto rounded-lg border border-gray-200">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-gray-50 text-left text-[10px] uppercase text-gray-400">
                <tr><th className="px-2 py-1.5">Section</th><th className="px-2 py-1.5">Origin</th><th className="px-2 py-1.5">Source document (path)</th><th className="px-2 py-1.5">Support</th></tr>
              </thead>
              <tbody>
                {report.provenance.map((p, i) => (
                  <tr key={i} className="border-t border-gray-100">
                    <td className="px-2 py-1.5 font-semibold text-gray-700">{p.field}</td>
                    <td className="px-2 py-1.5">
                      {p.grounded
                        ? <span className="rounded bg-emerald-50 px-1.5 py-0.5 font-bold text-emerald-600">📎 document</span>
                        : <span className="rounded bg-gray-100 px-1.5 py-0.5 text-gray-500">form / AI-derived</span>}
                    </td>
                    <td className="px-2 py-1.5 text-gray-500">
                      {p.grounded ? <><b>{p.source_name}</b><br /><span className="break-all text-[10px] text-gray-400">{p.source_path}</span></> : "—"}
                    </td>
                    <td className="px-2 py-1.5 text-gray-500">{Math.round(p.support * 100)}%</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* findings */}
      {(report.findings || []).length > 0 && (
        <div>
          <div className="label">Findings ({report.findings.length})</div>
          {report.findings.map((f, i) => (
            <div key={i} className="mb-1 flex items-start gap-2 text-xs">
              <span className={`mt-0.5 rounded px-1.5 py-0.5 text-[10px] font-bold ${
                f.severity === "CRITICAL" ? "bg-red-100 text-red-600"
                : f.severity === "MAJOR" ? "bg-amber-100 text-amber-700"
                : "bg-gray-100 text-gray-500"}`}>{f.severity}</span>
              <span className="text-gray-600"><b>{f.path}</b>: {f.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function InviteModal({ open, onClose, jobId, refreshDocs }) {
  const toast = useToast();
  const [emails, setEmails] = useState("");
  const [message, setMessage] = useState("");
  const [shared, setShared] = useState([]);

  useEffect(() => {
    if (!open) return;
    api.get("/review/sent").then((d) => {
      const mine = (d.reviews || []).filter((r) => r.job_id === jobId);
      setShared(mine.flatMap((r) => r.reviewers || []));
    }).catch(() => {});
  }, [open, jobId]);

  async function share() {
    const list = emails.split(",").map((e) => e.trim().toLowerCase()).filter(Boolean);
    if (!list.length) { toast("Enter at least one email.", "err"); return; }
    try {
      await api.post("/review/share", {
        job_id: jobId, reviewers: list.map((email) => ({ email })), message: message.trim(),
      });
      toast(`Shared with ${list.length} reviewer(s) — they've been notified in-app.`, "ok");
      setEmails(""); setMessage("");
      refreshDocs();
      onClose();
    } catch (e) { toast(`Share failed: ${e.message}`, "err"); }
  }

  return (
    <Modal open={open} onClose={onClose} title="👥 Invite for review">
      <label className="label">Reviewer emails (comma-separated)</label>
      <input className="input mb-3" data-testid="invite-emails" placeholder="a@adani.com, b@adani.com"
             value={emails} onChange={(e) => setEmails(e.target.value)} />
      <label className="label">Note to reviewers (optional)</label>
      <textarea className="input mb-3 h-16" value={message} onChange={(e) => setMessage(e.target.value)} />
      {shared.length > 0 && (
        <div className="mb-3">
          <div className="label">Already shared with</div>
          <div className="flex flex-wrap gap-2">
            {shared.map((a, i) => (
              <span key={i} className="flex items-center gap-1 text-xs text-gray-600">
                {a.name || a.email} <StatusChip status={a.status} />
              </span>
            ))}
          </div>
        </div>
      )}
      <div className="flex justify-end gap-2">
        <button className="btn-ghost" onClick={onClose}>Cancel</button>
        <button className="btn-brand" data-testid="invite-send" onClick={share}>📤 Share</button>
      </div>
    </Modal>
  );
}
