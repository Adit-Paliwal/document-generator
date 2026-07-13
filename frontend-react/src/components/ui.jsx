// Small shared UI atoms — status chips, KPI cards, modal, spinner, toasts.
import { createContext, useCallback, useContext, useState } from "react";

/* ── Status chip ────────────────────────────────────────────── */
const CHIP_STYLE = {
  under_draft:        "bg-gray-100 text-gray-600 border-gray-200",
  draft:              "bg-gray-100 text-gray-600 border-gray-200",
  under_review:       "bg-amber-50 text-amber-700 border-amber-200",
  reviewing:          "bg-brand-50 text-brand-600 border-brand-200",
  shared:             "bg-gray-100 text-gray-600 border-gray-200",
  approved:           "bg-emerald-50 text-emerald-700 border-emerald-200",
  accepted:           "bg-emerald-50 text-emerald-700 border-emerald-200",
  completed:          "bg-emerald-50 text-emerald-700 border-emerald-200",
  rejected:           "bg-red-50 text-red-600 border-red-200",
  failed:             "bg-red-50 text-red-600 border-red-200",
  revision_requested: "bg-amber-50 text-amber-700 border-amber-200",
  generating:         "bg-brand-50 text-brand-600 border-brand-200",
  in_progress:        "bg-brand-50 text-brand-600 border-brand-200",
  pending:            "bg-gray-100 text-gray-500 border-gray-200",
  ready:              "bg-sky-50 text-sky-700 border-sky-200",
};

export function StatusChip({ status }) {
  const s = (status || "draft").toLowerCase();
  return (
    <span className={`inline-flex items-center rounded-full border px-2.5 py-0.5 text-[11px] font-bold ${CHIP_STYLE[s] || CHIP_STYLE.draft}`}>
      {s.replace(/_/g, " ")}
    </span>
  );
}

/* ── KPI cards (order per Figma: Total → Under Draft → Under Review → Approved) ── */
export function KpiCards({ stats }) {
  const cards = [
    { label: "Total Projects", value: stats?.total,        accent: "border-l-brand-400" },
    { label: "Under Draft",    value: stats?.under_draft,  accent: "border-l-gray-300" },
    { label: "Under Review",   value: stats?.under_review, accent: "border-l-amber-300" },
    { label: "Approved",       value: stats?.approved,     accent: "border-l-emerald-300" },
  ];
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      {cards.map((c) => (
        <div key={c.label} className={`card border-l-4 px-4 py-3 ${c.accent}`}>
          <div className="text-[11px] font-bold uppercase tracking-wide text-gray-400">{c.label}</div>
          <div className="mt-0.5 text-2xl font-extrabold text-gray-800">{c.value ?? "—"}</div>
        </div>
      ))}
    </div>
  );
}

/* ── Modal ──────────────────────────────────────────────────── */
export function Modal({ open, onClose, title, children, wide }) {
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
    >
      <div className={`max-h-[88vh] w-full ${wide ? "max-w-3xl" : "max-w-lg"} overflow-y-auto rounded-xl bg-white p-5 shadow-2xl`}>
        <div className="mb-3 flex items-center justify-between">
          <h3 className="text-base font-bold text-gray-800">{title}</h3>
          <button className="rounded p-1 text-gray-400 hover:bg-gray-100" onClick={onClose}>✕</button>
        </div>
        {children}
      </div>
    </div>
  );
}

/* ── Spinner ────────────────────────────────────────────────── */
export function Spinner({ label }) {
  return (
    <div className="flex items-center justify-center gap-3 py-10 text-sm text-gray-500">
      <span className="h-5 w-5 animate-spin rounded-full border-2 border-brand-200 border-t-brand-500" />
      {label || "Loading…"}
    </div>
  );
}

/* ── Toasts ─────────────────────────────────────────────────── */
const ToastCtx = createContext(() => {});
export const useToast = () => useContext(ToastCtx);

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);
  const push = useCallback((msg, type = "info") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, msg, type }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 3800);
  }, []);
  const style = {
    ok:   "border-emerald-300 bg-emerald-50 text-emerald-700",
    err:  "border-red-300 bg-red-50 text-red-700",
    info: "border-brand-200 bg-brand-50 text-brand-700",
  };
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="fixed right-4 top-14 z-[60] flex w-80 flex-col gap-2">
        {toasts.map((t) => (
          <div key={t.id} className={`rounded-lg border px-4 py-2.5 text-sm font-semibold shadow-lg ${style[t.type]}`}>
            {t.msg}
          </div>
        ))}
      </div>
    </ToastCtx.Provider>
  );
}

/* ── Accordion row ──────────────────────────────────────────── */
export function Accordion({ header, children, defaultOpen = false }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="card overflow-hidden">
      <button
        className="flex w-full items-center justify-between px-4 py-3 text-left hover:bg-gray-50"
        onClick={() => setOpen(!open)}
      >
        <div className="flex-1">{header}</div>
        <span className={`ml-2 text-gray-400 transition-transform ${open ? "rotate-90" : ""}`}>▸</span>
      </button>
      {open && <div className="border-t border-gray-100 px-4 py-3">{children}</div>}
    </div>
  );
}
