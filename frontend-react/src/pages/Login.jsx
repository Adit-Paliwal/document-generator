import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { setUser } from "../api.js";

// Identity capture — stands in for Entra ID SSO. The backend trusts
// X-User-Email / X-User-Name headers; swap this page for MSAL when SSO lands.
export default function Login() {
  const nav = useNavigate();
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [err, setErr] = useState("");

  function submit(e) {
    e.preventDefault();
    const em = email.trim().toLowerCase();
    if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(em)) {
      setErr("Enter a valid email address.");
      return;
    }
    setUser(em, name.trim() || em.split("@")[0]);
    nav("/");
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gradient-to-br from-brand-700 via-brand-500 to-brand-300 p-4">
      <form onSubmit={submit} className="w-full max-w-sm rounded-2xl bg-white/95 p-8 shadow-2xl backdrop-blur">
        <div className="mb-6 text-center">
          <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-xl bg-brand-500 text-2xl font-black text-white">A</div>
          <h1 className="text-xl font-extrabold text-gray-800">IntelliDraft</h1>
          <p className="mt-1 text-xs text-gray-500">AI document generation · Adani internal</p>
        </div>

        <label className="label">Work email</label>
        <input
          className="input mb-3" data-testid="login-email" autoFocus
          placeholder="you@adani.com" value={email}
          onChange={(e) => { setEmail(e.target.value); setErr(""); }}
        />
        <label className="label">Display name</label>
        <input
          className="input mb-4" data-testid="login-name"
          placeholder="Your name" value={name}
          onChange={(e) => setName(e.target.value)}
        />
        {err && <div className="mb-3 text-xs font-semibold text-red-600">{err}</div>}
        <button className="btn-brand w-full justify-center py-2.5" data-testid="login-submit">
          Sign in →
        </button>
        <p className="mt-4 text-center text-[11px] text-gray-400">
          Entra ID SSO will replace this screen in production.
        </p>
      </form>
    </div>
  );
}
