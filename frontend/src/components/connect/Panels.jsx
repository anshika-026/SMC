import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Bell, CalendarClock, Footprints, Clock, ShieldAlert, Timer, Trash2, UserPlus, Users, BarChart3 } from "lucide-react";
import * as api from "../../api/client";
import { Bubble, COUNTRY_CODES, Label, MsgBadge, Panel, Switch, btnGhost, btnPrimary, field } from "./ui";

const RULE_ICON = {
  intrusion: ShieldAlert, loitering: Timer, visitor: Users, attendance: CalendarClock,
  late_arrival: Clock, footfall: Footprints, daily_summary: BarChart3,
};
const PRIORITY_CLS = { High: "text-rose-300", Normal: "text-sky-300", Low: "text-slate-400" };
const when = (ts) => new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// ---------------------------------------------------------------- rules

export function RulesPanel({ rules, recipients, onSaved, onError }) {
  return (
    <div className="grid gap-3 lg:grid-cols-2">
      {rules.map((r) => (
        <RuleCard key={r.key} rule={r} recipients={recipients} onSaved={onSaved} onError={onError} />
      ))}
    </div>
  );
}

function RuleCard({ rule, recipients, onSaved, onError }) {
  const [draft, setDraft] = useState(rule);
  const [saving, setSaving] = useState(false);
  useEffect(() => setDraft(rule), [rule]);
  const Icon = RULE_ICON[rule.key] || Bell;
  const dirty = JSON.stringify([draft.enabled, draft.recipient_ids, draft.priority, draft.preferences]) !== JSON.stringify([rule.enabled, rule.recipient_ids, rule.priority, rule.preferences]);
  const pref = (k) => (e) => setDraft((d) => ({ ...d, preferences: { ...d.preferences, [k]: e.target.type === "number" ? Number(e.target.value) : e.target.value } }));
  const toggleRecipient = (id) => setDraft((d) => ({ ...d, recipient_ids: d.recipient_ids.includes(id) ? d.recipient_ids.filter((x) => x !== id) : [...d.recipient_ids, id] }));

  async function save(patch = {}) {
    setSaving(true);
    try {
      const next = { ...draft, ...patch };
      onSaved(await api.connectApi.updateRule(rule.key, {
        enabled: next.enabled, recipient_ids: next.recipient_ids, priority: next.priority, preferences: next.preferences,
      }));
    } catch (e) {
      onError(e.message);
      setDraft(rule);
    } finally {
      setSaving(false);
    }
  }

  return (
    <Panel className="p-4 space-y-3">
      <div className="flex items-start gap-3">
        <span className={`w-9 h-9 rounded-xl grid place-items-center shrink-0 ${draft.enabled ? "bg-[#25D366]/15 text-[#25D366]" : "bg-white/5 text-slate-500"}`}>
          <Icon size={17} />
        </span>
        <div className="flex-1 min-w-0">
          <p className="font-medium text-white">{rule.label}</p>
          <p className="text-[11px] text-slate-500">{rule.kind === "report" ? "Daily report" : "Sent when it happens"}</p>
        </div>
        <Switch checked={draft.enabled} label={`${rule.label} on/off`} disabled={saving} onChange={(v) => { setDraft((d) => ({ ...d, enabled: v })); save({ enabled: v }); }} />
      </div>

      <div>
        <Label>Recipients</Label>
        {recipients.length === 0 ? (
          <p className="text-xs text-slate-500">Add recipients first (Recipients tab).</p>
        ) : (
          <div className="flex flex-wrap gap-1.5">
            {recipients.map((p) => {
              const on = draft.recipient_ids.includes(p.id);
              return (
                <button key={p.id} type="button" onClick={() => toggleRecipient(p.id)}
                  className={`rounded-full px-2.5 py-1 text-xs ring-1 transition ${on ? "bg-[#25D366]/15 text-[#7ef0a8] ring-[#25D366]/40" : "bg-white/[0.03] text-slate-400 ring-white/10 hover:text-slate-200"}`}>
                  {p.name} <span className="opacity-60">· {p.role}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <label className="block">
          <Label>Priority</Label>
          <select value={draft.priority} onChange={(e) => setDraft((d) => ({ ...d, priority: e.target.value }))} className={`${field} ${PRIORITY_CLS[draft.priority]}`}>
            {["High", "Normal", "Low"].map((p) => <option key={p} className="bg-[#0f1528] text-slate-100">{p}</option>)}
          </select>
        </label>
        {rule.kind === "report" ? (
          <label className="block">
            <Label>Send at</Label>
            <input type="time" value={draft.preferences.send_at || ""} onChange={pref("send_at")} className={field} />
          </label>
        ) : (
          <label className="block">
            <Label>Cooldown (min)</Label>
            <input type="number" min={0} max={1440} value={draft.preferences.cooldown_minutes ?? 0} onChange={pref("cooldown_minutes")} className={field} />
          </label>
        )}
        <label className="block">
          <Label>Quiet from</Label>
          <input type="time" value={draft.preferences.quiet_from || ""} onChange={pref("quiet_from")} className={field} />
        </label>
        <label className="block">
          <Label>Quiet until</Label>
          <input type="time" value={draft.preferences.quiet_to || ""} onChange={pref("quiet_to")} className={field} />
        </label>
      </div>
      <p className="text-[11px] text-slate-500">
        In quiet hours only High priority messages are sent.{rule.kind === "event" ? " The cooldown stops repeats of the same kind of alert within that many minutes." : ""}
      </p>
      {dirty && (
        <div className="flex justify-end gap-2">
          <button type="button" className={btnGhost} onClick={() => setDraft(rule)}>Discard</button>
          <button type="button" className={btnPrimary} disabled={saving} onClick={() => save()}>Save rule</button>
        </div>
      )}
    </Panel>
  );
}

// ---------------------------------------------------------------- recipients

const ROLES = ["Admin", "Security Manager", "HR Manager", "Reception"];

export function RecipientsPanel({ recipients, onChanged, onError }) {
  const [form, setForm] = useState({ name: "", country_code: "+91", number: "", role: "Admin" });
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));

  async function add(e) {
    e.preventDefault();
    setBusy(true);
    try {
      await api.connectApi.addRecipient(form);
      setForm((f) => ({ ...f, name: "", number: "" }));
      onChanged();
    } catch (err) {
      onError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function patch(r, f) {
    try {
      await api.connectApi.updateRecipient(r.id, f);
      onChanged();
    } catch (e) {
      onError(e.message);
    }
  }

  async function remove(r) {
    if (!window.confirm(`Remove ${r.name}? They will stop receiving notifications.`)) return;
    await api.connectApi.deleteRecipient(r.id);
    onChanged();
  }

  return (
    <div className="space-y-4">
      <Panel className="p-4">
        <form onSubmit={add} className="grid gap-3 sm:grid-cols-[1.2fr_110px_1fr_1fr_auto] items-end">
          <label className="block"><Label>Name</Label><input value={form.name} onChange={set("name")} placeholder="e.g. Neha Sharma" className={field} /></label>
          <label className="block">
            <Label>Code</Label>
            <select value={form.country_code} onChange={set("country_code")} className={field}>
              {COUNTRY_CODES.map(([c]) => <option key={c} className="bg-[#0f1528]">{c}</option>)}
            </select>
          </label>
          <label className="block"><Label>Phone number</Label><input inputMode="tel" value={form.number} onChange={set("number")} placeholder="98765 43210" className={field} /></label>
          <label className="block">
            <Label>Role</Label>
            <select value={form.role} onChange={set("role")} className={field}>
              {ROLES.map((r) => <option key={r} className="bg-[#0f1528]">{r}</option>)}
            </select>
          </label>
          <button type="submit" className={btnPrimary} disabled={busy || !form.name.trim() || !form.number.trim()}><UserPlus size={15} /> Add</button>
        </form>
      </Panel>
      <Panel className="overflow-hidden">
        {recipients.length === 0 ? (
          <p className="p-8 text-center text-sm text-slate-500">No recipients yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-[11px] uppercase tracking-wide text-slate-500">
              <tr className="border-b border-white/10"><th className="text-left px-4 py-2.5">Name</th><th className="text-left px-4">Phone</th><th className="text-left px-4">Role</th><th className="text-left px-4">Receives</th><th /></tr>
            </thead>
            <tbody>
              {recipients.map((r) => (
                <tr key={r.id} className="border-b border-white/5 last:border-0">
                  <td className="px-4 py-2.5 text-white">{r.name}</td>
                  <td className="px-4 font-mono text-xs text-slate-300">{r.phone_masked}</td>
                  <td className="px-4">
                    <select value={r.role} onChange={(e) => patch(r, { role: e.target.value })} className="bg-transparent text-slate-200 text-sm focus:outline-none">
                      {ROLES.map((x) => <option key={x} className="bg-[#0f1528]">{x}</option>)}
                    </select>
                  </td>
                  <td className="px-4"><Switch checked={r.active} label={`${r.name} active`} onChange={(v) => patch(r, { active: v })} /></td>
                  <td className="px-4 text-right">
                    <button type="button" aria-label={`Remove ${r.name}`} className="text-slate-500 hover:text-rose-300" onClick={() => remove(r)}><Trash2 size={15} /></button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

// ---------------------------------------------------------------- templates

export function TemplatesPanel({ rules, onSaved, onError }) {
  const [key, setKey] = useState(rules[0]?.key);
  const rule = rules.find((r) => r.key === key) || rules[0];
  const [text, setText] = useState(rule?.template || "");
  const [preview, setPreview] = useState(rule?.preview || "");
  const [saving, setSaving] = useState(false);
  const area = useRef(null);

  useEffect(() => {
    if (!rule) return;
    setText(rule.template);
    setPreview(rule.preview);
  }, [rule?.key, rule?.template]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!rule) return undefined;
    const t = setTimeout(() => api.connectApi.preview(rule.key, text).then((p) => setPreview(p.preview)).catch(() => {}), 250);
    return () => clearTimeout(t);
  }, [text, rule?.key]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!rule) return null;

  function insert(f) {
    const el = area.current;
    const at = el ? el.selectionStart : text.length;
    setText((t) => `${t.slice(0, at)}{${f}}${t.slice(at)}`);
  }

  async function save(reset = false) {
    setSaving(true);
    try {
      onSaved(await api.connectApi.updateRule(rule.key, reset ? { reset_template: true } : { template: text }));
    } catch (e) {
      onError(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[220px_1fr_1fr]">
      <Panel className="p-2 h-fit">
        {rules.map((r) => (
          <button key={r.key} type="button" onClick={() => setKey(r.key)}
            className={`w-full text-left rounded-xl px-3 py-2 text-sm ${r.key === rule.key ? "bg-white/10 text-white" : "text-slate-400 hover:text-slate-200"}`}>
            {r.label}
            {r.customized && <span className="ml-1 text-[10px] text-[#7ef0a8]">edited</span>}
          </button>
        ))}
      </Panel>
      <Panel className="p-4 space-y-3">
        <Label>Message</Label>
        <textarea ref={area} value={text} onChange={(e) => setText(e.target.value)} rows={10} maxLength={1000} className={`${field} font-mono text-[13px] leading-relaxed`} />
        <div>
          <Label>Insert a field</Label>
          <div className="flex flex-wrap gap-1.5">
            {rule.fields.map((f) => (
              <button key={f} type="button" onClick={() => insert(f)} className="rounded-full bg-white/[0.06] ring-1 ring-white/10 px-2 py-0.5 text-[11px] font-mono text-slate-300 hover:text-white">
                {`{${f}}`}
              </button>
            ))}
          </div>
        </div>
        <p className="text-[11px] text-slate-500">*bold* and _italic_ work as in WhatsApp. A field is filled from the real event when the message is sent.</p>
        <div className="flex justify-end gap-2">
          {rule.customized && <button type="button" className={btnGhost} disabled={saving} onClick={() => save(true)}>Reset to default</button>}
          <button type="button" className={btnPrimary} disabled={saving || text === rule.template || !text.trim()} onClick={() => save(false)}>Save template</button>
        </div>
      </Panel>
      <div className="space-y-2">
        <Label>Preview</Label>
        <Bubble text={preview} note="Preview with sample values. Real messages use the actual event's details." />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- history

export function HistoryPanel({ rules, refreshKey }) {
  const [f, setF] = useState({ q: "", event: "", status: "", start: "", end: "" });
  const [rows, setRows] = useState(null);
  const [open, setOpen] = useState(null);
  useEffect(() => {
    const t = setTimeout(() => {
      const clean = Object.fromEntries(Object.entries(f).filter(([, v]) => v));
      api.connectApi.history(clean).then((d) => setRows(d.messages)).catch(() => setRows([]));
    }, 200);
    return () => clearTimeout(t);
  }, [f, refreshKey]);
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));
  const labels = useMemo(() => Object.fromEntries(rules.map((r) => [r.key, r.label])), [rules]);

  return (
    <div className="space-y-3">
      <Panel className="p-3 grid gap-2 sm:grid-cols-[1fr_180px_150px_150px_150px]">
        <input value={f.q} onChange={set("q")} placeholder="Search recipient or message" className={field} />
        <select value={f.event} onChange={set("event")} className={field}>
          <option value="" className="bg-[#0f1528]">All notifications</option>
          {rules.map((r) => <option key={r.key} value={r.key} className="bg-[#0f1528]">{r.label}</option>)}
        </select>
        <select value={f.status} onChange={set("status")} className={field}>
          <option value="" className="bg-[#0f1528]">Any status</option>
          {["sent", "delivered", "read", "failed", "not_sent"].map((s) => <option key={s} value={s} className="bg-[#0f1528]">{s.replace("_", " ")}</option>)}
        </select>
        <input type="date" value={f.start} onChange={set("start")} className={field} aria-label="From" />
        <input type="date" value={f.end} onChange={set("end")} className={field} aria-label="To" />
      </Panel>
      <Panel className="overflow-hidden">
        {!rows ? (
          <p className="p-8 text-center text-sm text-slate-500">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="p-8 text-center text-sm text-slate-500">No notifications yet.</p>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-[11px] uppercase tracking-wide text-slate-500">
              <tr className="border-b border-white/10"><th className="text-left px-4 py-2.5">Time</th><th className="text-left px-4">Notification</th><th className="text-left px-4">Recipient</th><th className="text-left px-4">Status</th></tr>
            </thead>
            <tbody>
              {rows.map((m) => (
                <Fragment key={m.id}>
                  <tr onClick={() => setOpen(open === m.id ? null : m.id)} className="border-b border-white/5 cursor-pointer hover:bg-white/[0.03]">
                    <td className="px-4 py-2.5 text-slate-400 tabular-nums whitespace-nowrap">{when(m.ts)}</td>
                    <td className="px-4 text-white">{labels[m.event_key] || m.event_label}{m.is_test && <span className="ml-1.5 text-[10px] text-indigo-300">test</span>}</td>
                    <td className="px-4 text-slate-300">{m.recipient_name} <span className="font-mono text-[11px] text-slate-500">{m.phone_masked}</span></td>
                    <td className="px-4"><MsgBadge status={m.status} /></td>
                  </tr>
                  {open === m.id && (
                    <tr className="border-b border-white/5">
                      <td colSpan={4} className="px-4 py-3">
                        <div className="grid gap-3 md:grid-cols-2">
                          <Bubble text={m.body} />
                          <div className="text-xs text-slate-400 space-y-1">
                            <p>Status: <MsgBadge status={m.status} /> {m.status_at && m.status_at !== m.ts ? `· updated ${when(m.status_at)}` : ""}</p>
                            {m.error && <p className="text-rose-300">Reason: {m.error}</p>}
                            <p>{m.has_meta_id ? "Meta message id recorded" : "No Meta message id (not accepted by Meta)"}</p>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}

