import { useCallback, useEffect, useRef, useState } from "react";
import { Outlet, useNavigate } from "react-router-dom";
import { api, clearUser, getUser, timeAgo } from "./api.js";

const NOTIF_ICON = {
  review_shared: "👥", review_renotified: "⏰", review_responded: "✅",
  comment_added: "💬", comments_kept: "🤖", comment_applied: "📌",
};

export default function App() {
  const nav = useNavigate();
  const user = getUser();
  const [notifs, setNotifs] = useState([]);
  const [unread, setUnread] = useState(0);
  const [bellOpen, setBellOpen] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const bellRef = useRef(null);
  const menuRef = useRef(null);

  const refreshNotifs = useCallback(async () => {
    try {
      const d = await api.get("/notifications?limit=50");
      setNotifs(d.notifications || []);
      setUnread(d.unread_count || 0);
    } catch { /* polling — stay quiet */ }
  }, []);

  useEffect(() => {
    refreshNotifs();
    const t = setInterval(refreshNotifs, 30000);
    return () => clearInterval(t);
  }, [refreshNotifs]);

  useEffect(() => {
    const close = (e) => {
      if (bellRef.current && !bellRef.current.contains(e.target)) setBellOpen(false);
      if (menuRef.current && !menuRef.current.contains(e.target)) setMenuOpen(false);
    };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, []);

  async function clickNotif(n) {
    if (!n.is_read) {
      api.post("/notifications/read", { ids: [n.notification_id] }).then(refreshNotifs).catch(() => {});
    }
    setBellOpen(false);
    if (n.review_id) nav(`/review/${n.review_id}`);
    else if (n.project_id) nav(`/project/${n.project_id}`);
  }

  async function markAllRead() {
    try { await api.post("/notifications/read", {}); refreshNotifs(); } catch { /* ignore */ }
  }

  return (
    <div className="flex min-h-screen flex-col">
      {/* ── Header: logo left · title center · bell + profile right ── */}
      <header className="sticky top-0 z-40 flex h-12 items-center justify-between border-b border-gray-200 bg-white px-4 shadow-sm">
        <button onClick={() => nav("/")} className="flex items-center gap-2">
          <span className="rounded bg-brand-500 px-2 py-0.5 text-sm font-black tracking-wide text-white">A</span>
          <span className="text-sm font-bold text-brand-600">Adani</span>
        </button>

        <h1 className="absolute left-1/2 -translate-x-1/2 text-base font-extrabold tracking-wide text-gray-800">
          IntelliDraft
        </h1>

        <div className="flex items-center gap-2">
          {/* Notification bell */}
          <div className="relative" ref={bellRef}>
            <button
              data-testid="bell"
              onClick={() => { setBellOpen(!bellOpen); if (!bellOpen) refreshNotifs(); }}
              className="relative rounded-lg border border-gray-200 px-2.5 py-1.5 text-base hover:bg-brand-50"
              title="Notifications"
            >
              🔔
              {unread > 0 && (
                <span className="absolute -right-1.5 -top-1.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold text-white">
                  {unread > 99 ? "99+" : unread}
                </span>
              )}
            </button>
            {bellOpen && (
              <div className="absolute right-0 top-10 z-50 w-96 overflow-hidden rounded-xl border border-gray-200 bg-white shadow-2xl">
                <div className="flex items-center justify-between border-b border-gray-100 px-4 py-2.5">
                  <span className="text-sm font-bold">🔔 Notifications</span>
                  <button className="text-xs font-semibold text-brand-600 hover:underline" onClick={markAllRead}>
                    Mark all read
                  </button>
                </div>
                <div className="max-h-[60vh] overflow-y-auto">
                  {notifs.length === 0 && (
                    <div className="px-4 py-8 text-center text-sm text-gray-400">No notifications yet.</div>
                  )}
                  {notifs.map((n) => (
                    <button
                      key={n.notification_id}
                      onClick={() => clickNotif(n)}
                      className={`block w-full border-b border-gray-100 px-4 py-2.5 text-left text-sm hover:bg-gray-50 ${
                        n.is_read ? "" : "bg-brand-50"
                      }`}
                    >
                      <div className="font-semibold text-gray-800">
                        {NOTIF_ICON[n.type] || "🔔"} {n.title}
                      </div>
                      {n.body && <div className="mt-0.5 line-clamp-2 text-xs text-gray-500">{n.body}</div>}
                      <div className="mt-0.5 text-[11px] text-gray-400">{timeAgo(n.created_at)}</div>
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>

          {/* User profile */}
          <div className="relative" ref={menuRef}>
            <button
              data-testid="profile"
              onClick={() => setMenuOpen(!menuOpen)}
              className="flex items-center gap-2 rounded-lg border border-gray-200 px-2.5 py-1.5 hover:bg-brand-50"
            >
              <span className="flex h-6 w-6 items-center justify-center rounded-full bg-brand-500 text-xs font-bold text-white">
                {(user.name || user.email || "?").slice(0, 1).toUpperCase()}
              </span>
              <span className="max-w-36 truncate text-sm font-semibold text-gray-700">{user.name || user.email}</span>
            </button>
            {menuOpen && (
              <div className="absolute right-0 top-10 z-50 w-56 overflow-hidden rounded-xl border border-gray-200 bg-white shadow-2xl">
                <div className="border-b border-gray-100 px-4 py-2.5">
                  <div className="text-sm font-bold text-gray-800">{user.name}</div>
                  <div className="truncate text-xs text-gray-500">{user.email}</div>
                </div>
                <button
                  className="block w-full px-4 py-2.5 text-left text-sm font-semibold text-red-600 hover:bg-red-50"
                  onClick={() => { clearUser(); nav("/login"); }}
                >
                  ⎋ Logout
                </button>
              </div>
            )}
          </div>
        </div>
      </header>

      <main className="flex-1">
        <Outlet context={{ refreshNotifs }} />
      </main>
    </div>
  );
}
