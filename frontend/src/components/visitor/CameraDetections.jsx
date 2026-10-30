import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, LogIn, LogOut } from "lucide-react";
import * as api from "../../api/client";
import { todayIso } from "../workforce/shared";
import { clock } from "./shared";

// The day's camera entries and exits that can become a visit's entry/exit
// time. A detection is a person, not an identity: it is only attached to a
// visit when the operator confirms it here or in the visit panel.

export default function CameraDetections({ visits, onOpenVisit, onChanged }) {
  const [day, setDay] = useState(todayIso());
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [pick, setPick] = useState({});

  const load = useCallback(() => {
    api.vr.detections(day).then((d) => { setData(d); setError(""); }).catch((e) => setError(e.message));
  }, [day]);
  useEffect(() => {
    setData(null);
    load();
  }, [load]);

  async function link(d) {
    try {
      await api.vr.link(Number(pick[d.ref]), d.ref);
      setPick((p) => ({ ...p, [d.ref]: "" }));
      load();
      onChanged?.();
    } catch (e) {
      setError(e.message);
    }
  }

  const cams = data?.cameras || [];
  const entryCams = cams.filter((c) => c.entry_detection);
  const exitCams = cams.filter((c) => c.exit_detection);
  const open = (visits || []).filter((v) => v.visit_date === day);

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 space-y-3 text-xs">
        <div className="flex items-center gap-3 flex-wrap">
          <h3 className="text-sm font-semibold text-ink-900 mr-auto">Camera entries & exits</h3>
          <input type="date" className="input-field text-xs w-auto" value={day} max={todayIso()} onChange={(e) => setDay(e.target.value)} />
        </div>
        <div className="grid sm:grid-cols-2 gap-2">
          <Capability ok={entryCams.length > 0} title="Entry detection"
            text={entryCams.length ? `From ${entryCams.map((c) => c.name).join(", ")} (footfall counting or a visitor seating area).` : "No camera counts entries. Turn on Footfall for the gate camera."} />
          <Capability ok={exitCams.length > 0} title="Exit detection"
            text={exitCams.length ? `From ${exitCams.map((c) => c.name).join(", ")} (gate entry line).`
              : "No gate camera has an entry line, so exits are not detected at the gate. Draw an entry line on the gate camera in Footfall, or check visitors out at reception. Leaving a visitor seating area is still shown."} />
        </div>
        <p className="text-slate-500">Employees recognised at the gate are not listed. Confirm a detection only when you know which visitor it is.</p>
      </div>

      {error && <p className="text-sm text-danger-500">{error}</p>}
      <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] overflow-x-auto">
        <table className="data-table">
          <thead>
            <tr><th>Time</th><th>Event</th><th>Camera</th><th>Clip</th><th>Visit</th></tr>
          </thead>
          <tbody>
            {!data && !error && <tr><td colSpan={5} className="text-center text-slate-400 py-8">Loading…</td></tr>}
            {data && data.detections.length === 0 && <tr><td colSpan={5} className="text-center text-slate-400 py-8">No camera entries or exits on this day.</td></tr>}
            {(data?.detections || []).map((d) => {
              const choices = open.filter((v) => (d.kind === "entry" ? v.status === "pending" : v.status === "in" && v.entry_at <= d.ts));
              return (
                <tr key={d.ref}>
                  <td className="whitespace-nowrap">{clock(d.ts)}</td>
                  <td>
                    <span className="inline-flex items-center gap-1.5">
                      {d.kind === "entry" ? <LogIn size={13} className="text-emerald-600" /> : <LogOut size={13} className="text-slate-500" />}
                      {d.source}
                    </span>
                  </td>
                  <td>{d.camera_name}</td>
                  <td>{d.clip ? "Yes" : "—"}</td>
                  <td className="min-w-64">
                    {d.linked ? (
                      <button type="button" className="text-brand-600 inline-flex items-center gap-1" onClick={() => onOpenVisit(d.linked.visit_id)}>
                        <CheckCircle2 size={13} /> {d.linked.visitor_name} · {d.linked.visit_code}
                      </button>
                    ) : choices.length === 0 ? (
                      <span className="text-[11px] text-slate-400">{d.kind === "entry" ? "No visitor waiting for check-in" : "No visitor inside"}</span>
                    ) : (
                      <div className="flex gap-1.5">
                        <select className="input-field text-xs py-1" value={pick[d.ref] || ""} onChange={(e) => setPick((p) => ({ ...p, [d.ref]: e.target.value }))}>
                          <option value="">This is…</option>
                          {choices.map((v) => <option key={v.id} value={v.id}>{v.visitor_name} · to meet {v.host_name}</option>)}
                        </select>
                        <button type="button" className="btn-primary text-xs px-3" disabled={!pick[d.ref]} onClick={() => link(d)}>Confirm</button>
                      </div>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Capability({ ok, title, text }) {
  return (
    <div className={`rounded-xl px-3 py-2 flex gap-2 ${ok ? "bg-emerald-50/60" : "bg-amber-50"}`}>
      {ok ? <CheckCircle2 size={15} className="text-emerald-600 shrink-0 mt-0.5" /> : <AlertTriangle size={15} className="text-amber-600 shrink-0 mt-0.5" />}
      <div>
        <p className="font-semibold text-ink-900">{title}: {ok ? "available" : "not available"}</p>
        <p className="text-slate-600">{text}</p>
      </div>
    </div>
  );
}
