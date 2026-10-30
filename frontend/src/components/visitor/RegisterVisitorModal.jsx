import { useEffect, useMemo, useState } from "react";
import { Camera, ShieldCheck, UserCheck } from "lucide-react";
import Modal from "../Modal";
import * as api from "../../api/client";
import { todayIso } from "../workforce/shared";
import { Field, niceDate } from "./shared";

// Visitor registration. Returning visitors are found by mobile / e-mail /
// name as the operator types, and the operator chooses to link the visit to
// that profile; nothing is matched by face.

const PURPOSES = ["Meeting", "Interview", "Delivery", "Vendor / supplier", "Maintenance", "Client visit", "Personal"];
const EMPTY = {
  name: "", purpose: "Meeting", purposeOther: "", host_employee_id: "", visit_date: todayIso(),
  company: "", mobile: "", email: "", notes: "", gate_camera_id: "", snapshot_consent: false,
};

export default function RegisterVisitorModal({ open, onClose, onRegistered, settings }) {
  const [form, setForm] = useState(EMPTY);
  const [hosts, setHosts] = useState([]);
  const [cameras, setCameras] = useState([]);
  const [matches, setMatches] = useState([]);
  const [linked, setLinked] = useState(null);        // the existing profile chosen
  const [dupError, setDupError] = useState(false);   // the server found a likely duplicate
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setForm({ ...EMPTY, visit_date: todayIso(), gate_camera_id: settings?.default_gate_camera_id ?? "" });
    setMatches([]);
    setLinked(null);
    setDupError(false);
    setError("");
    api.vr.hosts().then((d) => setHosts(d.hosts)).catch(() => setHosts([]));
    api.vr.cameras().then((d) => setCameras(d.cameras)).catch(() => setCameras([]));
  }, [open, settings]);

  // Possible returning visitor, looked up as the operator types (debounced).
  useEffect(() => {
    if (!open || linked) return undefined;
    const { name, mobile, email, company } = form;
    if (name.trim().length < 3 && mobile.replace(/\D/g, "").length < 7 && !email.includes("@")) {
      setMatches([]);
      return undefined;
    }
    const t = setTimeout(() => {
      api.vr.match({ name, mobile, email, company }).then((d) => setMatches(d.matches)).catch(() => {});
    }, 350);
    return () => clearTimeout(t);
  }, [open, linked, form.name, form.mobile, form.email, form.company]); // eslint-disable-line react-hooks/exhaustive-deps

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
  const purpose = form.purpose === "Other" ? form.purposeOther : form.purpose;
  const host = hosts.find((h) => h.employee_id === form.host_employee_id);
  const gateCams = useMemo(() => cameras.filter((c) => c.entry_detection), [cameras]);

  function chooseProfile(m) {
    setLinked(m);
    setDupError(false);
    setForm((f) => ({ ...f, name: m.name, company: f.company || m.company || "" }));
  }

  async function submit(e, createNew = false) {
    e?.preventDefault();
    setBusy(true);
    setError("");
    try {
      const visit = await api.vr.register({
        name: form.name, purpose, host_employee_id: form.host_employee_id || null, visit_date: form.visit_date,
        company: form.company, mobile: form.mobile, email: form.email, notes: form.notes,
        gate_camera_id: form.gate_camera_id === "" ? null : Number(form.gate_camera_id),
        snapshot_consent: form.snapshot_consent, visitor_id: linked?.id || null, create_new: createNew,
      });
      onRegistered(visit);
    } catch (err) {
      if (err.status === 409) {
        setMatches(err.matches);
        setDupError(true);
      } else setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} title="Register visitor" width="max-w-3xl">
      <form onSubmit={submit} className="grid grid-cols-1 lg:grid-cols-5 gap-5 text-sm">
        <div className="lg:col-span-3 space-y-3">
          {linked ? (
            <div className="flex items-center gap-3 rounded-xl bg-brand-50 px-3 py-2.5">
              <UserCheck size={18} className="text-brand-600 shrink-0" />
              <div className="min-w-0 flex-1">
                <p className="font-medium text-ink-900 truncate">{linked.name} · {linked.visitor_code}</p>
                <p className="text-xs text-slate-500">Returning visitor · {linked.total_visits} earlier visit{linked.total_visits === 1 ? "" : "s"}</p>
              </div>
              <button type="button" className="text-xs text-brand-600 font-medium" onClick={() => { setLinked(null); setForm((f) => ({ ...f, name: "" })); }}>
                Change
              </button>
            </div>
          ) : (
            <Field label="Visitor name *">
              <input className="input-field" value={form.name} onChange={set("name")} required autoFocus maxLength={120} />
            </Field>
          )}
          <div className="grid grid-cols-2 gap-3">
            <Field label="Purpose of visit *">
              <select className="input-field" value={form.purpose} onChange={set("purpose")}>
                {PURPOSES.map((p) => <option key={p}>{p}</option>)}
                <option value="Other">Other…</option>
              </select>
            </Field>
            <Field label="Visit date *">
              <input type="date" className="input-field" value={form.visit_date} onChange={set("visit_date")} required />
            </Field>
          </div>
          {form.purpose === "Other" && (
            <Field label="Describe the purpose *">
              <input className="input-field" value={form.purposeOther} onChange={set("purposeOther")} required maxLength={200} />
            </Field>
          )}
          <Field label="Person to meet *" hint={host?.department ? `Department: ${host.department}` : undefined}>
            <select className="input-field" value={form.host_employee_id} onChange={set("host_employee_id")} required>
              <option value="">Choose an employee…</option>
              {hosts.map((h) => (
                <option key={h.employee_id} value={h.employee_id}>
                  {h.name}{h.department ? ` · ${h.department}` : ""}
                </option>
              ))}
            </select>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Company / organisation">
              <input className="input-field" value={form.company} onChange={set("company")} maxLength={120} />
            </Field>
            <Field label="Mobile number">
              <input className="input-field" value={form.mobile} onChange={set("mobile")} inputMode="tel" maxLength={20} />
            </Field>
          </div>
          <Field label="E-mail">
            <input type="email" className="input-field" value={form.email} onChange={set("email")} maxLength={120} />
          </Field>
          <Field label="Additional notes">
            <textarea className="input-field min-h-[64px]" value={form.notes} onChange={set("notes")} maxLength={1000} />
          </Field>
        </div>

        <div className="lg:col-span-2 space-y-3">
          {!linked && matches.length > 0 && (
            <div className={`rounded-xl p-3 ${dupError ? "bg-amber-50 ring-1 ring-amber-200" : "bg-slate-50"}`}>
              <p className="text-xs font-semibold text-ink-900 mb-1">{dupError ? "Already registered?" : "Visited before?"}</p>
              <p className="text-[11px] text-slate-500 mb-2">
                {dupError ? "This matches an existing visitor. Link the visit to their profile, or confirm it is someone else." : "Possible matching profiles:"}
              </p>
              <ul className="space-y-1.5">
                {matches.map((m) => (
                  <li key={m.id} className="flex items-center gap-2 bg-white rounded-lg px-2.5 py-2">
                    <div className="min-w-0 flex-1">
                      <p className="text-xs font-medium text-ink-900 truncate">{m.name} <span className="text-slate-400">{m.visitor_code}</span></p>
                      <p className="text-[11px] text-slate-500 truncate">
                        {m.reason}{m.company ? ` · ${m.company}` : ""} · {m.total_visits} visit{m.total_visits === 1 ? "" : "s"}{m.last_visit ? `, last ${niceDate(m.last_visit)}` : ""}
                      </p>
                    </div>
                    <button type="button" className="text-xs font-medium text-brand-600 shrink-0" onClick={() => chooseProfile(m)}>
                      Same person
                    </button>
                  </li>
                ))}
              </ul>
              {dupError && (
                <button type="button" className="mt-2 text-xs text-slate-600 underline" disabled={busy} onClick={() => submit(null, true)}>
                  It's a different person - register as new
                </button>
              )}
            </div>
          )}

          <div className="rounded-xl bg-slate-50 p-3 space-y-2">
            <p className="text-xs font-semibold text-ink-900 flex items-center gap-1.5"><Camera size={13} /> Entry gate camera</p>
            <select className="input-field" value={form.gate_camera_id} onChange={set("gate_camera_id")}>
              <option value="">Not set</option>
              {gateCams.map((c) => (
                <option key={c.camera_id} value={c.camera_id}>
                  {c.name} · {c.exit_detection ? "entry + exit detection" : "entry detection only"}
                </option>
              ))}
            </select>
            <p className="text-[11px] text-slate-500">Camera detections from this gate are suggested first when you confirm the visitor's entry and exit.</p>
          </div>

          <div className="rounded-xl ring-1 ring-brand-100 bg-white p-3 space-y-2">
            <p className="text-xs font-semibold text-ink-900 flex items-center gap-1.5"><ShieldCheck size={13} className="text-brand-600" /> Visitor notice</p>
            <p className="text-[11px] text-slate-600 leading-relaxed">{settings?.notice || "This building is monitored by CCTV."}</p>
            <label className="flex items-start gap-2 text-xs text-ink-900 cursor-pointer">
              <input type="checkbox" className="mt-0.5" checked={form.snapshot_consent} onChange={set("snapshot_consent")} />
              <span>The visitor agrees to one snapshot being taken and kept with this visit.</span>
            </label>
            {!form.snapshot_consent && <p className="text-[11px] text-slate-400">Declined: no snapshot or clip is kept for this visit. Registration works the same.</p>}
          </div>

          {error && <p className="text-xs text-danger-500">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" className="btn-secondary text-sm" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn-primary text-sm" disabled={busy || !purpose || !form.host_employee_id || (!linked && !form.name.trim())}>
              {busy ? "Saving…" : linked ? "Register visit" : "Register visitor"}
            </button>
          </div>
        </div>
      </form>
    </Modal>
  );
}
