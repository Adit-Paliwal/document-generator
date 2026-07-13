import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, timeAgo } from "../api.js";
import { Accordion, KpiCards, Spinner, StatusChip, useToast } from "../components/ui.jsx";

export default function Dashboard() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") || "projects";
  const [stats, setStats] = useState(null);

  useEffect(() => {
    api.get("/projects/stats").then(setStats).catch(() => {});
  }, [tab]);

  return (
    <div className="mx-auto max-w-6xl px-4 py-4">
      {/* KPI cards — common to both tabs, order per Figma */}
      <KpiCards stats={stats} />

      {/* Nav tabs UNDER the KPI cards (Figma msg#291) */}
      <div className="mt-4 flex items-center gap-1 border-b border-gray-200">
        {["projects", "review"].map((t) => (
          <button
            key={t}
            data-testid={`tab-${t}`}
            onClick={() => setParams({ tab: t })}
            className={`-mb-px border-b-2 px-4 py-2 text-sm font-bold capitalize ${
              tab === t ? "border-brand-500 text-brand-600" : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
          >
            {t}
          </button>
        ))}
        <div className="flex-1" />
        <CreateButton />
      </div>

      <div className="mt-4">{tab === "projects" ? <ProjectsTab /> : <ReviewTab />}</div>
    </div>
  );
}

function CreateButton() {
  const nav = useNavigate();
  return (
    <button className="btn-brand mb-1" data-testid="create-project" onClick={() => nav("/create")}>
      + Create New Project
    </button>
  );
}

/* ═══════════════ Projects tab ═══════════════ */

const COLUMNS = [
  { key: "project_name",  label: "Project" },
  { key: "project_code",  label: "Code" },
  { key: "business_unit", label: "Department" },
  { key: "review_status", label: "Status" },
  { key: "document_count", label: "Docs" },
  { key: "updated_at",    label: "Updated" },
];

function ProjectsTab() {
  const nav = useNavigate();
  const [rows, setRows] = useState(null);
  const [q, setQ] = useState("");
  const [status, setStatus] = useState("");
  const [dept, setDept] = useState("");
  const [sort, setSort] = useState({ key: "updated_at", dir: -1 });

  useEffect(() => {
    const t = setTimeout(() => {
      const qs = new URLSearchParams({ per_page: "100" });
      if (q.trim()) qs.set("q", q.trim());
      if (dept) qs.set("business_unit", dept);
      if (status) qs.set("review_status", status);
      api.get(`/projects?${qs}`).then((d) => setRows(d.projects || [])).catch(() => setRows([]));
    }, 250);
    return () => clearTimeout(t);
  }, [q, status, dept]);

  const depts = useMemo(
    () => [...new Set((rows || []).map((r) => r.business_unit).filter(Boolean))].sort(),
    [rows]
  );

  const sorted = useMemo(() => {
    if (!rows) return null;
    return [...rows].sort((a, b) => {
      const va = a[sort.key] ?? "", vb = b[sort.key] ?? "";
      return (va > vb ? 1 : va < vb ? -1 : 0) * sort.dir;
    });
  }, [rows, sort]);

  return (
    <div>
      {/* compact search & filter bar */}
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <input
          className="input max-w-xs" data-testid="proj-search"
          placeholder="Search name or code…" value={q} onChange={(e) => setQ(e.target.value)}
        />
        <select className="input w-auto" value={status} onChange={(e) => setStatus(e.target.value)} data-testid="proj-status-filter">
          <option value="">All statuses</option>
          <option value="under_draft">Under Draft</option>
          <option value="under_review">Under Review</option>
          <option value="approved">Approved</option>
        </select>
        <select className="input w-auto" value={dept} onChange={(e) => setDept(e.target.value)}>
          <option value="">All departments</option>
          {depts.map((d) => <option key={d} value={d}>{d}</option>)}
        </select>
      </div>

      {!sorted ? (
        <Spinner label="Loading projects…" />
      ) : (
        <div className="card overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-gray-200 text-left text-[11px] uppercase tracking-wide text-gray-400">
                {COLUMNS.map((c) => (
                  <th key={c.key} className="px-4 py-2.5">
                    <button
                      className="flex items-center gap-1 font-bold hover:text-gray-600"
                      onClick={() => setSort((s) => ({ key: c.key, dir: s.key === c.key ? -s.dir : 1 }))}
                    >
                      {c.label}
                      <span className="text-gray-300">{sort.key === c.key ? (sort.dir > 0 ? "▲" : "▼") : "⇅"}</span>
                    </button>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {sorted.length === 0 && (
                <tr><td colSpan={6} className="px-4 py-10 text-center text-gray-400">No projects found.</td></tr>
              )}
              {sorted.map((p) => (
                <tr
                  key={p.project_id} data-testid="proj-row"
                  className="cursor-pointer border-b border-gray-100 hover:bg-brand-50/40"
                  onClick={() => nav(`/project/${p.project_id}`)}
                >
                  <td className="px-4 py-2.5 font-semibold text-gray-800">{p.project_name || "Untitled"}</td>
                  <td className="px-4 py-2.5 text-gray-500">{p.project_code || "—"}</td>
                  <td className="px-4 py-2.5 text-gray-500">{p.business_unit || "—"}</td>
                  <td className="px-4 py-2.5"><StatusChip status={p.review_status} /></td>
                  <td className="px-4 py-2.5 text-gray-500">{p.document_count ?? 0}</td>
                  <td className="px-4 py-2.5 text-gray-400">{timeAgo(p.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

/* ═══════════════ Review tab — Sent / Received ═══════════════ */

function ReviewTab() {
  const [dir, setDir] = useState("received");
  const [reviews, setReviews] = useState(null);

  useEffect(() => {
    setReviews(null);
    api.get(`/review/${dir}`).then((d) => setReviews(d.reviews || [])).catch(() => setReviews([]));
  }, [dir]);

  // Group project → documents (each review row = one shared document)
  const byProject = useMemo(() => {
    const g = {};
    (reviews || []).forEach((r) => {
      const key = r.project_name || "Standalone documents";
      (g[key] = g[key] || []).push(r);
    });
    return g;
  }, [reviews]);

  return (
    <div>
      {/* squarish toggle per Figma feedback */}
      <div className="mb-3 inline-flex overflow-hidden rounded-md border border-gray-300">
        {["received", "sent"].map((d) => (
          <button
            key={d} data-testid={`review-${d}`}
            onClick={() => setDir(d)}
            className={`px-5 py-1.5 text-sm font-bold capitalize ${
              dir === d ? "bg-brand-500 text-white" : "bg-white text-gray-600 hover:bg-gray-50"
            }`}
          >
            {d}
          </button>
        ))}
      </div>

      {!reviews ? (
        <Spinner label="Loading reviews…" />
      ) : reviews.length === 0 ? (
        <div className="card px-4 py-10 text-center text-sm text-gray-400">
          {dir === "received" ? "Nothing has been shared with you for review yet." : "You haven't shared anything for review yet."}
        </div>
      ) : (
        <div className="flex flex-col gap-2">
          {Object.entries(byProject).map(([proj, docs]) => (
            <Accordion
              key={proj}
              header={
                <div className="flex items-center gap-3">
                  <span className="text-sm font-bold text-gray-800">{proj}</span>
                  <span className="text-xs text-gray-400">{docs.length} document(s)</span>
                </div>
              }
            >
              <div className="flex flex-col gap-2">
                {docs.map((r) => <ReviewDocRow key={r.review_id} r={r} dir={dir} />)}
              </div>
            </Accordion>
          ))}
        </div>
      )}
    </div>
  );
}

function ReviewDocRow({ r, dir }) {
  const nav = useNavigate();
  const toast = useToast();
  const [exportOpen, setExportOpen] = useState(false);

  async function doExport(fmt) {
    setExportOpen(false);
    try {
      const resp = await fetch(`/api/generate/${r.job_id}/export?format=${fmt}`);
      if (!resp.ok) throw new Error((await resp.json()).error || "Export failed");
      const ctype = resp.headers.get("content-type") || "";
      if (ctype.includes("application/json")) {
        const d = await resp.json();
        if (d.blob_url) { window.open(d.blob_url, "_blank"); return; }
      }
      const blob = await resp.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${r.document_type || "document"}.${fmt === "pdf" ? "pdf" : "docx"}`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      toast(`Export failed: ${e.message}`, "err");
    }
  }

  const status = dir === "received" ? r.my_status : (r.status === "completed" ? "completed" : "under_review");

  return (
    <div className="rounded-lg border border-gray-200 px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-3">
        <button
          className="text-sm font-bold text-brand-600 hover:underline"
          data-testid="open-review"
          onClick={() => nav(`/review/${r.review_id}`)}
        >
          📄 {r.document_type || "Document"}
        </button>
        <StatusChip status={status} />
        <span className="text-xs text-gray-400">
          Shared {r.days_since_shared === 0 ? "today" : `${r.days_since_shared}d ago`}
          {dir === "received" && r.from ? ` by ${r.from.name || r.from.email}` : ""}
        </span>
        <div className="relative ml-auto">
          <button className="btn-ghost px-2 py-1 text-xs" title="Export" onClick={() => setExportOpen(!exportOpen)}>⬇</button>
          {exportOpen && (
            <div className="absolute right-0 top-8 z-30 w-40 overflow-hidden rounded-lg border border-gray-200 bg-white shadow-xl">
              <button className="block w-full px-3 py-2 text-left text-xs font-semibold hover:bg-gray-50" onClick={() => doExport("docx")}>Export as Word</button>
              <button className="block w-full px-3 py-2 text-left text-xs font-semibold hover:bg-gray-50" onClick={() => doExport("pdf")}>Export as PDF</button>
            </div>
          )}
        </div>
      </div>
      {/* Sent view: reviewer list with per-reviewer status */}
      {dir === "sent" && (r.reviewers || []).length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2 border-t border-gray-100 pt-2">
          {r.reviewers.map((a) => (
            <span key={a.assignment_id} className="flex items-center gap-1.5 text-xs text-gray-600">
              <span className="font-semibold">{a.name || a.email}</span>
              <StatusChip status={a.status} />
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
