import { Film, Clock, UserCheck } from "lucide-react";

export function fmtDateTime(ts) {
  return new Date(ts * 1000).toLocaleString([], { day: "2-digit", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
export function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function statusText(ev) {
  return ev.status === "inside" ? "Inside area" : `Left at ${fmtTime(ev.left_at)}`;
}

export function clipState(ev) {
  if (ev.clip_ready || ev.clip_status === "ready") return { label: "Clip ready", cls: "badge-success", ok: true };
  if (ev.clip_status === "pending") return { label: "Recording clip…", cls: "badge-warning", ok: false, reason: "The clip is still being recorded" };
  return { label: "No clip", cls: "badge-neutral", ok: false, reason: ev.clip_error || "No clip was recorded for this event" };
}

export default function VisitorAlerts({ events, cameras, filter, onFilter, loading, error, highlightIds, onOpen }) {
  const shown = (events || [])
    .filter((e) => !filter || e.camera_id === filter)
    .sort((a, b) => (b.alert_at || b.entered_at) - (a.alert_at || a.entered_at) || b.id - a.id);

  return (
    <div className="card p-4 flex flex-col min-h-0">
      <div className="flex items-center justify-between gap-2 mb-3 flex-wrap">
        <h3 className="text-sm font-semibold text-ink-900">Visitor alerts</h3>
        <select className="input-field !w-auto text-xs" value={filter || ""} onChange={(e) => onFilter(e.target.value ? Number(e.target.value) : 0)}>
          <option value="">All cameras</option>
          {cameras.map((c) => (
            <option key={c.camera_id} value={c.camera_id}>
              {c.name}
            </option>
          ))}
        </select>
      </div>

      {error && <p className="text-xs text-danger-600 mb-2">Could not refresh alerts: {error}</p>}

      <div className="space-y-2 overflow-y-auto max-h-[70vh] pr-1">
        {loading && !events && <p className="text-sm text-slate-400 py-6 text-center">Loading visitor alerts…</p>}
        {events && shown.length === 0 && !error && (
          <p className="text-sm text-slate-400 py-6 text-center">No visitors yet — mark an area on a camera</p>
        )}
        {shown.map((ev) => {
          const cs = clipState(ev);
          return (
            <div
              key={ev.id}
              onClick={() => onOpen(ev.id)}
              className={`rounded-xl border p-3 cursor-pointer transition-colors hover:bg-slate-50 ${
                highlightIds.has(ev.id) ? "border-brand-500 bg-brand-50 ring-2 ring-brand-500/30" : "border-slate-200"
              }`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <p className="text-sm font-medium text-ink-900 truncate">{ev.camera_name}</p>
                  <p className="text-xs text-slate-500">
                    Visitor event #{ev.id}
                    {ev.alert_seq > 1 && ` · repeat alert ${ev.alert_seq}, seated since ${fmtTime(ev.entered_at)}`}
                  </p>
                </div>
                <span className={`badge ${cs.cls} shrink-0`}>{cs.label}</span>
              </div>
              <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-slate-500">
                <span className="flex items-center gap-1">
                  <Clock size={12} /> {fmtDateTime(ev.alert_at || ev.entered_at)}
                </span>
                <span className="flex items-center gap-1">
                  <UserCheck size={12} /> {statusText(ev)}
                </span>
              </div>
              <div className="mt-2">
                <button
                  type="button"
                  className="btn-secondary !py-1 !px-2.5 text-xs inline-flex items-center gap-1"
                  disabled={!cs.ok}
                  title={cs.ok ? "View clip" : cs.reason}
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpen(ev.id);
                  }}
                >
                  <Film size={12} /> View clip
                </button>
                {!cs.ok && <span className="ml-2 text-[11px] text-slate-400">{cs.reason}</span>}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
