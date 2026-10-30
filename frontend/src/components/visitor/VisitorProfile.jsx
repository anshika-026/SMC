import { useCallback, useEffect, useState } from "react";
import { Building2, Mail, Pencil, Phone, Trash2 } from "lucide-react";
import Modal from "../Modal";
import * as api from "../../api/client";
import { clock, dur, MediaImage, niceDate, StatusPill } from "./shared";

// A visitor's permanent profile: details, visit counts, every visit with its
// times, waiting and duration, and the snapshots still kept.

export default function VisitorProfile({ visitorId, onClose, onOpenVisit, onChanged }) {
  const [p, setP] = useState(null);
  const [error, setError] = useState("");
  const [edit, setEdit] = useState(null);

  const load = useCallback(() => {
    if (!visitorId) return;
    api.vr.profile(visitorId).then((d) => { setP(d); setError(""); }).catch((e) => setError(e.message));
  }, [visitorId]);

  useEffect(() => {
    setP(null);
    setEdit(null);
    load();
  }, [load]);

  async function save() {
    try {
      await api.vr.updateProfile(p.id, edit);
      setEdit(null);
      load();
      onChanged?.();
    } catch (e) {
      setError(e.message);
    }
  }

  async function erase() {
    if (!window.confirm(`Erase ${p.name} completely? Their profile, all ${p.total_visits} visit(s), snapshots and clips are deleted permanently.`)) return;
    try {
      await api.vr.eraseVisitor(p.id);
      onChanged?.();
      onClose();
    } catch (e) {
      setError(e.message);
    }
  }

  const snapshots = (p?.media || []).filter((m) => m.kind === "snapshot");

  return (
    <Modal open={!!visitorId} onClose={onClose} width="max-w-4xl" title={p ? "Visitor profile" : ""}>
      {!p && !error && <p className="text-sm text-slate-400 py-10 text-center">Loading…</p>}
      {error && <p className="text-xs text-danger-500 mb-3">{error}</p>}
      {p && (
        <div className="space-y-5 text-sm">
          <div className="flex flex-col sm:flex-row gap-4">
            <MediaImage mediaId={snapshots[0]?.id} className="w-24 h-28 rounded-xl shrink-0" />
            <div className="flex-1 min-w-0">
              {edit ? (
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
                  {["name", "company", "mobile", "email"].map((k) => (
                    <input key={k} className="input-field" placeholder={k[0].toUpperCase() + k.slice(1)} value={edit[k] ?? ""}
                      onChange={(e) => setEdit({ ...edit, [k]: e.target.value })} />
                  ))}
                  <textarea className="input-field sm:col-span-2" placeholder="Notes" value={edit.notes ?? ""} onChange={(e) => setEdit({ ...edit, notes: e.target.value })} />
                  <p className="text-[11px] text-slate-400 sm:col-span-2">Past visits keep the name and company they were registered with.</p>
                  <div className="sm:col-span-2 flex justify-end gap-2">
                    <button type="button" className="btn-secondary text-xs" onClick={() => setEdit(null)}>Cancel</button>
                    <button type="button" className="btn-primary text-xs" onClick={save}>Save profile</button>
                  </div>
                </div>
              ) : (
                <>
                  <div className="flex items-start gap-2">
                    <h3 className="text-lg font-semibold text-ink-900">{p.name}</h3>
                    <span className="text-xs text-slate-400 mt-1.5">{p.visitor_code}</span>
                    <button type="button" title="Edit profile" className="ml-auto text-slate-400 hover:text-brand-600"
                      onClick={() => setEdit({ name: p.name, company: p.company, mobile: p.mobile, email: p.email, notes: p.notes })}>
                      <Pencil size={14} />
                    </button>
                  </div>
                  <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500 mt-1">
                    <span className="inline-flex items-center gap-1"><Building2 size={12} />{p.company || "No company"}</span>
                    {p.mobile && <span className="inline-flex items-center gap-1"><Phone size={12} />{p.mobile}</span>}
                    {p.email && <span className="inline-flex items-center gap-1"><Mail size={12} />{p.email}</span>}
                  </div>
                  {p.notes && <p className="text-xs text-slate-500 mt-2">{p.notes}</p>}
                </>
              )}
            </div>
          </div>

          <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
            <Stat label="Total visits" value={p.total_visits} />
            <Stat label="Visits this month" value={p.visits_this_month} />
            <Stat label="First visit" value={niceDate(p.first_visit)} />
            <Stat label="Most recent visit" value={niceDate(p.last_visit)} />
          </div>

          <div>
            <p className="text-xs font-semibold text-ink-900 mb-2">Visit history</p>
            <div className="overflow-x-auto rounded-xl ring-1 ring-slate-100">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Visit date</th><th>Purpose</th><th>Person visited</th><th>Entry</th><th>Exit</th>
                    <th>Waiting</th><th>Duration</th><th>Snapshot</th><th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {p.visits.length === 0 && (
                    <tr><td colSpan={9} className="text-center text-slate-400 py-6">No visits.</td></tr>
                  )}
                  {p.visits.map((v) => (
                    <tr key={v.id} className="cursor-pointer hover:bg-slate-50" onClick={() => onOpenVisit(v.id)}>
                      <td className="whitespace-nowrap">{niceDate(v.visit_date)}<div className="text-[11px] text-slate-400">{v.visit_code}</div></td>
                      <td>{v.purpose}</td>
                      <td>{v.host_name}</td>
                      <td>{clock(v.entry_at)}</td>
                      <td>{clock(v.exit_at)}</td>
                      <td>{dur(v.waiting_seconds)}</td>
                      <td>{v.status === "in" ? <span className="text-emerald-700">{dur(v.elapsed_seconds)} so far</span> : dur(v.duration_seconds)}</td>
                      <td>{v.snapshot_id ? "Yes" : v.snapshot_consent ? "No" : "Declined"}</td>
                      <td><StatusPill status={v.status} /></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {snapshots.length > 0 && (
            <div>
              <p className="text-xs font-semibold text-ink-900 mb-2">Snapshots</p>
              <div className="flex gap-3 flex-wrap">
                {snapshots.map((m) => (
                  <button type="button" key={m.id} className="text-left" onClick={() => onOpenVisit(m.visit_id)}>
                    <MediaImage mediaId={m.id} className="w-24 h-28 rounded-xl" />
                    <p className="text-[11px] text-slate-500 mt-1">{niceDate(m.visit_date)}</p>
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="pt-3 border-t border-slate-100 flex justify-between items-center">
            <p className="text-[11px] text-slate-400">Registered {new Date(p.created_at * 1000).toLocaleDateString()}</p>
            <button type="button" className="text-xs text-danger-500 inline-flex items-center gap-1" onClick={erase}>
              <Trash2 size={12} /> Erase visitor data
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

function Stat({ label, value }) {
  return (
    <div className="rounded-xl bg-slate-50 px-3 py-2">
      <p className="text-[11px] text-slate-500">{label}</p>
      <p className="text-base font-semibold text-ink-900">{value}</p>
    </div>
  );
}
