import { useEffect, useState } from "react";
import {
  Layers, Server, Cpu, Network, Globe, Container, Plug, Sparkles, CircleCheck, CircleX, ArrowRight, ArrowLeft,
  Radar, Cloud, FolderKanban, Loader2,
} from "lucide-react";
import { endpoints, setToken } from "../api/client";
import { errDetail } from "../lib/format";

const STEPS = ["Administrator", "This server", "Access", "Review"];
const NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

function Stepper({ step }) {
  return (
    <div className="stepper" aria-label={`Step ${step + 1} of ${STEPS.length}`}>
      {STEPS.map((w, i) => (
        <span key={w} style={{ display: "contents" }}>
          {i > 0 && <span className={`ln${i <= step ? " done" : ""}`} />}
          <span className={`st${i < step ? " done" : i === step ? " cur" : ""}`}>
            <span className="n">{i < step ? <CircleCheck size={14} /> : i + 1}</span>{w}
          </span>
        </span>
      ))}
    </div>
  );
}

export default function Setup({ initial, onDone }) {
  const det = initial?.detected || {};
  const [step, setStep] = useState(0);
  const [f, setF] = useState({
    setup_code: "", username: "", password: "", password2: "",
    server_name: (det.hostname || "").toLowerCase().replace(/[^a-z0-9-]/g, "-"),
    projects_folder: det.projects_folder || "",
    mode: det.tailscale_ip ? "tailscale" : "domain",
    address: initial?.suggested_address || "",
  });
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));
  const [result, setResult] = useState({ address: null });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  // Check the address the other servers will use, shortly after it stops changing.
  const checkable = step === 2 && /^https?:\/\/\S+$/.test(f.address);
  useEffect(() => {
    if (!checkable) return undefined;
    const address = f.address;
    const t = setTimeout(() => {
      endpoints.checkAddress(address)
        .then((r) => setResult({ address, state: r.data.reachable ? "ok" : "bad", reason: r.data.reason }))
        .catch((e) => setResult({ address, state: "bad", reason: errDetail(e) }));
    }, 600);
    return () => clearTimeout(t);
  }, [f.address, checkable]);
  const check = !checkable ? { state: "idle" } : result.address === f.address ? result : { state: "checking" };

  const pickMode = (m) => {
    let address = f.address;
    if (m === "tailscale") address = initial?.suggested_address || (det.tailscale_ip ? `http://${det.tailscale_ip}:${det.port || 8081}` : "");
    else if (f.mode === "tailscale") address = "https://";
    setF((x) => ({ ...x, mode: m, address }));
  };

  const problems = [
    [
      initial?.needs_setup_code && !f.setup_code.trim() && "Enter the setup code from the installer",
      !/^[a-z][a-z0-9_.-]{1,31}$/i.test(f.username) && "Username: 2–32 letters, digits, dots or dashes",
      f.password.length < 12 && "Password: at least 12 characters",
      f.password !== f.password2 && "The passwords differ",
    ],
    [
      !NAME_RE.test(f.server_name) && "Server name: lower-case letters, digits and hyphens",
      !f.projects_folder.startsWith("/") && "Projects folder: an absolute path",
    ],
    [!/^https?:\/\/[^\s/]+/.test(f.address) && "Address: a full URL starting with http:// or https://"],
    [],
  ][step].filter(Boolean);

  const next = async () => {
    setErr("");
    if (problems.length) { setErr(problems[0]); return; }
    if (step < STEPS.length - 1) { setStep(step + 1); return; }
    setBusy(true);
    try {
      const { data } = await endpoints.completeSetup({
        setup_code: f.setup_code.trim(),
        admin: { username: f.username.trim(), password: f.password },
        server_name: f.server_name, projects_folder: f.projects_folder,
        access: { mode: f.mode, address: f.address.trim() },
      });
      if (data.token) setToken(data.token);
      onDone();
    } catch (e) {
      const st = e?.response?.status;
      setErr(st === 403 ? "The setup code is wrong. Use the code the installer printed." : errDetail(e, "Setup did not finish"));
      if (st === 403) setStep(0);
    } finally {
      setBusy(false);
    }
  };

  const checks = det.checks || [];

  return (
    <main>
      <div className="setup page">
        <div className="brandrow">
          <div className="brand"><span className="brand-mark"><Layers size={17} /></span>InfraDocs</div>
          <span className="ver">first run</span>
        </div>
        <Stepper step={step} />
        <div className="wiz">
          <div className="card ctx">
            <span className="eyebrow">Setting up</span>
            <div className="host">
              <div className="app-icon"><Server size={24} /></div>
              <div><div className="t">{f.server_name || det.hostname || "This server"}</div><div className="d">First server · becomes primary</div></div>
            </div>
            <div className="det">
              {(det.os || det.cpu_count) && <div className="r"><Cpu size={15} />{[det.os, det.cpu_count ? `${det.cpu_count} CPU` : null].filter(Boolean).join(" · ")}{det.mem_gb ? <span className="mono">{det.mem_gb} GB</span> : null}</div>}
              {det.tailscale_ip && <div className="r"><Network size={15} />Tailscale<span className="mono">{det.tailscale_ip}</span></div>}
              {det.public_ip && <div className="r"><Globe size={15} />Public address<span className="mono">{det.public_ip}</span></div>}
              {det.docker_version && <div className="r"><Container size={15} />Docker<span className="mono">{det.docker_version}</span></div>}
              {det.port && <div className="r"><Plug size={15} />Port<span className="mono">{det.port}</span></div>}
              {det.projects_found != null && <div className="r"><FolderKanban size={15} />Project folders<span className="mono">{det.projects_found}</span></div>}
            </div>
            <div className="verdict" style={{ borderColor: "rgb(125 176 255 / .3)", background: "rgb(125 176 255 / .06)" }}>
              <Sparkles size={16} color="var(--info)" />
              <span>Add more servers from the Servers page once this one runs. Three voting servers turn on automatic failover.</span>
            </div>
          </div>

          <form className="card concern" onSubmit={(e) => { e.preventDefault(); next(); }}>
            {step === 0 && (
              <>
                <h2>Create the administrator</h2>
                <div className="lead">This account signs in on every server in the cluster.</div>
                {initial?.needs_setup_code && (
                  <div className="field">
                    <label htmlFor="sc">Setup code</label>
                    <input id="sc" className="input" value={f.setup_code} onChange={set("setup_code")} autoFocus autoComplete="off" placeholder="Printed by the installer" />
                    <div className="help">The installer printed it in the terminal. It proves you control this server.</div>
                  </div>
                )}
                <div className="field">
                  <label htmlFor="un">Username</label>
                  <input id="un" className="input" value={f.username} onChange={set("username")} autoComplete="username" autoFocus={!initial?.needs_setup_code} />
                </div>
                <div className="field">
                  <label htmlFor="pw">Password</label>
                  <div className="inputwrap">
                    <input id="pw" className="input" type="password" value={f.password} onChange={set("password")} autoComplete="new-password" />
                    {f.password.length >= 12 && <span className="ok"><CircleCheck size={14} />Strong enough</span>}
                  </div>
                  <div className="help">At least 12 characters.</div>
                </div>
                <div className="field">
                  <label htmlFor="pw2">Repeat password</label>
                  <input id="pw2" className="input" type="password" value={f.password2} onChange={set("password2")} autoComplete="new-password" />
                </div>
              </>
            )}
            {step === 1 && (
              <>
                <h2>Name this server</h2>
                <div className="lead">The name shows on every screen and in the replica set.</div>
                <div className="field">
                  <label htmlFor="sn">Server name</label>
                  <input id="sn" className="input" value={f.server_name} onChange={set("server_name")} autoFocus />
                  <div className="help">Lower-case letters, digits and hyphens.</div>
                </div>
                <div className="field">
                  <label htmlFor="pf">Projects folder</label>
                  <div className="inputwrap">
                    <input id="pf" className="input" value={f.projects_folder} onChange={set("projects_folder")} />
                    {det.projects_found != null && f.projects_folder === det.projects_folder && <span className="ok"><CircleCheck size={14} />{det.projects_found} folders found</span>}
                  </div>
                  <div className="help">Each folder inside it becomes a project.</div>
                </div>
              </>
            )}
            {step === 2 && (
              <>
                <h2>Choose how you reach InfraDocs</h2>
                <div className="lead">Other servers use the same address to join this one.</div>
                <div className="choices" role="radiogroup">
                  {[
                    ["tailscale", Network, "Tailscale", "Private to your tailnet. No ports opened."],
                    ["domain", Globe, "Your domain", "Public HTTPS behind your own proxy and certificate."],
                    ["cloudflare", Cloud, "Cloudflare Tunnel", "Public through Cloudflare, no inbound ports."],
                  ].map(([k, I, t, d]) => (
                    <button key={k} type="button" role="radio" aria-checked={f.mode === k} className={`choice${f.mode === k ? " on" : ""}`} onClick={() => pickMode(k)}>
                      {k === "tailscale" && det.tailscale_ip && <span className="chip ok rec"><span className="d" />Detected</span>}
                      <span className="ci"><I size={20} /></span>
                      <div className="t">{t}</div>
                      <div className="d">{d}</div>
                    </button>
                  ))}
                </div>
                <div className="field">
                  <label htmlFor="ad">Address</label>
                  <div className="inputwrap">
                    <input id="ad" className="input" value={f.address} onChange={set("address")} placeholder="https://infra.example.com" />
                    {check.state === "checking" && <span className="ok" style={{ color: "var(--subtle)" }}><Loader2 size={14} className="spin" />Checking</span>}
                    {check.state === "ok" && <span className="ok"><CircleCheck size={14} />Reachable</span>}
                    {check.state === "bad" && <span className="bad"><CircleX size={14} />Not reachable</span>}
                  </div>
                  {check.state === "bad" && <div className="help">{check.reason || "Nothing answered at this address."} Setup can continue; servers join once it answers.</div>}
                </div>
                {checks.length > 0 && (
                  <div className="checks">
                    {checks.map((c) => (
                      <div className="c" key={c.id}>{c.ok ? <CircleCheck size={16} /> : <CircleX size={16} color="var(--danger)" />}<span>{c.label}</span></div>
                    ))}
                  </div>
                )}
              </>
            )}
            {step === 3 && (
              <>
                <h2>Review and run the first scan</h2>
                <div className="lead">InfraDocs starts as a standalone primary. Add servers from the Servers page.</div>
                <div className="kv" style={{ fontSize: 14, gridTemplateColumns: "160px minmax(0,1fr)", gap: "12px 14px" }}>
                  <span className="k">Administrator</span><span className="v">{f.username}</span>
                  <span className="k">Server</span><span className="v mono">{f.server_name}</span>
                  <span className="k">Projects folder</span><span className="v mono">{f.projects_folder}</span>
                  <span className="k">Access</span><span className="v">{{ tailscale: "Tailscale", domain: "Your domain", cloudflare: "Cloudflare Tunnel" }[f.mode]}</span>
                  <span className="k">Address</span><span className="v mono">{f.address}</span>
                  <span className="k">Role</span><span className="v">Primary · standalone until a second server joins</span>
                </div>
              </>
            )}
            {err && <div className="formerr">{err}</div>}
            <div className="wfoot">
              <span className="note">Step {step + 1} of {STEPS.length}</span>
              <span className="sp">
                {step > 0 && <button type="button" className="btn" onClick={() => { setErr(""); setStep(step - 1); }}><ArrowLeft size={15} />Back</button>}
                <button type="submit" className="btn primary" disabled={busy}>
                  {busy ? <Loader2 size={15} className="spin" /> : step === STEPS.length - 1 ? <Radar size={15} /> : null}
                  {step === STEPS.length - 1 ? "Finish and scan" : "Next"}
                  {step < STEPS.length - 1 && <ArrowRight size={15} />}
                </button>
              </span>
            </div>
          </form>
        </div>
      </div>
    </main>
  );
}
