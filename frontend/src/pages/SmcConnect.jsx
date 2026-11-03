import { useCallback, useEffect, useState } from "react";
import { Building2, CheckCheck, Clock3, Link2, Loader2, Phone, RefreshCw, Send, Unplug, XCircle } from "lucide-react";
import * as api from "../api/client";
import WhatsAppIcon from "../components/connect/WhatsAppIcon";
import ConnectWizard from "../components/connect/ConnectWizard";
import { HistoryPanel, RecipientsPanel, RulesPanel, TemplatesPanel } from "../components/connect/Panels";
import { COUNTRY_CODES, DarkModal, Label, MsgBadge, Panel, StatusPill, btnDanger, btnGhost, btnPrimary, field } from "../components/connect/ui";

// SMC Connect: WhatsApp Business notifications (backend/app/whatsapp.py). Opened from the profile
// menu. Every number here comes from the backend: connection only when Meta confirmed it, delivery
// counts from Meta's own status callbacks, and Demo mode when the server has no credentials.

const TABS = [
  ["rules", "Notification rules"],
  ["recipients", "Recipients"],
  ["templates", "Message templates"],
  ["history", "Notification history"],
];
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—");

export default function SmcConnect() {
  const [overview, setOverview] = useState(null);
  const [rules, setRules] = useState([]);
  const [recipients, setRecipients] = useState([]);
  const [tab, setTab] = useState("rules");
  const [wizard, setWizard] = useState(false);
  const [testOpen, setTestOpen] = useState(false);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [historyKey, setHistoryKey] = useState(0);

  const load = useCallback(() => {
    api.connectApi.overview().then(setOverview).catch((e) => setError(e.message));
    api.connectApi.rules().then((d) => setRules(d.rules)).catch(() => {});
    api.connectApi.recipients().then((d) => setRecipients(d.recipients)).catch(() => {});
    setHistoryKey((k) => k + 1);
  }, []);
  useEffect(() => {
    load();
  }, [load]);

  const c = overview?.connection;
  const s = overview?.stats;
  const setConnection = (conn) => setOverview((o) => ({ ...o, connection: conn }));

  async function run(name, fn) {
    setBusy(name);
    setError("");
    try {
      setConnection(await fn());
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  const connected = c?.status === "connected";
  return (
    <div className="rounded-3xl bg-[#0b1020] text-slate-200 p-4 sm:p-6 lg:p-7 space-y-6 shadow-[0_10px_40px_rgba(11,16,32,0.25)]">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-4">
        <span className="w-12 h-12 rounded-2xl grid place-items-center bg-[#25D366]/15 text-[#25D366] ring-1 ring-[#25D366]/30">
          <WhatsAppIcon size={24} />
        </span>
        <div className="mr-auto">
          <h1 className="text-xl font-semibold text-white tracking-tight">SMC Connect</h1>
          <p className="text-sm text-slate-400">WhatsApp Business notifications for security, visitors, attendance and daily reports.</p>
        </div>
        {c && <StatusPill status={c.status} />}
        <div className="flex flex-wrap gap-2">
          {connected ? (
            <>
              <button type="button" className={btnGhost} disabled={!!busy} onClick={() => run("verify", api.connectApi.verify)}>
                {busy === "verify" ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />} Re-check
              </button>
              <button type="button" className={btnDanger} disabled={!!busy} onClick={() => window.confirm("Disconnect WhatsApp? Notifications stop until you reconnect.") && run("disconnect", api.connectApi.disconnect)}>
                <Unplug size={14} /> Disconnect
              </button>
            </>
          ) : (
            <button type="button" className={btnPrimary} onClick={() => setWizard(true)}>
              <Link2 size={15} /> {c?.status === "disconnected" || c?.status === "failed" ? "Reconnect WhatsApp" : "Connect WhatsApp Business"}
            </button>
          )}
          <button type="button" className={btnGhost} onClick={() => setTestOpen(true)}>
            <Send size={14} /> Send test notification
          </button>
        </div>
      </div>

      {c?.last_error && !c.demo_mode && <p className="rounded-xl bg-rose-500/10 ring-1 ring-rose-400/30 px-4 py-2 text-sm text-rose-200">{c.last_error}</p>}
      {error && <p className="text-sm text-rose-300">{error}</p>}

      {/* Overview */}
      <div className="grid gap-3 grid-cols-2 lg:grid-cols-6">
        <Stat icon={WhatsAppIcon} label="WhatsApp" value={c ? <StatusPill status={c.status} /> : "…"} sub={c?.checked_at ? `checked ${when(c.checked_at)}` : "never checked"} />
        <Stat icon={Phone} label="Business number" value={c?.business_number || "—"} sub={connected ? c.verified_name : c?.entered_number ? `entered +${c.entered_number}, not verified` : "not set"} />
        <Stat icon={Building2} label="Meta Business Account" value={connected ? (c.business_account?.review_status || (c.business_account ? "Unknown" : "Not set")) : "—"} sub={connected ? c.business_account?.name || "account id not configured" : "not connected"} />
        <Stat icon={Send} label="Notifications sent" value={s ? s.sent : "…"} sub={s?.not_sent ? `${s.not_sent} not sent (not connected)` : "accepted by Meta"} />
        <Stat icon={CheckCheck} label="Delivered / failed" value={s ? <span>{s.delivered} <span className="text-slate-500">/</span> <span className={s.failed ? "text-rose-300" : ""}>{s.failed}</span></span> : "…"} sub={s ? `${s.read} read` : ""} />
        <Stat icon={Clock3} label="Last successful" value={s?.last_success ? when(s.last_success.ts) : "—"} sub={s?.last_success ? `${s.last_success.event_label} → ${s.last_success.recipient_name}` : "none yet"} />
      </div>

      {/* Sections */}
      <div className="flex flex-wrap gap-1 rounded-2xl bg-white/[0.04] ring-1 ring-white/10 p-1 w-fit max-w-full overflow-x-auto">
        {TABS.map(([k, label]) => (
          <button key={k} type="button" onClick={() => setTab(k)}
            className={`rounded-xl px-3.5 py-2 text-sm whitespace-nowrap transition ${tab === k ? "bg-white text-[#0b1020] font-medium" : "text-slate-400 hover:text-white"}`}>
            {label}
          </button>
        ))}
      </div>

      {tab === "rules" && <RulesPanel rules={rules} recipients={recipients} onError={setError} onSaved={(r) => setRules((rs) => rs.map((x) => (x.key === r.key ? r : x)))} />}
      {tab === "recipients" && <RecipientsPanel recipients={recipients} onError={setError} onChanged={load} />}
      {tab === "templates" && <TemplatesPanel rules={rules} onError={setError} onSaved={(r) => setRules((rs) => rs.map((x) => (x.key === r.key ? r : x)))} />}
      {tab === "history" && <HistoryPanel rules={rules} refreshKey={historyKey} />}

      <ConnectWizard open={wizard} connection={c} onClose={() => { setWizard(false); load(); }} onChanged={setConnection} />
      <TestDialog open={testOpen} rules={rules} recipients={recipients} connection={c} onClose={() => { setTestOpen(false); load(); }} />
    </div>
  );
}

function Stat({ icon: Icon, label, value, sub }) {
  return (
    <Panel className="p-4 min-w-0">
      <div className="flex items-center gap-2 text-[11px] uppercase tracking-wide text-slate-400">
        <Icon size={13} /> {label}
      </div>
      <div className="mt-2 text-lg font-semibold text-white truncate">{value}</div>
      <p className="text-[11px] text-slate-500 mt-1 truncate" title={typeof sub === "string" ? sub : undefined}>{sub}</p>
    </Panel>
  );
}

// Sends one real test message; shows only what the backend reports.
function TestDialog({ open, rules, recipients, connection, onClose }) {
  const [to, setTo] = useState("recipient");
  const [rid, setRid] = useState("");
  const [cc, setCc] = useState("+91");
  const [number, setNumber] = useState("");
  const [eventKey, setEventKey] = useState("daily_summary");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (open) {
      setResult(null);
      setError("");
      setRid(recipients[0]?.id ?? "");
      setTo(recipients.length ? "recipient" : "number");
    }
  }, [open, recipients]);

  async function send() {
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const body = to === "recipient" ? { recipient_id: Number(rid), event_key: eventKey } : { country_code: cc, number, event_key: eventKey };
      setResult(await api.connectApi.test(body));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <DarkModal open={open} onClose={onClose} title="Send test notification">
      <div className="space-y-4 text-sm">
        <div className="flex gap-1 rounded-xl bg-white/[0.04] p-1 w-fit">
          {[["recipient", "A recipient"], ["number", "Another number"]].map(([k, l]) => (
            <button key={k} type="button" disabled={k === "recipient" && !recipients.length} onClick={() => setTo(k)}
              className={`rounded-lg px-3 py-1.5 text-xs disabled:opacity-40 ${to === k ? "bg-white text-[#0b1020] font-medium" : "text-slate-400"}`}>{l}</button>
          ))}
        </div>
        {to === "recipient" ? (
          <label className="block">
            <Label>Recipient</Label>
            <select value={rid} onChange={(e) => setRid(e.target.value)} className={field}>
              {recipients.map((r) => <option key={r.id} value={r.id} className="bg-[#0f1528]">{r.name} · {r.role} · {r.phone_masked}</option>)}
            </select>
          </label>
        ) : (
          <div className="grid grid-cols-[110px_1fr] gap-3">
            <label className="block"><Label>Code</Label>
              <select value={cc} onChange={(e) => setCc(e.target.value)} className={field}>{COUNTRY_CODES.map(([x]) => <option key={x} className="bg-[#0f1528]">{x}</option>)}</select>
            </label>
            <label className="block"><Label>Phone number</Label><input inputMode="tel" value={number} onChange={(e) => setNumber(e.target.value)} className={field} /></label>
          </div>
        )}
        <label className="block">
          <Label>Sample message</Label>
          <select value={eventKey} onChange={(e) => setEventKey(e.target.value)} className={field}>
            {rules.map((r) => <option key={r.key} value={r.key} className="bg-[#0f1528]">{r.label}</option>)}
          </select>
        </label>
        {connection && connection.status !== "connected" && (
          <p className="text-xs text-amber-200/90">WhatsApp is not connected, so the test will be logged as Not sent.</p>
        )}
        {error && <p className="text-xs text-rose-300">{error}</p>}
        {result && (
          <div className={`rounded-xl p-3 ring-1 ${result.status === "sent" ? "bg-emerald-500/10 ring-emerald-400/30" : "bg-rose-500/10 ring-rose-400/30"}`}>
            <p className="flex items-center gap-2 font-medium text-white">
              {result.status === "sent" ? <CheckCheck size={16} className="text-emerald-300" /> : <XCircle size={16} className="text-rose-300" />}
              <MsgBadge status={result.status} />
              {result.status === "sent" ? "Meta accepted the message." : result.error}
            </p>
            {result.status === "sent" && <p className="text-[11px] text-slate-400 mt-1">Delivery is confirmed separately by Meta and appears in the history.</p>}
          </div>
        )}
        <div className="flex justify-end gap-2">
          <button type="button" className={btnGhost} onClick={onClose}>Close</button>
          <button type="button" className={btnPrimary} disabled={busy || (to === "recipient" ? !rid : !number.trim())} onClick={send}>
            {busy ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />} Send test
          </button>
        </div>
      </div>
    </DarkModal>
  );
}
