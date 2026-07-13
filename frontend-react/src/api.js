// Thin fetch client for the IntelliDraft FastAPI backend.
// Identity: X-User-Email / X-User-Name headers (Entra ID SSO placeholder —
// the login page captures identity into localStorage until SSO is wired).

const BASE = "/api";

export function getUser() {
  return {
    email: (localStorage.getItem("id_user_email") || "").trim().toLowerCase(),
    name: (localStorage.getItem("id_user_name") || "").trim(),
  };
}

export function setUser(email, name) {
  localStorage.setItem("id_user_email", (email || "").trim().toLowerCase());
  localStorage.setItem("id_user_name", (name || "").trim());
}

export function clearUser() {
  localStorage.removeItem("id_user_email");
  localStorage.removeItem("id_user_name");
}

function authHeaders() {
  const u = getUser();
  const h = {};
  if (u.email) h["X-User-Email"] = u.email;
  if (u.name) h["X-User-Name"] = u.name;
  return h;
}

async function request(method, path, body, opts = {}) {
  const headers = { ...authHeaders(), ...(opts.headers || {}) };
  const init = { method, headers };
  if (body instanceof FormData) {
    init.body = body; // browser sets multipart boundary
  } else if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const resp = await fetch(BASE + path, init);
  if (resp.status === 204) return null;
  const ctype = resp.headers.get("content-type") || "";
  const data = ctype.includes("application/json") ? await resp.json() : await resp.text();
  if (!resp.ok) {
    const msg = (data && data.error) || `HTTP ${resp.status}`;
    const err = new Error(msg);
    err.status = resp.status;
    err.data = data;
    throw err;
  }
  return data;
}

export const api = {
  get: (p, opts) => request("GET", p, undefined, opts),
  post: (p, b, opts) => request("POST", p, b, opts),
  put: (p, b, opts) => request("PUT", p, b, opts),
  patch: (p, b, opts) => request("PATCH", p, b, opts),
  del: (p, opts) => request("DELETE", p, undefined, opts),
};

export function timeAgo(iso) {
  if (!iso) return "";
  const s = Math.floor((Date.now() - new Date(iso + (iso.endsWith("Z") ? "" : "Z")).getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

// Minimal markdown → HTML for the word-like preview (headings, bold, lists, tables)
export function mdToHtml(md) {
  if (!md) return "";
  const esc = (t) => t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const lines = md.split("\n");
  let html = "", inUl = false, inOl = false, inTable = false;
  const closeLists = () => {
    if (inUl) { html += "</ul>"; inUl = false; }
    if (inOl) { html += "</ol>"; inOl = false; }
  };
  const inline = (t) =>
    esc(t)
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/\*(.+?)\*/g, "<i>$1</i>")
      .replace(/`(.+?)`/g, "<code>$1</code>");
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (/^\s*\|.*\|\s*$/.test(l)) {
      if (/^\s*\|[\s\-:|]+\|\s*$/.test(l)) continue; // separator row
      if (!inTable) { closeLists(); html += "<table>"; inTable = true; }
      const cells = l.trim().replace(/^\||\|$/g, "").split("|");
      html += "<tr>" + cells.map((c) => `<td>${inline(c.trim())}</td>`).join("") + "</tr>";
      continue;
    } else if (inTable) { html += "</table>"; inTable = false; }
    const h = l.match(/^(#{1,4})\s+(.*)/);
    if (h) { closeLists(); html += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`; continue; }
    const ul = l.match(/^\s*[-*]\s+(.*)/);
    if (ul) { if (!inUl) { html += "<ul>"; inUl = true; } html += `<li>${inline(ul[1])}</li>`; continue; }
    const ol = l.match(/^\s*\d+\.\s+(.*)/);
    if (ol) { if (!inOl) { html += "<ol>"; inOl = true; } html += `<li>${inline(ol[1])}</li>`; continue; }
    closeLists();
    if (l.trim()) html += `<p>${inline(l)}</p>`;
  }
  closeLists();
  if (inTable) html += "</table>";
  return html;
}
