import { useCallback, useEffect, useMemo, useState } from "react";
import { Armchair, ChevronRight, DoorOpen, MapPinned, Plus, ShieldAlert, Trash2, Users } from "lucide-react";
import Modal from "../Modal";
import SidePanel from "../SidePanel";
import VisitorZoneEditor from "../VisitorZoneEditor";
import { liveCameraFor } from "../VisitorCameraGrid";
import DeskZoneEditor from "../DeskZoneEditor";
import StaffEntranceEditor from "../StaffEntranceEditor";
import FootfallAddCamera from "../FootfallAddCamera";
import RestrictedZoneEditor from "./RestrictedZoneEditor";
import * as api from "../../api/client";

// Zones & areas: every area marked on a camera, whatever it is for, in one
// list (backend zones.py). Each type keeps its own rules and its own editor:
//   Employee zones   desks (who sits where; Workforce Insights)
//   Visitor zones    visitor seating areas (unknown people -> visitor alerts;
//                    recognised employees never alert)
//   Restricted zones alert on entry (restricted_zones.py)
//   Entry / exit     the staff gate line (attendance) and footfall gates

const TYPE = {
  employee: { label: "Employee zone", icon: Armchair, cls: "bg-sky-50 text-sky-700" },
  visitor: { label: "Visitor zone", icon: Users, cls: "bg-violet-50 text-violet-700" },
  restricted: { label: "Restricted zone", icon: ShieldAlert, cls: "bg-rose-50 text-rose-700" },
  entry_exit: { label: "Entry / exit", icon: DoorOpen, cls: "bg-emerald-50 text-emerald-700" },
};
const FILTERS = [["", "All zones"], ["employee", "Employee"], ["visitor", "Visitor"], ["restricted", "Restricted"], ["entry_exit", "Entry / exit"]];

const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null);

export default function ZonesView({ visitorActivity }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("");
  const [cameras, setCameras] = useState([]);
  const [vcams, setVcams] = useState([]);
  const [gates, setGates] = useState([]);
  const [editor, setEditor] = useState(null);      // {kind, zone?}
  const [adding, setAdding] = useState(false);
  const [activity, setActivity] = useState(null);  // zone whose activity panel is open

  const load = useCallback(() => {
    api.zonesApi.list().then((d) => { setData(d); setError(""); }).catch((e) => setError(e.message));
    api.getVisitorCameras().then((d) => setVcams(d.cameras)).catch(() => {});
    api.getFootfallSummary().then((d) => setGates(d.gates)).catch(() => {});
  }, []);
  useEffect(() => {
    load();
    api.getCameras().then(setCameras).catch(() => setCameras([]));
  }, [load]);

  const zones = useMemo(() => (data?.zones || []).filter((z) => !filter || z.type === filter), [data, filter]);
  const done = () => {
    setEditor(null);
    load();
  };

  async function removeRestricted(z) {
    if (!window.confirm(`Delete the restricted zone “${z.name}”? Its activity history is kept.`)) return;
    try {
      await api.zonesApi.deleteRestricted(z.id);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  function edit(z) {
    if (z.source === "restricted") setEditor({ kind: "restricted", zone: z });
    else if (z.source === "visitor_area") setEditor({ kind: "visitor", cameraId: z.camera_id });
    else if (z.source === "desk") setEditor({ kind: "desk" });
    else if (z.source === "staff_gate") setEditor({ kind: "gate" });
    else if (z.source === "footfall_gate") setEditor({ kind: "footfall", cameraId: z.camera_id });
  }

  const vc = editor?.kind === "visitor" ? vcams.find((c) => c.camera_id === editor.cameraId) : null;

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 flex flex-wrap items-center gap-2">
        <div className="mr-auto">
          <h3 className="text-sm font-semibold text-ink-900 flex items-center gap-1.5">
            <MapPinned size={15} className="text-brand-600" /> Zones &amp; areas
          </h3>
          <p className="text-xs text-slate-500">Every area marked on a camera, in one place. Each type keeps its own rules.</p>
        </div>
        <div className="flex flex-wrap gap-1">
          {FILTERS.map(([k, label]) => (
            <button key={k || "all"} type="button" onClick={() => setFilter(k)}
              className={`px-3 py-1.5 rounded-full text-xs ${filter === k ? "bg-ink-900 text-white" : "bg-slate-50 text-slate-600 hover:bg-slate-100"}`}>
              {label}
              {data && <span className="opacity-60"> {k ? data.counts[k] : data.zones.length}</span>}
            </button>
          ))}
        </div>
        <button type="button" className="btn-primary text-sm inline-flex items-center gap-1.5" onClick={() => setAdding(true)}>
          <Plus size={15} /> Add zone
        </button>
      </div>

      {error && <p className="text-sm text-danger-500">{error}</p>}
      {!data && !error && <p className="text-sm text-slate-400 py-10 text-center">Loading zones…</p>}
      {data && zones.length === 0 && (
        <div className="bg-white rounded-2xl p-10 text-center text-sm text-slate-500">No {filter ? TYPE[filter].label.toLowerCase() + "s" : "zones"} yet. Use “Add zone”.</div>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
        {zones.map((z) => {
          const t = TYPE[z.type];
          const a = z.activity || {};
          return (
            <div key={z.key} className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 flex flex-col gap-2 min-w-0">
              <div className="flex items-start gap-2">
                <span className={`w-8 h-8 rounded-lg grid place-items-center shrink-0 ${t.cls}`}>
                  <t.icon size={16} />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-semibold text-ink-900 truncate" title={z.name}>{z.name}</p>
                  <p className="text-[11px] text-slate-500">
                    {t.label} · {z.camera}
                  </p>
                </div>
                <span className={`badge ${z.enabled ? "badge-success" : "badge-neutral"}`}>{z.enabled ? "On" : "Off"}</span>
              </div>
              <p className="text-xs text-slate-600 leading-snug">{z.rule}</p>
              <div className="flex items-baseline gap-2 text-xs mt-auto pt-1">
                <span className="text-xl font-light text-ink-900 tabular-nums">{a.count ?? 0}</span>
                <span className="text-slate-500">{a.label}</span>
                {a.alerts ? <span className="text-rose-600">· {a.alerts} alert{a.alerts === 1 ? "" : "s"}</span> : null}
                {a.employees ? <span className="text-slate-500">· {a.employees} employee{a.employees === 1 ? "" : "s"} seen, not alerted</span> : null}
                {a.exits ? <span className="text-slate-500">· {a.exits} exits</span> : null}
              </div>
              <p className="text-[11px] text-slate-400">{a.last_at ? `Last activity ${clock(a.last_at)}` : "No activity today"}</p>
              <div className="flex items-center gap-3 border-t border-slate-100 pt-2 text-xs">
                <button type="button" className="text-brand-600 font-medium" onClick={() => edit(z)}>
                  Edit
                </button>
                {(z.source === "restricted" || z.source === "visitor_area") && (
                  <button type="button" className="text-slate-600 inline-flex items-center gap-0.5" onClick={() => setActivity(z)}>
                    Activity <ChevronRight size={12} />
                  </button>
                )}
                {z.source === "restricted" && (
                  <button type="button" className="ml-auto text-slate-400 hover:text-danger-500" title="Delete zone" onClick={() => removeRestricted(z)}>
                    <Trash2 size={13} />
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* Add: pick the purpose, then the right editor */}
      <Modal open={adding} onClose={() => setAdding(false)} title="Add a zone" width="max-w-lg">
        <div className="grid grid-cols-1 gap-2 text-sm">
          {[
            ["employee", "Employee zone", "Mark desks: records which recognised employee sits where.", () => setEditor({ kind: "desk" })],
            ["visitor", "Visitor zone", "A visitor seating area on a camera: unknown people raise visitor alerts.", () => setEditor({ kind: "visitor-pick" })],
            ["restricted", "Restricted zone", "Alert when someone enters an area they should not.", () => setEditor({ kind: "restricted" })],
            ["entry_exit", "Entry / exit · main gate", "The line employees cross at the main gate (attendance times).", () => setEditor({ kind: "gate" })],
            ["entry_exit", "Entry / exit · footfall gate", "Count people entering through a gate (footfall).", () => setEditor({ kind: "footfall", cameraId: null })],
          ].map(([type, label, help, go]) => {
            const t = TYPE[type];
            return (
              <button key={label} type="button" onClick={() => { setAdding(false); go(); }}
                className="flex items-start gap-3 rounded-xl ring-1 ring-slate-200 hover:ring-brand-500 hover:bg-brand-50/30 p-3 text-left">
                <span className={`w-8 h-8 rounded-lg grid place-items-center shrink-0 ${t.cls}`}>
                  <t.icon size={16} />
                </span>
                <span>
                  <span className="font-medium text-ink-900 block">{label}</span>
                  <span className="text-xs text-slate-500">{help}</span>
                </span>
              </button>
            );
          })}
        </div>
      </Modal>

      <Modal open={editor?.kind === "visitor-pick"} onClose={() => setEditor(null)} title="Visitor zone: choose the camera" width="max-w-md">
        <div className="space-y-1.5">
          {vcams.map((c) => (
            <button key={c.camera_id} type="button" onClick={() => setEditor({ kind: "visitor", cameraId: c.camera_id })}
              className="w-full flex items-center justify-between rounded-xl ring-1 ring-slate-200 hover:ring-brand-500 px-3 py-2 text-sm text-left">
              <span>{c.name}</span>
              <span className="text-xs text-slate-500">{c.zone ? "has a visitor area (edit)" : c.online ? "online" : "offline"}</span>
            </button>
          ))}
        </div>
      </Modal>

      {editor?.kind === "visitor" && vc && (
        <VisitorZoneEditor vc={vc} camera={liveCameraFor(vc, cameras)} onClose={done} onChanged={load} />
      )}
      <Modal open={editor?.kind === "desk"} onClose={done} title="Employee zones: mark desks on a camera" width="max-w-3xl">
        {editor?.kind === "desk" && <DeskZoneEditor cameras={cameras} onChanged={load} />}
      </Modal>
      <Modal open={editor?.kind === "gate"} onClose={done} title="Main gate entry / exit line" width="max-w-5xl">
        {editor?.kind === "gate" && <StaffEntranceEditor onSaved={load} />}
      </Modal>
      <FootfallAddCamera open={editor?.kind === "footfall"} initialCameraId={editor?.cameraId ?? null} gates={gates} onClose={done} onDone={load} />
      <RestrictedZoneEditor open={editor?.kind === "restricted"} zone={editor?.zone || null} cameras={cameras} onClose={() => setEditor(null)} onSaved={done} />

      <SidePanel open={!!activity} onClose={() => setActivity(null)} width="max-w-3xl" title={activity ? `${activity.name} · activity` : ""}>
        {activity?.source === "restricted" && <RestrictedActivity zone={activity} />}
        {activity?.source === "visitor_area" && visitorActivity}
      </SidePanel>
    </div>
  );
}

function RestrictedActivity({ zone }) {
  const [rows, setRows] = useState(null);
  useEffect(() => {
    api.zonesApi.restrictedActivity(zone.id).then((d) => setRows(d.activity)).catch(() => setRows([]));
  }, [zone.id]);
  return (
    <div className="text-sm">
      <p className="text-xs text-slate-500 mb-3">{zone.rule}</p>
      {!rows ? (
        <p className="text-slate-400">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-slate-400">Nobody has entered this zone yet.</p>
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Who</th>
              <th>Entered</th>
              <th>Left</th>
              <th>Alert</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td>{new Date(r.entered_at * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
                <td>{r.left_at ? clock(r.left_at) : <span className="badge badge-warning">inside</span>}</td>
                <td>{r.alert_id ? <span className="badge badge-danger">alert #{r.alert_id}</span> : <span className="text-slate-400">—</span>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
