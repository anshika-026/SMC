import { useEffect, useState } from "react";
import { CheckCircle2, KeyRound, Loader2, ShieldCheck, XCircle } from "lucide-react";
import * as api from "../../api/client";
import { runEmbeddedSignup } from "../../lib/metaSignup";
import { COUNTRY_CODES, DarkModal, Label, StatusPill, btnGhost, btnPrimary, field } from "./ui";

// Connect WhatsApp Business. With Embedded Signup set up on the server, the organization signs in
// to Meta in Meta's popup and picks its WhatsApp Business Account and number; otherwise the manual
// setup (credentials on the server, then Verify). Connected is shown only when the backend says
// Meta confirmed it.

export default function ConnectWizard({ open, connection, signup, onClose, onChanged }) {
  const embedded = !!signup?.available;
  const [manual, setManual] = useState(false);
  useEffect(() => {
    if (open) setManual(!embedded);
  }, [open, embedded]);

  return (
    <DarkModal open={open} onClose={onClose} title="Connect WhatsApp Business" width="max-w-xl">
      {embedded && !manual ? (
        <EmbeddedFlow signup={signup} connection={connection} onChanged={onChanged} onClose={onClose} onManual={() => setManual(true)} />
      ) : (
        <ManualFlow connection={connection} onChanged={onChanged} onClose={onClose} onEmbedded={embedded ? () => setManual(false) : null} />
      )}
    </DarkModal>
  );
}

function Steps({ steps, step }) {
  return (
    <ol className="flex items-center gap-2 mb-5 text-xs">
      {steps.map((s, i) => (
        <li key={s} className="flex items-center gap-2 flex-1">
          <span className={`w-6 h-6 rounded-full grid place-items-center font-semibold ${i < step ? "bg-[#25D366] text-[#062b16]" : i === step ? "bg-white text-[#0f1528]" : "bg-white/10 text-slate-400"}`}>
            {i + 1}
          </span>
          <span className={i === step ? "text-white" : "text-slate-400"}>{s}</span>
          {i < steps.length - 1 && <span className="flex-1 h-px bg-white/10" />}
        </li>
      ))}
    </ol>
  );
}

const NOT_CONNECTED_TITLE = {
  not_configured: "Not configured on the server",
  configured: "Not confirmed by Meta",
  connection_failed: "Connection failed",
};

// The outcome of a check. Try again asks the backend to check with Meta again (POST /verify);
// it never decides the status itself.
function Result({ result, onRetry, onClose, error, extra }) {
  const [busy, setBusy] = useState(false);
  async function retry() {
    setBusy(true);
    try {
      await onRetry();
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-4 text-sm">
      {result.status === "connected" ? (
        <div className="rounded-xl bg-emerald-500/10 ring-1 ring-emerald-400/30 p-4 space-y-1">
          <p className="font-medium text-emerald-200 flex items-center gap-2"><CheckCircle2 size={16} /> Meta confirmed the connection</p>
          <p className="text-slate-300">{result.verified_name} · {result.business_number}</p>
          {result.business_account?.name && <p className="text-xs text-slate-400">Business account: {result.business_account.name} ({result.business_account.review_status || "status unknown"})</p>}
        </div>
      ) : (
        <div className="rounded-xl bg-rose-500/10 ring-1 ring-rose-400/30 p-4 space-y-1">
          <p className="font-medium text-rose-200 flex items-center gap-2"><XCircle size={16} /> {NOT_CONNECTED_TITLE[result.state] || "Not connected"}</p>
          <p className="text-slate-300">{result.last_error || "Meta did not confirm this number."}</p>
        </div>
      )}
      {error && <p className="text-xs text-rose-300">{error}</p>}
      <div className="flex items-center justify-between gap-2">
        <StatusPill status={result.state || result.status} />
        <div className="flex gap-2">
          {extra}
          {result.status !== "connected" && (
            <button type="button" className={btnGhost} disabled={busy} onClick={retry}>
              {busy && <Loader2 size={14} className="animate-spin" />} {busy ? "Checking with Meta…" : "Try again"}
            </button>
          )}
          <button type="button" className={btnPrimary} onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- Embedded Signup

function EmbeddedFlow({ signup, onChanged, onClose, onManual }) {
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);

  async function start() {
    setError("");
    setBusy("meta");
    try {
      const session = await runEmbeddedSignup({ appId: signup.app_id, configId: signup.config_id, version: signup.graph_version });
      setBusy("server");
      const c = await api.connectApi.embeddedSignup({ ...session, pin: pin || null });
      onChanged(c);
      setResult(c);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  async function recheck() {
    setError("");
    try {
      const c = await api.connectApi.verify();
      onChanged(c);
      setResult(c);
    } catch (e) {
      setError(e.message);
    }
  }

  if (result) return (
    <>
      <Steps steps={["Meta sign-in", "Connected"]} step={1} />
      <Result result={result} onRetry={recheck} onClose={onClose} error={error}
        extra={result.status !== "connected" && <button type="button" className={btnGhost} onClick={() => { setError(""); setResult(null); }}>Sign in again</button>} />
    </>
  );
  return (
    <div className="space-y-4 text-sm">
      <Steps steps={["Meta sign-in", "Connected"]} step={0} />
      <div className="rounded-xl bg-white/[0.04] ring-1 ring-white/10 p-4 space-y-2">
        <p className="font-medium text-white flex items-center gap-2"><ShieldCheck size={16} className="text-[#25D366]" /> Sign in with your Meta business account</p>
        <ol className="list-decimal pl-5 text-slate-400 space-y-1 text-[13px]">
          <li>Meta opens in a popup. Choose or create your business portfolio and WhatsApp Business Account.</li>
          <li>Add or pick the phone number SMC will send from, and verify it there.</li>
          <li>SMC receives access to that account only. Your Meta password and token never pass through this page.</li>
        </ol>
      </div>
      <label className="block">
        <Label>Two-step verification PIN (6 digits, optional)</Label>
        <div className="relative">
          <KeyRound size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-500" />
          <input inputMode="numeric" maxLength={6} value={pin} onChange={(e) => setPin(e.target.value.replace(/\D/g, ""))} placeholder="••••••" className={`${field} pl-8 font-mono tracking-widest`} />
        </div>
        <span className="block text-[11px] text-slate-500 mt-1">
          For a number that is new to the WhatsApp Cloud API: SMC registers it with this PIN. If the number already has a PIN, enter that one. Leave empty if it is already registered.
        </span>
      </label>
      {error && <p className="text-xs text-rose-300">{error}</p>}
      <div className="flex items-center justify-between gap-2">
        {onManual ? <button type="button" className="text-xs text-slate-400 hover:text-white" onClick={onManual}>Use manual setup instead</button> : <span />}
        <div className="flex gap-2">
          <button type="button" className={btnGhost} onClick={onClose}>Cancel</button>
          <button type="button" className={btnPrimary} disabled={!!busy || (pin && pin.length !== 6)} onClick={start}>
            {busy && <Loader2 size={14} className="animate-spin" />}
            {busy === "meta" ? "Waiting for Meta…" : busy === "server" ? "Connecting…" : "Continue with Meta"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- manual setup

function ManualFlow({ connection, onChanged, onClose, onEmbedded }) {
  const [step, setStep] = useState(0);
  const [cc, setCc] = useState("+91");
  const [number, setNumber] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);

  useEffect(() => {
    const n = connection?.entered_number;
    if (!n) return;
    const code = COUNTRY_CODES.map(([c]) => c).sort((a, b) => b.length - a.length).find((c) => n.startsWith(c.slice(1)));
    if (code) {
      setCc(code);
      setNumber(n.slice(code.length - 1));
    }
  }, [connection?.entered_number]);

  async function call(fn, next) {
    setBusy(true);
    setError("");
    try {
      const c = await fn();
      onChanged(c);
      setResult(c);
      setStep(next);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Steps steps={["Business number", "Server credentials", "Verification"]} step={step} />
      {step === 0 && (
        <div className="space-y-4">
          <p className="text-sm text-slate-400">The WhatsApp Business number SMC will send notifications from.</p>
          <div className="grid grid-cols-[120px_1fr] gap-3">
            <label className="block">
              <Label>Country code</Label>
              <select value={cc} onChange={(e) => setCc(e.target.value)} className={field}>
                {COUNTRY_CODES.map(([c, name]) => <option key={c} value={c} className="bg-[#0f1528]">{c} {name}</option>)}
              </select>
            </label>
            <label className="block">
              <Label>Phone number</Label>
              <input inputMode="tel" value={number} onChange={(e) => setNumber(e.target.value)} placeholder="98765 43210" className={field} />
            </label>
          </div>
          {error && <p className="text-xs text-rose-300">{error}</p>}
          <div className="flex items-center justify-between gap-2">
            {onEmbedded ? <button type="button" className="text-xs text-slate-400 hover:text-white" onClick={onEmbedded}>Sign in with Meta instead</button> : <span />}
            <div className="flex gap-2">
              <button type="button" className={btnGhost} onClick={onClose}>Cancel</button>
              <button type="button" className={btnPrimary} disabled={busy || !number.trim()} onClick={() => call(() => api.connectApi.connect(cc, number), 1)}>
                {busy && <Loader2 size={14} className="animate-spin" />} Continue
              </button>
            </div>
          </div>
        </div>
      )}
      {step === 1 && (
        <div className="space-y-4 text-sm">
          <div className="rounded-xl bg-white/[0.04] ring-1 ring-white/10 p-4 space-y-2">
            <p className="font-medium text-white flex items-center gap-2"><ShieldCheck size={16} className="text-[#25D366]" /> Credentials on the SMC server</p>
            <ol className="list-decimal pl-5 text-slate-400 space-y-1 text-[13px]">
              <li>In Meta Business Manager, add this number to your WhatsApp Business Account and verify it there.</li>
              <li>Create a system-user access token with the <i>whatsapp_business_messaging</i> and <i>whatsapp_business_management</i> permissions.</li>
              <li>Your SMC administrator sets the token, phone-number ID and business-account ID on the SMC server. They never pass through this page.</li>
            </ol>
          </div>
          {!connection?.setup?.manual && (
            <p className="rounded-xl bg-indigo-500/10 ring-1 ring-indigo-400/30 px-3 py-2 text-xs text-indigo-200">
              Not configured: {(connection?.setup?.manual_missing || []).join(" and ")} {(connection?.setup?.manual_missing || []).length === 1 ? "is" : "are"} not set
              in backend/.env on the server. Verification will report Not configured until they are set and SMC is restarted.
            </p>
          )}
          {error && <p className="text-xs text-rose-300">{error}</p>}
          <div className="flex justify-between gap-2">
            <button type="button" className={btnGhost} onClick={() => setStep(0)}>Back</button>
            <button type="button" className={btnPrimary} disabled={busy} onClick={() => call(api.connectApi.verify, 2)}>
              {busy && <Loader2 size={14} className="animate-spin" />} Verify with Meta
            </button>
          </div>
        </div>
      )}
      {step === 2 && result && <Result result={result} onRetry={() => call(api.connectApi.verify, 2)} onClose={onClose} error={error} />}
    </>
  );
}
