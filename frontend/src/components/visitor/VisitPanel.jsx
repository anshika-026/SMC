import { useCallback, useEffect, useState } from "react";
import { Camera, Check, CircleSlash, History, LogIn, LogOut, Pencil, Trash2, Video } from "lucide-react";
import SidePanel from "../SidePanel";
import * as api from "../../api/client";
import { clock, dateTime, dur, fromLocalInput, MediaImage, MediaVideo, niceDate, SOURCE_LABEL, StatusPill, toLocalInput } from "./shared";

// One visit: its times (with who/what recorded them), the actions that are
// due next, camera detections the operator can confirm as this visitor, the
// consented snapshot / clips, and the full audit trail of the visit.

const STEPS = [
  { kind: "entry", label: "Entry", action: "Check in now", icon: LogIn },
  { kind: "host_notified", label: "Host notified", action: "Host notified" },
  { kind: "meeting_start", label: "Meeting start", action: "Meeting started" },
  { kind: "exit", label: "Exit", action: "Check out now", icon: LogOut },
];
const SNAPSHOT_NOTE = {
  declined: "Visitor declined a snapshot. None will be taken.",
  none: "No snapshot yet.",
  no_clear_face: "No clear face was visible in the frames checked, so nothing was saved.",
  unavailable: "The face detector is not available on this server, so no snapshot could be taken.",
  expired: "The snapshot was deleted after its retention period.",
  capturing: "Checking the frames for a clear face… this can take up to a minute.",
};

export default function VisitPanel({ visitId, onClose, onChanged, onOpenProfile }) {
  const [v, setV] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");
  const [cands, setCands] = useState(null);
  const [fix, setFix] = useState(null);           // {kind, value, note}
  const [snapCam, setSnapCam] = useState("");
  const [cameras, setCameras] = useState([]);

  const load = useCallback(() => {
    if (!visitId) return;
    api.vr.visit(visitId).then((d) => { setV(d); setError(""); }).catch((e) => setError(e.message));
    api.vr.candidates(visitId).then(setCands).catch(() => setCands(null));
  }, [visitId]);

  useEffect(() => {
    setV(null);
    setFix(null);
    load();
  }, [load]);

  useEffect(() => {
    api.vr.cameras().then((d) => setCameras(d.cameras)).catch(() => {});
  }, []);

  // While a snapshot is being checked in the background, look again shortly.
  const capturing = v?.snapshot_status === "capturing";
  useEffect(() => {
    if (!capturing) return undefined;
    const t = setTimeout(load, 3000);
    return () => clearTimeout(t);
  }, [capturing, v, load]);

  async function run(label, fn) {
    setBusy(label);
    setError("");
    try {
      const r = await fn();
      load();
      onChanged?.();
      return r;
    } catch (e) {
      setError(e.message);
      return null;
    } finally {
      setBusy("");
    }
  }

  if (!visitId) return null;
  const open = !!visitId;
  const times = v ? { entry: v.entry_at, host_notified: v.host_notified_at, meeting_start: v.meeting_start_at, exit: v.exit_at } : {};
  // The next time to record: entry first, then host notified / meeting start / exit (nothing once out).
  const nextStep = v && (v.status === "pending" || v.status === "in") ? STEPS.find((s) => !times[s.kind] && (s.kind === "entry" || times.entry)) : null;
  const snapshot = v?.media?.find((m) => m.kind === "snapshot");
  const clips = v?.media?.filter((m) => m.kind === "clip") || [];
  const sourceOf = (kind) => {
    const e = v?.events?.filter((x) => x.kind === kind && !x.voided).slice(-1)[0];
    if (!e || e.ts == null) return null;
    return `${SOURCE_LABEL[e.source] || e.source}${e.camera_name ? ` · ${e.camera_name}` : ""}`;
  };

  return (
    <SidePanel open={open} onClose={onClose} width="max-w-xl" title={v ? `${v.visitor_name} · ${v.visit_code}` : "Visit"}>
      {!v && !error && <p className="text-sm text-slate-400 py-10 text-center">Loading…</p>}
      {error && <p className="text-xs text-danger-500 mb-3">{error}</p>}
      {v && (
        <div className="space-y-5 text-sm">
          <div className="flex gap-4">
            <MediaImage mediaId={snapshot?.id} className="w-24 h-28 rounded-xl shrink-0" />
            <div className="min-w-0 flex-1 space-y-1">
              <div className="flex items-center gap-2 flex-wrap">
                <StatusPill status={v.status} />
                <span className={`text-[11px] px-2 py-0.5 rounded-full ${v.first_visit ? "bg-brand-50 text-brand-700" : "bg-violet-50 text-violet-700"}`}>
                  {v.first_visit ? "First visit" : `Repeat · visit ${v.prior_visits + 1}`}
                </span>
              </div>
              <button type="button" className="text-left font-semibold text-ink-900 hover:text-brand-600" onClick={() => onOpenProfile(v.visitor_id)}>
                {v.visitor_name} <span className="text-xs font-normal text-slate-400">{v.visitor_code}</span>
              </button>
              <p className="text-xs text-slate-500">{v.visitor_company || "No company given"}</p>
              <p className="text-xs text-slate-500">{niceDate(v.visit_date)} · {v.purpose}</p>
              <p className="text-xs text-slate-500">To meet <span className="text-ink-900 font-medium">{v.host_name}</span>{v.host_department ? ` · ${v.host_department}` : ""}</p>
            </div>
          </div>

          <div className="grid grid-cols-3 gap-2">
            <Metric label="Waiting time" value={dur(v.waiting_seconds, "Not recorded")} hint="meeting start − entry" />
            <Metric label={v.status === "in" ? "Inside for" : "Visit duration"} value={v.status === "in" ? dur(v.elapsed_seconds) : dur(v.duration_seconds, "Not recorded")} hint="exit − entry" />
            <Metric label="Registered" value={clock(v.registered_at)} hint={niceDate(v.visit_date)} />
          </div>

          <div className="rounded-xl ring-1 ring-slate-100">
            {STEPS.map((s) => (
              <div key={s.kind} className="flex items-center gap-3 px-3 py-2.5 border-b border-slate-100 last:border-0">
                <span className={`w-2 h-2 rounded-full ${times[s.kind] ? "bg-brand-500" : "bg-slate-200"}`} />
                <div className="flex-1 min-w-0">
                  <p className="text-xs text-slate-500">{s.label}</p>
                  <p className="text-ink-900 font-medium">{times[s.kind] ? dateTime(times[s.kind]) : <span className="text-slate-400 font-normal">Not recorded</span>}</p>
                  {sourceOf(s.kind) && <p className="text-[11px] text-slate-400">{sourceOf(s.kind)}</p>}
                </div>
                {v.status !== "cancelled" && (
                  <button type="button" title="Correct this time" className="text-slate-400 hover:text-brand-600 p-1"
                    onClick={() => setFix({ kind: s.kind, value: toLocalInput(times[s.kind]), note: "", clear: false })}>
                    <Pencil size={14} />
                  </button>
                )}
              </div>
            ))}
          </div>

          {fix && (
            <div className="rounded-xl bg-slate-50 p-3 space-y-2">
              <p className="text-xs font-semibold text-ink-900">Correct {STEPS.find((s) => s.kind === fix.kind).label.toLowerCase()} time</p>
              <div className="flex gap-2 flex-wrap items-center">
                <input type="datetime-local" className="input-field w-auto" value={fix.value} disabled={fix.clear}
                  onChange={(e) => setFix({ ...fix, value: e.target.value })} />
                {times[fix.kind] && (
                  <label className="text-xs flex items-center gap-1.5">
                    <input type="checkbox" checked={fix.clear} onChange={(e) => setFix({ ...fix, clear: e.target.checked })} /> Remove this time
                  </label>
                )}
              </div>
              <input className="input-field" placeholder="Reason (required), e.g. camera missed the exit" value={fix.note}
                onChange={(e) => setFix({ ...fix, note: e.target.value })} />
              <div className="flex justify-end gap-2">
                <button type="button" className="btn-secondary text-xs" onClick={() => setFix(null)}>Cancel</button>
                <button type="button" className="btn-primary text-xs" disabled={!fix.note.trim() || !!busy}
                  onClick={async () => {
                    const ok = await run("fix", () => api.vr.correct(v.id, fix.kind, fix.clear ? null : fromLocalInput(fix.value), fix.note));
                    if (ok) setFix(null);
                  }}>
                  Save correction
                </button>
              </div>
            </div>
          )}

          {nextStep && !fix && (
            <div className="flex flex-wrap gap-2">
              <button type="button" className="btn-primary text-sm inline-flex items-center gap-1.5" disabled={!!busy}
                onClick={() => run("step", () => api.vr.recordTime(v.id, nextStep.kind))}>
                {nextStep.icon ? <nextStep.icon size={14} /> : <Check size={14} />} {nextStep.action}
              </button>
              {v.status === "in" && nextStep.kind !== "exit" && (
                <button type="button" className="btn-secondary text-sm inline-flex items-center gap-1.5" disabled={!!busy}
                  onClick={() => run("exit", () => api.vr.recordTime(v.id, "exit"))}>
                  <LogOut size={14} /> Check out now
                </button>
              )}
              {v.status === "pending" && (
                <button type="button" className="btn-secondary text-sm inline-flex items-center gap-1.5" disabled={!!busy}
                  onClick={() => window.confirm("Cancel this visit?") && run("cancel", () => api.vr.cancel(v.id))}>
                  <CircleSlash size={14} /> Cancel visit
                </button>
              )}
            </div>
          )}

          {cands?.kind && (
            <div>
              <p className="text-xs font-semibold text-ink-900 mb-1">Camera {cands.kind === "entry" ? "entries" : "exits"} that may be this visitor</p>
              <p className="text-[11px] text-slate-500 mb-2">
                A camera sees a person, not who they are. Confirm only when you know it is {v.visitor_name}; the time and camera are then recorded on this visit.
              </p>
              {cands.detections.length === 0 ? (
                <p className="text-xs text-slate-400 bg-slate-50 rounded-lg px-3 py-2">
                  No unlinked camera {cands.kind === "entry" ? "entries" : "exits"} for this day.
                  {cands.kind === "exit" && " Exit detection needs an entry line on the gate camera (Footfall), or a visitor seating area."}
                </p>
              ) : (
                <ul className="space-y-1.5 max-h-52 overflow-y-auto">
                  {cands.detections.map((d) => (
                    <li key={d.ref} className="flex items-center gap-2 rounded-lg ring-1 ring-slate-100 px-3 py-2">
                      <Camera size={14} className="text-slate-400 shrink-0" />
                      <div className="min-w-0 flex-1">
                        <p className="text-xs font-medium text-ink-900">{clock(d.ts)} · {d.camera_name}{d.camera_id === v.gate_camera_id ? " (visit gate)" : ""}</p>
                        <p className="text-[11px] text-slate-500">{d.source}{d.clip ? " · clip available" : ""}</p>
                      </div>
                      <button type="button" className="text-xs font-medium text-brand-600 shrink-0" disabled={!!busy}
                        onClick={() => run("link", () => api.vr.link(v.id, d.ref))}>
                        Confirm
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <div>
            <p className="text-xs font-semibold text-ink-900 mb-2">Snapshot & clips</p>
            {snapshot ? (
              <div className="flex items-start gap-3">
                <MediaImage mediaId={snapshot.id} className="w-32 h-40 rounded-xl" />
                <div className="text-[11px] text-slate-500 space-y-1">
                  <p>Taken {dateTime(snapshot.captured_at)}{snapshot.camera_name ? ` · ${snapshot.camera_name}` : ""}</p>
                  <p>{snapshot.source === "live_capture" ? "Live capture" : "From the entry clip"} · best of the frames checked</p>
                  <p>Deleted automatically {niceDate(new Date(snapshot.expires_at * 1000).toISOString().slice(0, 10))}</p>
                  <button type="button" className="text-danger-500 inline-flex items-center gap-1" onClick={() => window.confirm("Delete this snapshot?") && run("delsnap", () => api.vr.deleteMedia(snapshot.id))}>
                    <Trash2 size={12} /> Delete snapshot
                  </button>
                </div>
              </div>
            ) : (
              <p className="text-xs text-slate-500">{SNAPSHOT_NOTE[v.snapshot_status] || SNAPSHOT_NOTE.none}</p>
            )}
            {snapshot && capturing && <p className="text-[11px] text-slate-500 mt-1">{SNAPSHOT_NOTE.capturing}</p>}
            {!capturing && v.snapshot_message && <p className="text-[11px] text-slate-500 mt-1">Last attempt: {v.snapshot_message}</p>}
            {v.snapshot_consent && v.status !== "cancelled" && (
              <div className="flex gap-2 items-center mt-2 flex-wrap">
                <select className="input-field w-auto text-xs" value={snapCam} onChange={(e) => setSnapCam(e.target.value)}>
                  <option value="">{v.gate_camera_name ? `${v.gate_camera_name} (visit gate)` : "Choose camera…"}</option>
                  {cameras.map((c) => <option key={c.camera_id} value={c.camera_id}>{c.name}</option>)}
                </select>
                <button type="button" className="btn-secondary text-xs inline-flex items-center gap-1.5" disabled={!!busy || capturing || (!snapCam && !v.gate_camera_id)}
                  onClick={() => run("snap", () => api.vr.snapshot(v.id, snapCam ? Number(snapCam) : null))}>
                  <Camera size={13} /> {busy === "snap" ? "Taking frames…" : capturing ? "Checking…" : snapshot ? "Try for a clearer snapshot" : "Take snapshot now"}
                </button>
              </div>
            )}
            {clips.map((c) => (
              <div key={c.id} className="mt-3">
                <p className="text-[11px] text-slate-500 mb-1 flex items-center gap-1"><Video size={12} /> {c.moment === "exit" ? "Exit" : "Entry"} clip · {dateTime(c.captured_at)} · {c.camera_name}</p>
                <MediaVideo mediaId={c.id} className="w-full rounded-lg max-h-56" />
              </div>
            ))}
          </div>

          {v.notes && (
            <div>
              <p className="text-xs font-semibold text-ink-900 mb-1">Notes</p>
              <p className="text-xs text-slate-600 whitespace-pre-wrap">{v.notes}</p>
            </div>
          )}

          <details className="group">
            <summary className="text-xs font-semibold text-ink-900 cursor-pointer flex items-center gap-1.5"><History size={13} /> Record history ({v.events.length})</summary>
            <ul className="mt-2 space-y-1">
              {v.events.map((e) => (
                <li key={e.id} className={`text-[11px] ${e.voided ? "text-slate-400 line-through" : "text-slate-600"}`}>
                  {dateTime(e.created_at)} · {e.kind.replace("_", " ")} {e.ts ? `→ ${dateTime(e.ts)}` : e.kind !== "registered" ? "→ removed" : ""} · {SOURCE_LABEL[e.source] || e.source}
                  {e.camera_name ? ` · ${e.camera_name}` : ""}{e.actor ? ` · ${e.actor}` : ""}{e.note ? ` · "${e.note}"` : ""}
                </li>
              ))}
            </ul>
          </details>

          <div className="pt-2 border-t border-slate-100 flex justify-end">
            <button type="button" className="text-xs text-danger-500 inline-flex items-center gap-1"
              onClick={async () => {
                if (!window.confirm("Delete this visit and its snapshot/clips permanently?")) return;
                const ok = await run("del", () => api.vr.deleteVisit(v.id));
                if (ok) onClose();
              }}>
              <Trash2 size={12} /> Delete visit
            </button>
          </div>
        </div>
      )}
    </SidePanel>
  );
}

function Metric({ label, value, hint }) {
  return (
    <div className="rounded-xl bg-slate-50 px-3 py-2">
      <p className="text-[11px] text-slate-500">{label}</p>
      <p className="text-base font-semibold text-ink-900 leading-tight mt-0.5">{value}</p>
      <p className="text-[10px] text-slate-400">{hint}</p>
    </div>
  );
}
