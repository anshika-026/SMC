import { useEffect, useState } from "react";
import { CheckCircle2, Loader2, ShieldCheck, XCircle } from "lucide-react";
import * as api from "../../api/client";
import { COUNTRY_CODES, DarkModal, Label, StatusPill, btnGhost, btnPrimary, field } from "./ui";

// Connect WhatsApp Business: 1) the business number, 2) Meta Business onboarding, 3) Meta's
// answer. The account is shown as connected only when the backend says Meta confirmed it.

const STEPS = ["Business number", "Meta Business", "Verification"];

export default function ConnectWizard({ open, connection, onClose, onChanged }) {
  const [step, setStep] = useState(0);
  const [cc, setCc] = useState("+91");
  const [number, setNumber] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);

  useEffect(() => {
    if (!open) return;
    setStep(0);
    setError("");
    setResult(null);
    const n = connection?.entered_number;
    if (n) {
      const code = COUNTRY_CODES.map(([c]) => c).sort((a, b) => b.length - a.length).find((c) => n.startsWith(c.slice(1)));
      if (code) {
        setCc(code);
        setNumber(n.slice(code.length - 1));
      }
    }
  }, [open, connection?.entered_number]);

  async function saveNumber() {
    setBusy(true);
    setError("");
    try {
      onChanged(await api.connectApi.connect(cc, number));
      setStep(1);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function verify() {
    setBusy(true);
    setError("");
    try {
      const c = await api.connectApi.verify();
      onChanged(c);
      setResult(c);
      setStep(2);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <DarkModal open={open} onClose={onClose} title="Connect WhatsApp Business" width="max-w-xl">
      <ol className="flex items-center gap-2 mb-5 text-xs">
        {STEPS.map((s, i) => (
          <li key={s} className="flex items-center gap-2 flex-1">
            <span className={`w-6 h-6 rounded-full grid place-items-center font-semibold ${i < step ? "bg-[#25D366] text-[#062b16]" : i === step ? "bg-white text-[#0f1528]" : "bg-white/10 text-slate-400"}`}>
              {i + 1}
            </span>
            <span className={i === step ? "text-white" : "text-slate-400"}>{s}</span>
            {i < STEPS.length - 1 && <span className="flex-1 h-px bg-white/10" />}
          </li>
        ))}
      </ol>

      {step === 0 && (
        <div className="space-y-4">
          <p className="text-sm text-slate-400">The WhatsApp Business number SMC will send notifications from.</p>
          <div className="grid grid-cols-[120px_1fr] gap-3">
            <label className="block">
              <Label>Country code</Label>
              <select value={cc} onChange={(e) => setCc(e.target.value)} className={field}>
                {COUNTRY_CODES.map(([c, name]) => (
                  <option key={c} value={c} className="bg-[#0f1528]">
                    {c} {name}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <Label>Phone number</Label>
              <input inputMode="tel" value={number} onChange={(e) => setNumber(e.target.value)} placeholder="98765 43210" className={field} />
            </label>
          </div>
          {error && <p className="text-xs text-rose-300">{error}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" className={btnGhost} onClick={onClose}>Cancel</button>
            <button type="button" className={btnPrimary} disabled={busy || !number.trim()} onClick={saveNumber}>
              {busy && <Loader2 size={14} className="animate-spin" />} Continue
            </button>
          </div>
        </div>
      )}

      {step === 1 && (
        <div className="space-y-4 text-sm">
          <div className="rounded-xl bg-white/[0.04] ring-1 ring-white/10 p-4 space-y-2">
            <p className="font-medium text-white flex items-center gap-2"><ShieldCheck size={16} className="text-[#25D366]" /> Meta Business onboarding</p>
            <ol className="list-decimal pl-5 text-slate-400 space-y-1 text-[13px]">
              <li>In Meta Business Manager, add this number to your WhatsApp Business Account and verify it there.</li>
              <li>Create a system-user access token with the <i>whatsapp_business_messaging</i> permission.</li>
              <li>Your SMC administrator sets the token, phone-number ID and business-account ID on the SMC server. They never pass through this page.</li>
            </ol>
            <p className="text-[11px] text-slate-500">Meta Embedded Signup (one-click onboarding) can replace these steps once a Meta app is approved for it.</p>
          </div>
          {!connection?.configured && (
            <p className="rounded-xl bg-indigo-500/10 ring-1 ring-indigo-400/30 px-3 py-2 text-xs text-indigo-200">
              Demo mode: the server has no WhatsApp credentials yet, so verification will report Not connected.
            </p>
          )}
          {error && <p className="text-xs text-rose-300">{error}</p>}
          <div className="flex justify-between gap-2">
            <button type="button" className={btnGhost} onClick={() => setStep(0)}>Back</button>
            <button type="button" className={btnPrimary} disabled={busy} onClick={verify}>
              {busy && <Loader2 size={14} className="animate-spin" />} Verify with Meta
            </button>
          </div>
        </div>
      )}

      {step === 2 && result && (
        <div className="space-y-4 text-sm">
          {result.status === "connected" ? (
            <div className="rounded-xl bg-emerald-500/10 ring-1 ring-emerald-400/30 p-4 space-y-1">
              <p className="font-medium text-emerald-200 flex items-center gap-2"><CheckCircle2 size={16} /> Meta confirmed the connection</p>
              <p className="text-slate-300">{result.verified_name} · {result.business_number}</p>
              {result.business_account?.name && <p className="text-xs text-slate-400">Business account: {result.business_account.name} ({result.business_account.review_status || "status unknown"})</p>}
            </div>
          ) : (
            <div className="rounded-xl bg-rose-500/10 ring-1 ring-rose-400/30 p-4 space-y-1">
              <p className="font-medium text-rose-200 flex items-center gap-2"><XCircle size={16} /> Not connected</p>
              <p className="text-slate-300">{result.last_error || "Meta did not confirm this number."}</p>
            </div>
          )}
          <div className="flex items-center justify-between">
            <StatusPill status={result.status} />
            <div className="flex gap-2">
              {result.status !== "connected" && <button type="button" className={btnGhost} onClick={() => setStep(1)}>Try again</button>}
              <button type="button" className={btnPrimary} onClick={onClose}>Done</button>
            </div>
          </div>
        </div>
      )}
    </DarkModal>
  );
}
