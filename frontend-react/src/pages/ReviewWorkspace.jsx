import { useCallback, useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, getUser, mdToHtml, timeAgo } from "../api.js";
import { Modal, Spinner, StatusChip, useToast } from "../components/ui.jsx";

// Full-page review workspace (Figma): document preview left, persona AI review
// + comments right, verdict icon row bottom-right.
export default function ReviewWorkspace() {
  const { reviewId } = useParams();
  const nav = useNavigate();
  const toast = useToast();
  const me = getUser();

  const [ws, setWs] = useState(null);
  const [personas, setPersonas] = useState([]);
  const [persona, setPersona] = useState("Project Manager");
  const [personaMgrOpen, setPersonaMgrOpen] = useState(false);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiResult, setAiResult] = useState(null);
  const [comment, setComment] = useState("");
  const [commentSec, setCommentSec] = useState("");
  const [editSec, setEditSec] = useState(null);
  const [summBusy, setSummBusy] = useState(false);

  const load = useCallback(() => {
    api.get(`/review/${reviewId}`).then(setWs).catch((e) => toast(`Load failed: ${e.message}`, "err"));
  }, [reviewId, toast]);

  useEffect(() => {
    load();
    api.get("/personas").then((d) => setPersonas(d.personas || [])).catch(() => {});
  }, [load]);

  if (!ws) return <Spinner label="Opening review…" />;

  const isAuthor = (ws.requested_by?.email || "") === me.email;
  const myAssign = (ws.reviewers || []).find((a) => a.email === me.email);

  /* ── actions ─────────────────────────────────────────── */

  async function runAiReview() {
    setAiBusy(true); setAiResult(null);
    try {
      const d = await api.post(`/review/${reviewId}/ai-review`, { persona });
      setAiResult({ ...d, selected: new Set(d.section_comments.map((_, i) => i)) });
    } catch (e) { toast(`AI review failed: ${e.message}`, "err"); }
    finally { setAiBusy(false); }
  }

  async function keepSelected() {
    const chosen = aiResult.section_comments.filter((_, i) => aiResult.selected.has(i))
      .map((c) => ({ section_id: c.section_id, comment: c.comment }));
    if (!chosen.length) { toast("Nothing selected.", "err"); return; }
    try {
      const d = await api.post(`/review/${reviewId}/ai-review/keep`, { persona: aiResult.persona, comments: chosen });
      toast(`${d.count} AI comment(s) saved.`, "ok");
      setAiResult(null);
      load();
    } catch (e) { toast(`Keep failed: ${e.message}`, "err"); }
  }

  async function addComment() {
    if (!comment.trim()) return;
    try {
      await api.post(`/review/${reviewId}/comments`, { text: comment.trim(), section_id: commentSec || null });
      setComment(""); setCommentSec("");
      toast("Comment added.", "ok");
      load();
    } catch (e) { toast(`Comment failed: ${e.message}`, "err"); }
  }

  async function verdict(action) {
    const labels = { accepted: "approve", rejected: "reject", revision_requested: "request revisions on" };
    if (!confirm(`Are you sure you want to ${labels[action]} this document?`)) return;
    try {
      const d = await api.post(`/review/${reviewId}/respond`, { action });
      toast(`Recorded — overall status: ${d.review_status.replace(/_/g, " ")}`, "ok");
      load();
    } catch (e) { toast(e.message, "err"); }
  }

  async function applyComment(c) {
    if (!confirm("Apply this feedback? The section will be regenerated and saved as a new version.")) return;
    try {
      const d = await api.post(`/review/comments/${c.comment_id}/apply`, {});
      toast(`Applied — section regenerated (v${d.new_version?.version_number ?? "?"}).`, "ok");
      load();
    } catch (e) { toast(`Apply failed: ${e.message}`, "err"); }
  }

  async function saveSectionEdit() {
    try {
      await api.patch(`/sections/${editSec.section_id}`, { content: editSec.content });
      toast("Section saved.", "ok");
      setEditSec(null);
      load();
    } catch (e) { toast(`Save failed: ${e.message}`, "err"); }
  }

  async function summarize(force) {
    setSummBusy(true);
    try {
      await api.post(`/review/${reviewId}/summarize`, { force });
      toast("Summaries updated.", "ok");
      load();
    } catch (e) { toast(e.message, "err"); }
    finally { setSummBusy(false); }
  }

  function download(fmt) {
    window.open(`/api/generate/${ws.job_id}/export?format=${fmt}`, "_blank");
  }

  /* ── render ──────────────────────────────────────────── */

  return (
    <div className="flex h-[calc(100vh-48px)]">
      {/* ══ LEFT: word-like document preview (full height) ══ */}
      <div className="flex min-w-0 flex-1 flex-col border-r border-gray-200">
        <div className="flex items-center gap-3 border-b border-gray-100 bg-white px-4 py-2">
          <button className="btn-ghost px-2.5" onClick={() => nav("/?tab=review")} title="Back to Review">←</button>
          <span className="text-sm font-bold text-gray-800">
            Review: {ws.project_name || ""} · {ws.document_type || "Document"}
          </span>
          <StatusChip status={ws.review_status} />
          <span className="text-[11px] text-gray-400">
            from {ws.requested_by?.name || ws.requested_by?.email} · {timeAgo(ws.created_at)}
          </span>
        </div>
        <div className="flex-1 overflow-y-auto bg-gray-200/70 p-5">
          <div className="doc-page">
            {(ws.sections || []).map((s) => (
              <div key={s.section_id} className="group relative mb-2 rounded px-1 hover:bg-brand-50/40">
                <button
                  className="absolute -right-1 top-1 hidden rounded bg-brand-500 px-2 py-0.5 text-[11px] font-bold text-white group-hover:block"
                  onClick={() => setEditSec({ section_id: s.section_id, title: s.section_title, content: s.content })}
                >✎ Edit</button>
                <div dangerouslySetInnerHTML={{ __html: mdToHtml(`## ${s.section_title}\n${s.content || ""}`) }} />
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* ══ RIGHT: persona AI review · comments · verdict icons ══ */}
      <div className="flex w-[380px] shrink-0 flex-col bg-white">
        {/* persona row + settings (Figma: dropdown + Generate in same row, ⚙ manages personas) */}
        <div className="border-b border-gray-100 p-3">
          <div className="mb-2 flex items-center justify-between">
            <span className="text-xs font-bold uppercase tracking-wide text-gray-500">Review Persona</span>
            <button className="rounded p-1 text-gray-400 hover:bg-gray-100" title="Manage personas"
                    onClick={() => setPersonaMgrOpen(true)}>⚙</button>
          </div>
          <div className="flex gap-2">
            <select className="input" data-testid="persona-select" value={persona} onChange={(e) => setPersona(e.target.value)}>
              {(personas.length ? personas.map((p) => p.name) : ["Project Manager"]).map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
            <button className="btn-brand whitespace-nowrap" data-testid="ai-review-run" disabled={aiBusy} onClick={runAiReview}>
              {aiBusy ? "…" : "🤖 Generate AI Review"}
            </button>
          </div>
        </div>

        {/* scrollable middle: AI result → summaries → comments */}
        <div className="flex-1 overflow-y-auto p-3">
          {aiBusy && <Spinner label={`Reviewing as ${persona}…`} />}

          {aiResult && (
            <div className="mb-3 rounded-xl border border-brand-200 bg-brand-50 p-3">
              <div className="mb-1 text-xs font-bold text-brand-700">{aiResult.persona} — overall AI review</div>
              <p className="text-sm text-gray-700">{aiResult.summary}</p>
              {aiResult.section_comments.length > 0 && (
                <div className="mt-2 border-t border-brand-100 pt-2">
                  {aiResult.section_comments.map((c, i) => (
                    <label key={i} className="mb-2 flex items-start gap-2 text-xs">
                      <input
                        type="checkbox" className="mt-0.5"
                        checked={aiResult.selected.has(i)}
                        onChange={() => setAiResult((r) => {
                          const sel = new Set(r.selected);
                          sel.has(i) ? sel.delete(i) : sel.add(i);
                          return { ...r, selected: sel };
                        })}
                      />
                      <span>
                        <b>{c.section_title}</b>{" "}
                        <span className={`rounded px-1.5 text-[10px] font-bold uppercase ${
                          c.severity === "high" ? "bg-red-100 text-red-600"
                          : c.severity === "low" ? "bg-gray-100 text-gray-500"
                          : "bg-amber-100 text-amber-700"}`}>{c.severity}</span>
                        <br />{c.comment}
                      </span>
                    </label>
                  ))}
                  <button className="btn-brand mt-1 w-full justify-center text-xs" onClick={keepSelected}>
                    💾 Keep selected as comments
                  </button>
                </div>
              )}
            </div>
          )}

          {/* Author: persona-wise feedback summaries */}
          {isAuthor && (
            <div className="mb-3">
              <div className="mb-1 flex items-center justify-between">
                <span className="text-xs font-bold uppercase tracking-wide text-gray-500">Feedback summaries</span>
                <button className="text-xs font-semibold text-brand-600 hover:underline" disabled={summBusy}
                        onClick={() => summarize(false)}>
                  {summBusy ? "Summarizing…" : "↻ Summarize"}
                </button>
              </div>
              {(ws.ai_summaries || []).map((s) => (
                <div key={s.summary_id} className="mb-2 rounded-lg bg-gray-50 p-2.5 text-xs text-gray-600">
                  <b className="text-brand-600">{s.persona}:</b> {s.summary}
                </div>
              ))}
              {(ws.ai_summaries || []).length === 0 && (
                <p className="text-xs text-gray-400">No summaries yet — click Summarize once comments arrive.</p>
              )}
            </div>
          )}

          {/* Comments thread */}
          <div className="mb-1 text-xs font-bold uppercase tracking-wide text-gray-500">
            Comments ({(ws.comments || []).length})
          </div>
          {(ws.comments || []).map((c) => (
            <CommentCard key={c.comment_id} c={c} isAuthor={isAuthor} onApply={applyComment} />
          ))}

          {/* Add comment */}
          <div className="mt-2 rounded-lg border border-gray-200 p-2">
            <select className="input mb-1.5 text-xs" value={commentSec} onChange={(e) => setCommentSec(e.target.value)}>
              <option value="">Whole document</option>
              {(ws.sections || []).map((s) => (
                <option key={s.section_id} value={s.section_id}>{s.section_title}</option>
              ))}
            </select>
            <textarea
              className="input h-14 text-xs" data-testid="comment-input" placeholder="Add a comment…"
              value={comment} onChange={(e) => setComment(e.target.value)}
            />
            <button className="btn-brand mt-1.5 w-full justify-center text-xs" data-testid="comment-add" onClick={addComment}>
              ＋ Add
            </button>
          </div>
        </div>

        {/* bottom icon row: Save · Reject · Request revision · Approve · Download */}
        <div className="flex items-center justify-end gap-1.5 border-t border-gray-100 p-2.5">
          <button className="btn-ghost px-2.5" title="Save (edits save automatically per section)" onClick={() => toast("Section edits are saved as versions automatically.", "info")}>💾</button>
          {myAssign && (
            <>
              <button className="btn-danger px-2.5" title="Reject" data-testid="verdict-reject" onClick={() => verdict("rejected")}>✕</button>
              <button className="btn-ghost px-2.5" title="Request revision" onClick={() => verdict("revision_requested")}>✎</button>
              <button className="btn-brand px-2.5" title="Approve" data-testid="verdict-approve" onClick={() => verdict("accepted")}>✓</button>
            </>
          )}
          <DownloadMenu onPick={download} />
        </div>
      </div>

      {/* section edit modal */}
      <Modal open={!!editSec} onClose={() => setEditSec(null)} title={`Edit — ${editSec?.title}`} wide>
        <textarea className="input h-80 font-mono text-xs" value={editSec?.content || ""}
                  onChange={(e) => setEditSec((s) => ({ ...s, content: e.target.value }))} />
        <div className="mt-3 flex justify-end gap-2">
          <button className="btn-ghost" onClick={() => setEditSec(null)}>Cancel</button>
          <button className="btn-brand" onClick={saveSectionEdit}>💾 Save</button>
        </div>
      </Modal>

      <PersonaManager open={personaMgrOpen} onClose={() => setPersonaMgrOpen(false)}
                      personas={personas} refresh={() => api.get("/personas").then((d) => setPersonas(d.personas || []))} />
    </div>
  );
}

function CommentCard({ c, isAuthor, onApply }) {
  return (
    <div className={`mb-2 rounded-lg border border-gray-200 p-2.5 text-xs ${c.status === "resolved" ? "opacity-50" : ""}`}>
      <div className="mb-1 text-[11px] text-gray-400">
        <b className="text-gray-600">{c.author?.name || c.author?.email}</b>
        {c.source === "ai" && <span className="ml-1 rounded bg-brand-100 px-1 text-[10px] font-bold text-brand-600">AI · {c.persona}</span>}
        {c.section_title && <> · on <b>{c.section_title}</b></>}
        {" · "}{timeAgo(c.created_at)}
        {c.status === "resolved" && " · ✓ resolved"}
      </div>
      <div className="text-gray-700">{c.text}</div>
      {isAuthor && c.status !== "resolved" && c.section_id && (
        <button className="mt-1.5 text-[11px] font-bold text-brand-600 hover:underline" onClick={() => onApply(c)}>
          📌 Apply to section
        </button>
      )}
      {(c.replies || []).map((r) => (
        <div key={r.comment_id} className="ml-3 mt-2 border-l-2 border-gray-200 pl-2">
          <div className="text-[11px] text-gray-400"><b className="text-gray-600">{r.author?.name || r.author?.email}</b> · {timeAgo(r.created_at)}</div>
          <div className="text-gray-700">{r.text}</div>
        </div>
      ))}
    </div>
  );
}

function DownloadMenu({ onPick }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative">
      <button className="btn-ghost px-2.5" title="Download" onClick={() => setOpen(!open)}>⬇</button>
      {open && (
        <div className="absolute bottom-10 right-0 z-30 w-40 overflow-hidden rounded-lg border border-gray-200 bg-white shadow-xl">
          <button className="block w-full px-3 py-2 text-left text-xs font-semibold hover:bg-gray-50" onClick={() => { setOpen(false); onPick("docx"); }}>Export as Word</button>
          <button className="block w-full px-3 py-2 text-left text-xs font-semibold hover:bg-gray-50" onClick={() => { setOpen(false); onPick("pdf"); }}>Export as PDF</button>
        </div>
      )}
    </div>
  );
}

function PersonaManager({ open, onClose, personas, refresh }) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");

  async function add() {
    if (!name.trim()) return;
    try {
      await api.post("/personas", { name: name.trim(), description: desc.trim() });
      toast("Persona saved.", "ok");
      setName(""); setDesc("");
      refresh();
    } catch (e) { toast(e.message, "err"); }
  }

  async function remove(p) {
    try { await api.del(`/personas/${p.persona_id}`); refresh(); }
    catch (e) { toast(e.message, "err"); }
  }

  return (
    <Modal open={open} onClose={onClose} title="⚙ Review personas">
      {personas.map((p) => (
        <div key={p.persona_id} className="flex items-start justify-between border-b border-gray-100 py-2">
          <div>
            <div className="text-sm font-bold text-gray-800">{p.name} {p.is_system && <span className="text-[10px] font-semibold text-gray-400">system</span>}</div>
            <div className="text-xs text-gray-500">{p.description}</div>
          </div>
          {!p.is_system && <button className="btn-danger px-2 text-xs" onClick={() => remove(p)}>🗑</button>}
        </div>
      ))}
      <div className="mt-3 rounded-lg bg-gray-50 p-3">
        <div className="label">Add custom persona</div>
        <input className="input mb-2" placeholder="Persona name" value={name} onChange={(e) => setName(e.target.value)} />
        <textarea className="input mb-2 h-14" placeholder="Reviewing style / focus…" value={desc} onChange={(e) => setDesc(e.target.value)} />
        <button className="btn-brand w-full justify-center text-xs" onClick={add}>💾 Save persona</button>
      </div>
    </Modal>
  );
}
