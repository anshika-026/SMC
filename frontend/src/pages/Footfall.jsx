import { useEffect, useState } from "react";
import { AreaChart, Area, ResponsiveContainer, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import { DoorOpen, Download, Film, Footprints, Pencil, Plus, Trash2 } from "lucide-react";
import PageHeader from "../components/PageHeader";
import KpiPanel from "../components/KpiPanel";
import DataTable from "../components/DataTable";
import Modal from "../components/Modal";
import FootfallAddCamera from "../components/FootfallAddCamera";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";

// Real unique footfall across every entry gate (backend/app/footfall.py):
// all gates share one identity gallery, so a person who enters at one gate
// and leaves or re-enters through another is still counted once.
// embedded: shown inside Visitor Management's Footfall view, which owns the
// page title and camera management (add / edit / stop counting), so those
// controls are left out here.

const REFRESH_MS = 30_000;

function hourLabel(h) {
  if (h === null || h === undefined) return "—";
  const suffix = h < 12 ? "am" : "pm";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}${suffix}`;
}

function timeLabel(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

export default function Footfall({ embedded = false, reloadKey = 0 }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [showYesterday, setShowYesterday] = useState(true);
  const [addOpen, setAddOpen] = useState(false);
  const [editCameraId, setEditCameraId] = useState(null);
  const [removing, setRemoving] = useState(null);
  const [actionError, setActionError] = useState("");
  const [picture, setPicture] = useState(null); // {person, url|null, error}
  const [reloadTick, setReloadTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .getFootfallSummary()
        .then((d) => {
          if (!cancelled) {
            setData(d);
            setError("");
          }
        })
        .catch(() => !cancelled && setError("Couldn't load footfall — is the backend running?"));
    load();
    const id = setVisibleInterval(load, REFRESH_MS);
    return () => {
      cancelled = true;
      clearVisibleInterval(id);
    };
  }, [reloadTick, reloadKey]);

  // Free the picture's object URL when it is closed or replaced.
  useEffect(() => {
    const url = picture?.url;
    return () => {
      if (url) URL.revokeObjectURL(url);
    };
  }, [picture?.url]);

  async function removeCamera(g) {
    setActionError("");
    try {
      await api.removeFootfallCamera(g.camera_id);
      setRemoving(null);
      setReloadTick((n) => n + 1);
    } catch (e) {
      setActionError(e.message);
    }
  }

  // A counted person's evidence: their ~10 s clip. Rows recorded before clips
  // existed fall back to the picture.
  async function openPicture(row) {
    const base = { row, person: row.name || row.person, url: null, video: false, error: "" };
    if (row.clip_id) {
      if (row.clip_status === "expired") {
        setPicture({ ...base, error: "This clip was deleted automatically (clips are kept for 24 hours)." });
        return;
      }
      if (row.clip_status === "failed") {
        setPicture({ ...base, error: row.clip_error || "No clip was recorded for this person." });
        return;
      }
      setPicture({ ...base, video: true });
      try {
        const url = await api.fetchFootfallClipObjectUrl(row.clip_id);
        setPicture({ ...base, video: true, url });
      } catch (e) {
        setPicture({ ...base, video: true, error: e.status === 409 ? "The clip is still being recorded - open it again in a few seconds." : e.message });
      }
      return;
    }
    if (!row.snapshot_id) return;
    setPicture(base);
    try {
      const url = await api.fetchFootfallSnapshotObjectUrl(row.snapshot_id);
      setPicture({ ...base, url });
    } catch {
      setPicture({ ...base, error: "This picture is no longer available (pictures are deleted after 24 hours)." });
    }
  }

  if (error && !data) return <p className="text-sm text-danger-500">{error}</p>;
  if (!data) return <p className="text-sm text-slate-400">Loading footfall…</p>;

  const hourly = data.hourly.map((r) => ({ ...r, hour: hourLabel(r.hour) }));
  const maxGate = Math.max(1, ...data.gates.map((g) => g.unique_today));

  return (
    <div className="space-y-5">
      {!embedded && (
        <PageHeader
          title="Footfall"
          action={
            <button onClick={() => setAddOpen(true)} className="btn-primary flex items-center gap-1.5 text-sm">
              <Plus size={15} /> Add camera
            </button>
          }
        />
      )}

      {!data.model_ready && (
        <p className="card px-4 py-3 text-sm text-danger-500">
          The Re-ID model isn't installed, so counts will run high. Run{" "}
          <code>python -m scripts.fetch_reid_model</code> in <code>backend/</code> and restart the backend.
        </p>
      )}

      <KpiPanel
        icon={Footprints}
        title="Footfall"
        items={[
          {
            label: "Verified entries today",
            value: String(data.unique_today),
            sub: `each person once, across ${data.gates.length} gate${data.gates.length === 1 ? "" : "s"}`,
          },
          {
            label: "Peak entry hour",
            value: data.busiest_hour == null ? "—" : `${hourLabel(data.busiest_hour)}–${hourLabel((data.busiest_hour + 1) % 24)}`,
            sub: "most first arrivals",
          },
          { label: "Employee entries", value: String(data.employee_entries ?? 0), sub: "recognised by face" },
          { label: "Visitor entries", value: String(data.visitor_entries ?? data.unique_today), sub: `${data.returning_today} returning · ${data.new_today} first-time` },
        ]}
        note={data.gates.some((g) => !g.entry_line) ? "Gates without an entry line count everyone seen in their area - draw the line to count only inward entries." : undefined}
      />

      <div className="grid lg:grid-cols-2 gap-5">
        <div className="card p-5">
          <div className="flex items-center justify-between mb-4">
            <h3 className="font-semibold text-ink-900">Arrivals by hour</h3>
            <label className="flex items-center gap-2 text-xs text-slate-500 cursor-pointer select-none">
              <span
                onClick={() => setShowYesterday((v) => !v)}
                className={`inline-block w-9 h-5 rounded-full transition-colors ${
                  showYesterday ? "bg-brand-500" : "bg-slate-300"
                } relative`}
              >
                <span
                  className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all ${
                    showYesterday ? "left-4.5" : "left-0.5"
                  }`}
                />
              </span>
              Yesterday
            </label>
          </div>
          <div className="h-56">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={hourly}>
                <defs>
                  <linearGradient id="todayFill" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="var(--color-brand-500)" stopOpacity={0.25} />
                    <stop offset="100%" stopColor="var(--color-brand-500)" stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid vertical={false} stroke="#eceef4" />
                <XAxis dataKey="hour" tick={{ fontSize: 12, fill: "#94a3b8" }} axisLine={false} tickLine={false} />
                <YAxis hide allowDecimals={false} />
                <Tooltip />
                <Area
                  type="monotone"
                  dataKey="today"
                  name="Today"
                  stroke="var(--color-brand-500)"
                  strokeWidth={2}
                  fill="url(#todayFill)"
                />
                {showYesterday && (
                  <Area
                    type="monotone"
                    dataKey="yesterday"
                    name="Yesterday"
                    stroke="var(--color-muted-400)"
                    strokeWidth={2}
                    strokeDasharray="4 3"
                    fill="transparent"
                  />
                )}
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="card p-5">
          <h3 className="font-semibold text-ink-900 mb-1">By gate</h3>
          <p className="text-xs text-slate-400 mb-4">
            A person seen at two gates shows under both, so these add up to more than the unique total.
          </p>
          {data.gates.length === 0 ? (
            <p className="text-sm text-slate-500">
              No camera is counting yet. Click <b>Add camera</b>, choose a camera and mark the area to count in.
            </p>
          ) : (
            <div className="space-y-3">
              {data.gates.map((g) => (
                <div key={g.camera_id} className="text-sm">
                  <div className="flex items-center gap-2 mb-1">
                    <DoorOpen size={14} className="text-slate-400" />
                    <span className="text-ink-900 flex-1">{g.name}</span>
                    {!g.counting && <span className="badge badge-warning">not counting</span>}
                    {!embedded && !g.entry_line && (
                      <button type="button" onClick={() => setEditCameraId(g.camera_id)} className="badge badge-warning" title="Without an entry line everyone in the area is counted, including people outside the gate">
                        draw entry line
                      </button>
                    )}
                    {!embedded && (
                      <>
                        <button
                          type="button"
                          title="Edit gate (entry line and area)"
                          onClick={() => setEditCameraId(g.camera_id)}
                          className="text-slate-400 hover:text-ink-900"
                        >
                          <Pencil size={13} />
                        </button>
                        <button type="button" title="Stop counting at this camera" onClick={() => setRemoving(g)} className="text-slate-400 hover:text-danger-500">
                          <Trash2 size={13} />
                        </button>
                      </>
                    )}
                    <span className="font-semibold text-ink-900 w-10 text-right">{g.unique_today}</span>
                  </div>
                  <div className="h-2 rounded-full bg-[#f1f2f7] overflow-hidden">
                    <div
                      className="h-full rounded-full bg-brand-500"
                      style={{ width: `${(g.unique_today / maxGate) * 100}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <DataTable
        columns={[
          {
            key: "name",
            label: "Person",
            render: (r) => (
              <span>
                <span className={r.employee_id ? "font-medium text-ink-900" : "text-slate-600"}>{r.name || "Person"}</span>
                <span className="block text-[11px] text-slate-400">{r.person}</span>
              </span>
            ),
          },
          { key: "first_seen", label: "First seen", render: (r) => timeLabel(r.first_seen) },
          { key: "last_seen", label: "Last seen", render: (r) => timeLabel(r.last_seen) },
          { key: "gates", label: "Gates", render: (r) => r.gates.join(", ") },
          {
            key: "clip",
            label: "Clip",
            render: (r) =>
              !r.clip_id ? (
                <span className="text-xs text-slate-400">{r.snapshot_id ? "Picture" : "-"}</span>
              ) : (
                <span className={`badge ${r.clip_status === "ready" ? "badge-success" : r.clip_status === "pending" ? "badge-warning" : "badge-neutral"}`}>
                  <Film size={11} />
                  {{ ready: "Play", pending: "Recording…", failed: "No clip", expired: "Deleted" }[r.clip_status] || r.clip_status}
                </span>
              ),
          },
        ]}
        rows={data.visitors}
        emptyLabel="Nobody counted yet today"
        onRowClick={openPicture}
      />
      <p className="text-xs text-slate-400">
        Registered employees are named when face recognition confirms them; everyone else is shown as "Person". Click a person to
        play the ~10 second clip of when they were first counted today. Clips are deleted automatically after 24 hours; the person
        stays in the count.
      </p>
      {actionError && <p className="text-sm text-danger-500">{actionError}</p>}

      <FootfallAddCamera
        open={addOpen || editCameraId !== null}
        initialCameraId={editCameraId}
        gates={data.gates}
        onClose={() => {
          setAddOpen(false);
          setEditCameraId(null);
        }}
        onDone={() => setReloadTick((n) => n + 1)}
      />

      <Modal open={!!removing} onClose={() => setRemoving(null)} title="Stop counting at this camera?">
        <p className="text-sm text-slate-600 mb-4">
          {removing?.name} will no longer count footfall and its counting area is cleared. People already counted today stay in the numbers.
        </p>
        <div className="flex justify-end gap-2">
          <button onClick={() => setRemoving(null)} className="btn-secondary text-sm">
            Cancel
          </button>
          <button onClick={() => removeCamera(removing)} className="btn-primary text-sm">
            Stop counting
          </button>
        </div>
      </Modal>

      <Modal
        open={!!picture}
        onClose={() => setPicture(null)}
        title={picture ? `${picture.person} — ${picture.video || picture.row?.clip_id ? "clip" : "picture"}` : ""}
        width={picture?.video ? "max-w-2xl" : "max-w-lg"}
      >
        {picture?.row?.clip_id && (
          <p className="text-xs text-slate-500 mb-2">
            {picture.row.clip_camera} · counted at {picture.row.clip_at ? new Date(picture.row.clip_at * 1000).toLocaleString() : "-"}
            {picture.row.employee_id ? ` · employee ${picture.row.employee_id}` : ""}
          </p>
        )}
        {picture?.url && picture.video ? (
          <>
            <video src={picture.url} controls autoPlay playsInline className="w-full max-h-[70vh] rounded-lg bg-black" />
            <a
              href={picture.url}
              download={`footfall-${(picture.person || "person").replace(/[^a-z0-9]+/gi, "-").toLowerCase()}-${picture.row.clip_id}.mp4`}
              className="btn-secondary text-sm inline-flex items-center gap-1.5 mt-3"
            >
              <Download size={14} /> Download clip
            </a>
          </>
        ) : picture?.url ? (
          <img src={picture.url} alt={`Picture of ${picture.person}`} className="w-full max-h-[70vh] object-contain rounded-lg bg-slate-100" />
        ) : (
          <p className="text-sm text-slate-500">{picture?.error || (picture?.video ? "Loading clip…" : "Loading picture…")}</p>
        )}
        <p className="text-xs text-slate-400 mt-3">
          {picture?.video ? "About 5 seconds before and 5 seconds after they were counted. " : ""}Deleted automatically after 24 hours.
        </p>
      </Modal>
    </div>
  );
}
