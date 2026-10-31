import { useState } from "react";
import { Layers, LogIn, KeyRound } from "lucide-react";
import { endpoints, setToken } from "../api/client";
import { errDetail } from "../lib/format";

function Brand() {
  return (
    <div className="brand" style={{ fontSize: 19 }}>
      <span className="brand-mark"><Layers size={17} /></span>InfraDocs
    </div>
  );
}

export function Login({ onLoggedIn }) {
  const [u, setU] = useState("");
  const [p, setP] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    try {
      const { data } = await endpoints.login(u.trim(), p);
      setToken(data.token);
      onLoggedIn(data);
    } catch (e2) {
      setErr(e2?.response?.status === 401 ? "Wrong username or password" : errDetail(e2, "Sign-in failed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <form onSubmit={submit} className="card">
        <Brand />
        <div>
          <h1>Sign in</h1>
        </div>
        <div className="lead">Use the administrator account created during setup.</div>
        <div className="field">
          <label htmlFor="u">Username</label>
          <input id="u" className="input" autoFocus autoComplete="username" value={u} onChange={(e) => setU(e.target.value)} />
        </div>
        <div className="field">
          <label htmlFor="p">Password</label>
          <input id="p" className="input" type="password" autoComplete="current-password" value={p} onChange={(e) => setP(e.target.value)} />
        </div>
        {err && <div className="formerr">{err}</div>}
        <button type="submit" className="btn primary" style={{ height: 44, justifyContent: "center" }} disabled={busy || !u || !p}>
          <LogIn size={16} />{busy ? "Signing in" : "Sign in"}
        </button>
      </form>
    </div>
  );
}

export function ChangePassword({ onDone }) {
  const [p1, setP1] = useState("");
  const [p2, setP2] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e) {
    e.preventDefault();
    setErr("");
    if (p1.length < 12) return setErr("Use at least 12 characters");
    if (p1 !== p2) return setErr("The passwords differ");
    setBusy(true);
    try {
      await endpoints.changePassword(p1);
      onDone();
    } catch (e2) {
      setErr(errDetail(e2, "Password not changed"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth">
      <form onSubmit={submit} className="card">
        <Brand />
        <h1>Set a new password</h1>
        <div className="lead">This account still has its initial password.</div>
        <div className="field">
          <label htmlFor="p1">New password</label>
          <input id="p1" className="input" type="password" autoFocus autoComplete="new-password" value={p1} onChange={(e) => setP1(e.target.value)} />
          <div className="help">At least 12 characters.</div>
        </div>
        <div className="field">
          <label htmlFor="p2">Repeat password</label>
          <input id="p2" className="input" type="password" autoComplete="new-password" value={p2} onChange={(e) => setP2(e.target.value)} />
        </div>
        {err && <div className="formerr">{err}</div>}
        <button type="submit" className="btn primary" style={{ height: 44, justifyContent: "center" }} disabled={busy || !p1 || !p2}>
          <KeyRound size={16} />{busy ? "Saving" : "Save and continue"}
        </button>
      </form>
    </div>
  );
}
