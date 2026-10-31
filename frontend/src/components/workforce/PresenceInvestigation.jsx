import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeft, BadgeCheck, Camera, CircleAlert, Clock3, Expand, MapPin, MonitorPlay, RefreshCw, ScanFace, Settings2, ShieldAlert, ShieldCheck, Shrink, Star, UserCheck, X } from "lucide-react";
import * as api from "../../api/client";
import useLiveCameraFeed from "../../hooks/useLiveCameraFeed";
import { DayBadge, clock } from "./shared";

// "Verify Presence on Camera" for one employee, opened from their attendance
// record. The backend (presence.py) recommends the camera most relevant to
// their last RELIABLE detection (live face match first, then their latest face
// recognition, then a live appearance-only lead) and the other cameras that
// cover the same mapped location; this screen opens that camera's live feed
// next to the employee's details, lets the admin switch camera (or view both
// Main Hall cameras), and verify or correct attendance. Nothing here changes
// attendance by itself: verification and corrections are explicit admin
// actions with a reason, kept with their history (workforce.add_correction).

const REFRESH_MS = 5000;

const VERDICT = {
  verified_live: { cls: "bg-emerald-50 text-emerald-800 ring-emerald-200", Icon: ShieldCheck },
  verified_recent: { cls: "bg-sky-50 text-sky-800 ring-sky-200", Icon: BadgeCheck },
  seen_earlier: { cls: "bg-amber-50 text-amber-800 ring-amber-200", Icon: Clock3 },
  unverified: { cls: "bg-slate-100 text-slate-700 ring-slate-200", Icon: CircleAlert },
};
const KIND = {
  live_face: "Live · recognised by face",
  live_appearance: "Live · appearance match only (not verified)",
  face: "Recognised by face",
  gate: "Gate crossing (Staff Count)",
  footfall: "Footfall gate entry (named by face)",
};
const REC = {
  live: { label: "Live · verified by face", cls: "bg-emerald-50 text-emerald-700" },
  historical: { label: "Historical detection", cls: "bg-amber-50 text-amber-700" },
  lead: { label: "Live · appearance only, not verified", cls: "bg-orange-50 text-orange-700" },
  unavailable: { label: "Camera location unavailable", cls: "bg-slate-100 text-slate-600" },
};
const AVAIL = {
  online: { label: "Streaming", cls: "badge-success" },
  connecting: { label: "Connecting", cls: "badge-warning" },
  idle: { label: "Connects when opened", cls: "badge-neutral" },
  "no video": { label: "No video", cls: "badge-danger" },
  offline: { label: "Offline", cls: "badge-danger" },
  "feed off": { label: "Feed switched off", cls: "badge-neutral" },
};

const ago = (ts) => {
  if (!ts) return "";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min ago`;
};

export default function PresenceInvestigation({ employeeId, day, onClose, onCorrected }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [view, setView] = useState("camera");            // camera | both | evidence
  const [cameras, setCameras] = useState([]);            // live-feed camera objects (api.getCameras)
  const [camId, setCamId] = useState(null);              // the camera on screen
  const [picked, setPicked] = useState(false);           // the admin chose it (don't follow the recommendation)
  const [correcting, setCorrecting] = useState(null);
  const [checking, setChecking] = useState(false);

  const load = useCallback(() => {
    setChecking(true);
    return api.hr
      .presence(employeeId, day)
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(e.message))
      .finally(() => setChecking(false));
  }, [employeeId, day]);

  useEffect(() => {
    load();
    api.getCameras().then(setCameras).catch(() => setCameras([]));
  }, [load]);

  // Live evidence changes: look again every few seconds (today only).
  useEffect(() => {
    if (!data?.is_today) return undefined;
    const t = setInterval(load, REFRESH_MS);
    return () => clearInterval(t);
  }, [data?.is_today, load]);

  // Open the recommended camera (once; afterwards only the admin switches).
  const recId = data?.recommendation?.camera?.camera_id ?? null;
  useEffect(() => {
    if (!picked && camId === null && recId !== null) setCamId(recId);
  }, [recId, picked, camId]);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && !document.fullscreenElement && (view !== "camera" ? setView("camera") : onClose());
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [view, onClose]);

  const choose = (id) => {
    setCamId(id);
    setPicked(true);
    setView("camera");
  };

  const emp = data?.employee;
  const att = data?.attendance;
  const v = data ? VERDICT[data.verdict.state] : null;
  const dirCam = data?.camera_directory?.find((c) => c.camera_id === camId) || null;

  const verify = () => {
    const seen = data.cameras.find((c) => c.camera_id === camId);
    const now = Date.now() / 1000;
    const parts = [`Checked on the live feed of ${dirCam?.name || "a camera"} at ${clock(now)}`];
    if (seen) parts.push(`${KIND[seen.kind] || "detected"} there ${seen.live ? "now" : `at ${clock(seen.last_seen)}`}`);
    setCorrecting({ kind: "presence_verified", ts: data.is_today ? now : data.last_detection?.at, evidence: parts.join("; ") });
  };

  return (
    <div className="fixed inset-0 z-[60] bg-[#f5f6fa] overflow-y-auto" role="dialog" aria-modal="true" aria-label="Presence investigation">
      <div className="max-w-[1500px] mx-auto px-4 sm:px-6 py-4 space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <button type="button" onClick={view !== "camera" ? () => setView("camera") : onClose} className="btn-secondary text-sm inline-flex items-center gap-1.5">
            <ArrowLeft size={15} /> {view !== "camera" ? "Back to camera" : "Back to employee details"}
          </button>
          <div className="min-w-0 mr-auto">
            <p className="text-[11px] uppercase tracking-wide text-slate-400">Verify presence on camera · {day}</p>
            <h2 className="text-lg font-semibold text-ink-900 truncate">
              {emp ? emp.name : "…"} <span className="text-sm font-normal text-slate-400">ID {employeeId}{emp?.department ? ` · ${emp.department}` : ""}</span>
            </h2>
          </div>
          {data && (
            <div className="flex rounded-full bg-white shadow-[0_1px_3px_rgba(15,23,42,0.06)] p-0.5 text-xs">
              {[
                ["camera", "Camera"],
                ["evidence", "All detections"],
              ].map(([k, label]) => (
                <button key={k} type="button" onClick={() => setView(k)} className={`px-3 py-1.5 rounded-full ${view === k || (k === "camera" && view === "both") ? "bg-ink-900 text-white" : "text-slate-600"}`}>
                  {label}
                </button>
              ))}
            </div>
          )}
          <button type="button" onClick={load} className="btn-secondary text-sm inline-flex items-center gap-1.5" title="Check the cameras again">
            <RefreshCw size={14} className={checking ? "animate-spin" : ""} /> Check again
          </button>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-slate-600 p-1" aria-label="Close">
            <X size={20} />
          </button>
        </div>

        {error && <p className="text-sm text-danger-500">{error}</p>}
        {!data && !error && <p className="text-sm text-slate-400 py-16 text-center">Checking detections and choosing a camera…</p>}

        {data && view === "evidence" && <EvidenceView data={data} onChanged={() => { load(); onCorrected?.(); }} />}
        {data && view === "both" && <MainHallView data={data} cameras={cameras} onSaved={load} />}
        {data && view === "camera" && (
          <div className="grid grid-cols-1 lg:grid-cols-[minmax(300px,380px)_1fr] gap-4 items-start">
            {/* Left: the employee, the evidence behind the camera choice, the actions */}
            <div className="space-y-3 lg:sticky lg:top-4">
              <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 text-sm space-y-3">
                <div className="flex items-center gap-2">
                  <DayBadge status={att.status} />
                  {att.corrected && <span className="badge badge-neutral">{att.presence_verified ? "Presence verified" : "Corrected"}</span>}
                </div>
                <dl className="grid grid-cols-2 gap-y-1.5 text-[13px]">
                  <dt className="text-slate-500">Entry</dt>
                  <dd className="tabular-nums">{clock(att.first_in)}</dd>
                  <dt className="text-slate-500">Exit</dt>
                  <dd className="tabular-nums">{att.inside_now ? "— (inside)" : clock(att.last_out)}</dd>
                  <dt className="text-slate-500">Last detection</dt>
                  <dd>{data.last_detection ? `${clock(data.last_detection.at)} · ${data.last_detection.camera}` : "None"}</dd>
                  <dt className="text-slate-500">Last location</dt>
                  <dd className="text-xs">{att.location_detail || "Not known"}</dd>
                </dl>
                <div className={`rounded-xl ring-1 ${v.cls} px-3 py-2 flex gap-2`}>
                  <v.Icon size={18} className="shrink-0 mt-0.5" />
                  <div>
                    <p className="font-semibold text-[13px]">{data.verdict.label}</p>
                    <p className="text-xs opacity-80">{data.verdict.detail}</p>
                    {data.verdict.lead && <p className="text-xs mt-0.5">{data.verdict.lead}</p>}
                  </div>
                </div>
              </section>

              <RecommendationCard rec={data.recommendation} />

              <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 space-y-2">
                <p className="text-xs text-slate-500">After checking the live feed:</p>
                <button type="button" onClick={verify} className="btn-primary text-sm w-full inline-flex items-center justify-center gap-1.5">
                  <UserCheck size={15} /> Verify attendance
                </button>
                <button type="button" onClick={() => setCorrecting({ kind: att.first_in ? "exit" : "entry" })} className="btn-secondary text-sm w-full">
                  Correct entry / exit
                </button>
                <p className="text-[11px] text-slate-400">A camera detection never marks anyone present by itself. Your verification is saved with a reason, your name and the time.</p>
              </section>

              <CorrectionHistory corrections={data.corrections} onChanged={() => { load(); onCorrected?.(); }} />
            </div>

            {/* Right: the live feed of the chosen camera */}
            <CameraPanel data={data} cameras={cameras} camId={camId} onChoose={choose} onBoth={() => setView("both")} onSaved={load} />
          </div>
        )}
      </div>

      {correcting && data && (
        <CorrectionForm
          employee={data.employee}
          day={data.day}
          initial={correcting}
          attendance={data.attendance}
          onClose={() => setCorrecting(null)}
          onSaved={() => {
            setCorrecting(null);
            load();
            onCorrected?.();
          }}
        />
      )}
    </div>
  );
}

function RecommendationCard({ rec }) {
  const s = REC[rec.state];
  const ev = rec.evidence;
  return (
    <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 text-sm space-y-2">
      <div className="flex items-center gap-2">
        <Star size={14} className="text-brand-600" />
        <h3 className="text-[13px] font-semibold text-ink-900">Why this camera</h3>
        <span className={`ml-auto text-[11px] font-medium rounded-full px-2 py-0.5 ${s.cls}`}>{s.label}</span>
      </div>
      {rec.camera ? (
        <>
          <p className="text-ink-900">
            <span className="font-semibold">{rec.camera.name}</span>
            <span className="text-slate-500"> · {rec.camera.zone || "location not mapped"}</span>
          </p>
          <p className="text-xs text-slate-600">{rec.reason}</p>
          {ev && (
            <dl className="grid grid-cols-2 gap-y-1 text-xs">
              <dt className="text-slate-500">Detected</dt>
              <dd className="tabular-nums">{ev.live ? "now" : `${clock(ev.at)} (${ago(ev.at)})`}</dd>
              <dt className="text-slate-500">Source</dt>
              <dd>{KIND[ev.kind]}</dd>
              <dt className="text-slate-500">Identity</dt>
              <dd className={ev.verified ? "text-emerald-700" : "text-orange-700"}>
                {ev.verified ? `Verified by face${ev.confidence ? ` (${Math.round(ev.confidence * 100)}%)` : ""}` : "Not verified"}
              </dd>
              <dt className="text-slate-500">Freshness</dt>
              <dd>{ev.live ? "Live" : "Historical: they may have moved"}</dd>
            </dl>
          )}
          {rec.note && <p className="text-xs text-amber-700">{rec.note}</p>}
          {rec.gate_camera && <p className="text-xs text-amber-700">This is the gate camera: it also sees people outside.</p>}
        </>
      ) : (
        <p className="text-xs text-slate-600">{rec.reason} Choose a camera on the right to check manually.</p>
      )}
    </section>
  );
}

function CameraPanel({ data, cameras, camId, onChoose, onBoth, onSaved }) {
  const dir = data.camera_directory;
  const rec = data.recommendation;
  const cam = dir.find((c) => c.camera_id === camId) || null;
  const liveCam = cameras.find((c) => c.id === camId) || null;
  const seen = data.cameras.find((c) => c.camera_id === camId) || null;
  const quick = [rec.camera, ...(rec.alternatives || [])].filter(Boolean);
  const hall = data.main_hall;
  const [zoneEdit, setZoneEdit] = useState(null);
  const [zoneError, setZoneError] = useState("");

  async function saveZone() {
    try {
      await api.hr.setCameraLocation(camId, zoneEdit);
      setZoneEdit(null);
      setZoneError("");
      onSaved();
    } catch (e) {
      setZoneError(e.message);
    }
  }

  return (
    <div className="space-y-3 min-w-0">
      {/* Camera switcher: the recommendation, cameras covering the same location, any other camera */}
      <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-3 flex flex-wrap items-center gap-2">
        {quick.map((c, i) => (
          <button
            key={c.camera_id}
            type="button"
            onClick={() => onChoose(c.camera_id)}
            className={`rounded-xl px-3 py-1.5 text-xs text-left ring-1 transition ${c.camera_id === camId ? "ring-brand-500 bg-brand-50 text-brand-700" : "ring-slate-200 text-slate-600 hover:bg-slate-50"}`}
          >
            <span className="font-semibold">{c.name}</span>
            <span className="block text-[10px]">{i === 0 ? "Recommended" : "Same location"} · {AVAIL[c.availability]?.label || c.availability}</span>
          </button>
        ))}
        <select className="input-field !w-auto !py-1.5 text-xs" value="" onChange={(e) => e.target.value && onChoose(Number(e.target.value))}>
          <option value="">{quick.length ? "Other camera…" : "Choose a camera…"}</option>
          {dir.map((c) => (
            <option key={c.camera_id} value={c.camera_id}>
              {c.name} · {c.zone || "location not mapped"} · {AVAIL[c.availability]?.label || c.availability}
            </option>
          ))}
        </select>
        {hall.configured && (
          <button type="button" onClick={onBoth} className="ml-auto btn-secondary text-xs inline-flex items-center gap-1.5">
            <MonitorPlay size={14} /> View both Main Hall cameras
          </button>
        )}
      </div>

      {!cam ? (
        <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-6">
          <div className="flex items-center gap-2 text-ink-900 font-semibold mb-1">
            <MapPin size={16} className="text-slate-400" /> Camera location unavailable
          </div>
          <p className="text-sm text-slate-500 mb-4">No camera has identified {data.employee.name} {data.is_today ? "today" : "on this day"}, so there is no camera to recommend. Pick one to check:</p>
          <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 gap-2">
            {dir.map((c) => (
              <button key={c.camera_id} type="button" onClick={() => onChoose(c.camera_id)} className="rounded-xl ring-1 ring-slate-200 hover:ring-brand-500 hover:bg-brand-50/40 px-3 py-2.5 text-left">
                <p className="text-sm font-medium text-ink-900">{c.name}</p>
                <p className="text-[11px] text-slate-500">{c.zone || "location not mapped"}</p>
                <span className={`badge mt-1 ${AVAIL[c.availability]?.cls || "badge-neutral"}`}>{AVAIL[c.availability]?.label || c.availability}</span>
              </button>
            ))}
          </div>
        </div>
      ) : (
        <>
          <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] px-4 py-3 flex flex-wrap items-center gap-x-5 gap-y-1.5 text-sm">
            <span className="font-semibold text-ink-900 inline-flex items-center gap-1.5">
              <Camera size={15} className="text-brand-600" /> {cam.name}
            </span>
            {zoneEdit === null ? (
              <span className="text-slate-500 inline-flex items-center gap-1">
                <MapPin size={13} /> {cam.zone || "Location not mapped"}
                <button type="button" className="text-[11px] text-brand-600 ml-1" onClick={() => setZoneEdit(cam.zone || "")}>
                  {cam.zone ? "Change" : "Set location"}
                </button>
              </span>
            ) : (
              <span className="inline-flex items-center gap-1.5">
                <input className="input-field !py-1 !w-40 text-xs" value={zoneEdit} placeholder="e.g. Reception" onChange={(e) => setZoneEdit(e.target.value)} autoFocus />
                <button type="button" className="btn-primary text-xs !py-1" onClick={saveZone}>Save</button>
                <button type="button" className="text-xs text-slate-500" onClick={() => setZoneEdit(null)}>Cancel</button>
              </span>
            )}
            <span className={`badge ${AVAIL[cam.availability]?.cls || "badge-neutral"}`}>{AVAIL[cam.availability]?.label || cam.availability}</span>
            <span className="text-slate-500 text-xs ml-auto">
              {seen
                ? seen.live
                  ? seen.verified
                    ? `${data.employee.name} recognised here now`
                    : `Possible ${data.employee.name} here now (appearance only, not verified)`
                  : `${data.employee.name} last recognised here at ${clock(seen.last_seen)} (${ago(seen.last_seen)})`
                : `No detection of ${data.employee.name} on this camera ${data.is_today ? "today" : "on this day"}`}
            </span>
            {zoneError && <span className="text-xs text-danger-500 w-full">{zoneError}</span>}
          </div>
          <HallFeed
            key={camId}
            slot={camId === rec.camera?.camera_id ? "Recommended" : "Selected camera"}
            hallCamera={{ camera_id: cam.camera_id, name: cam.name }}
            camera={liveCam}
            seen={seen}
            employeeName={data.employee.name}
            large
          />
        </>
      )}
    </div>
  );
}

function EvidenceView({ data, onChanged }) {
  const att = data.attendance;
  return (
    <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 items-start">
      <div className="xl:col-span-2 space-y-4">
        <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)]">
          <div className="px-4 py-3 border-b border-slate-100 flex items-center gap-2">
            <Camera size={15} className="text-brand-600" />
            <h3 className="text-sm font-semibold text-ink-900">Cameras with detections</h3>
            <span className="text-xs text-slate-400">{data.is_today ? "live tracks re-checked every 5 s" : "recorded on this day"}</span>
          </div>
          {data.cameras.length === 0 ? (
            <p className="text-sm text-slate-500 px-4 py-8 text-center">
              No matching detection is available{data.is_today ? " right now or earlier today" : " for this day"}. Presence not verified.
            </p>
          ) : (
            <div className="overflow-x-auto">
              <table className="data-table">
                <thead>
                  <tr>
                    <th>Camera</th>
                    <th>Location</th>
                    <th>Last detected</th>
                    <th>Evidence</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {data.cameras.map((c) => (
                    <tr key={c.camera_id}>
                      <td className="font-medium text-ink-900">{c.camera}</td>
                      <td className="text-xs text-slate-500">
                        {c.site || "—"}
                        {c.purpose ? ` · ${c.purpose}` : ""}
                        {c.gate && <span className="block text-amber-600">gate camera: may see people outside</span>}
                      </td>
                      <td className="tabular-nums whitespace-nowrap">
                        {clock(c.last_seen)}
                        <span className="block text-[11px] text-slate-400">{c.live ? "now" : ago(c.last_seen)}</span>
                      </td>
                      <td className="text-xs">{KIND[c.kind]}</td>
                      <td>
                        <EvidenceBadge live={c.live} verified={c.verified} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)]">
          <div className="px-4 py-3 border-b border-slate-100 flex items-center gap-2">
            <ScanFace size={15} className="text-brand-600" />
            <h3 className="text-sm font-semibold text-ink-900">Detection timeline</h3>
          </div>
          {data.evidence.length === 0 ? (
            <p className="text-sm text-slate-400 px-4 py-6 text-center">No detections.</p>
          ) : (
            <ol className="divide-y divide-slate-100">
              {data.evidence.map((e, i) => (
                <li key={i} className="px-4 py-2.5 flex items-start gap-3 text-sm">
                  <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${e.live ? (e.verified ? "bg-emerald-500 animate-pulse" : "bg-amber-400 animate-pulse") : "bg-slate-300"}`} />
                  <div className="min-w-0 flex-1">
                    <p className="text-ink-900">
                      {e.camera} <span className="text-slate-400">·</span> <span className="text-slate-600">{e.detail}</span>
                    </p>
                    <p className="text-[11px] text-slate-400">
                      {e.from && e.from !== e.at ? `${clock(e.from)} – ` : ""}
                      {e.live ? "now" : clock(e.at)}
                      {e.confidence ? ` · confidence ${Math.round(e.confidence * 100)}%` : ""}
                      {e.sightings ? ` · ${e.sightings} sightings` : ""}
                    </p>
                  </div>
                  <EvidenceBadge live={e.live} verified={e.verified} />
                </li>
              ))}
            </ol>
          )}
        </section>
      </div>

      <div className="space-y-4">
        <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 text-sm space-y-2">
          <h3 className="text-sm font-semibold text-ink-900">Attendance record</h3>
          <dl className="grid grid-cols-2 gap-y-1.5">
            <dt className="text-slate-500">Status</dt>
            <dd>
              <DayBadge status={att.status} />
            </dd>
            <dt className="text-slate-500">Entry</dt>
            <dd className="tabular-nums">
              {clock(att.first_in)}
              {att.original_first_in !== att.first_in && att.corrected && <span className="block text-[11px] text-slate-400">recorded: {clock(att.original_first_in)}</span>}
            </dd>
            <dt className="text-slate-500">Exit</dt>
            <dd className="tabular-nums">
              {att.inside_now ? "—" : clock(att.last_out)}
              {att.original_last_out !== att.last_out && att.corrected && <span className="block text-[11px] text-slate-400">recorded: {clock(att.original_last_out)}</span>}
            </dd>
            <dt className="text-slate-500">Location</dt>
            <dd className="text-xs">{att.location_detail || "Not known"}</dd>
          </dl>
        </section>
        <CorrectionHistory corrections={data.corrections} onChanged={onChanged} />
      </div>
    </div>
  );
}

function EvidenceBadge({ live, verified }) {
  if (live && verified) return <span className="badge badge-success shrink-0">Live · verified</span>;
  if (live) return <span className="badge badge-warning shrink-0">Live · not verified</span>;
  return <span className="badge badge-neutral shrink-0">Historical</span>;
}

// ---- Main Hall split screen -----------------------------------------------

function MainHallView({ data, cameras, onSaved }) {
  const [hall, setHall] = useState(data.main_hall);
  const [editing, setEditing] = useState(!data.main_hall.configured);
  const [pick, setPick] = useState(() => (data.main_hall.cameras || []).map((c) => (c ? String(c.camera_id) : "")));
  const [error, setError] = useState("");

  async function save() {
    try {
      const h = await api.hr.setMainHall(pick.map(Number));
      setHall(h);
      setEditing(false);
      setError("");
      onSaved?.();
    } catch (e) {
      setError(e.message);
    }
  }

  const byCam = Object.fromEntries(data.cameras.map((c) => [c.camera_id, c]));
  return (
    <div className="space-y-3">
      {/* The employee being looked for stays in view */}
      <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
        <span className="font-semibold text-ink-900">Looking for {data.employee.name}</span>
        <span className="text-slate-500">
          Last known camera: <span className="text-ink-900">{data.last_detection?.camera || "none"}</span>
        </span>
        <span className="text-slate-500">
          Last detection: <span className="text-ink-900">{data.last_detection ? `${clock(data.last_detection.at)} (${data.last_detection.live ? "live" : ago(data.last_detection.at)})` : "no matching detection"}</span>
        </span>
        <span className="text-slate-500">
          Status: <span className="text-ink-900">{data.verdict.label}</span>
        </span>
        <button type="button" className="ml-auto text-xs text-brand-600 inline-flex items-center gap-1" onClick={() => setEditing((x) => !x)}>
          <Settings2 size={13} /> {editing ? "Cancel" : "Change cameras"}
        </button>
      </div>

      {editing && (
        <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 flex flex-wrap items-end gap-3 text-sm">
          {["Camera 01 (left)", "Camera 02 (right)"].map((label, i) => (
            <label key={label} className="block">
              <span className="text-xs text-slate-500">{label}</span>
              <select className="input-field mt-1 !w-56" value={pick[i] || ""} onChange={(e) => setPick((p) => p.map((x, k) => (k === i ? e.target.value : x)))}>
                <option value="">Choose a camera…</option>
                {cameras.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.label}
                    {c.site ? ` · ${c.site}` : ""}
                  </option>
                ))}
              </select>
            </label>
          ))}
          <button type="button" className="btn-primary text-sm" disabled={!pick[0] || !pick[1]} onClick={save}>
            Save Main Hall cameras
          </button>
          {error && <p className="text-xs text-danger-500 w-full">{error}</p>}
          <p className="text-[11px] text-slate-400 w-full">The two cameras that cover the main hall, where employees sit. Saved for everyone.</p>
        </div>
      )}

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {hall.cameras.map((c, i) => {
          const cam = c ? cameras.find((x) => x.id === c.camera_id) : null;
          return (
            <HallFeed
              key={`${i}-${c?.camera_id ?? "none"}`}
              slot={`Camera 0${i + 1}`}
              hallCamera={c}
              camera={cam}
              seen={c ? byCam[c.camera_id] : null}
              employeeName={data.employee.name}
            />
          );
        })}
      </div>
    </div>
  );
}

function HallFeed({ slot, hallCamera, camera, seen, employeeName, large = false }) {
  const boxRef = useRef(null);
  const [full, setFull] = useState(false);

  useEffect(() => {
    const onChange = () => setFull(document.fullscreenElement === boxRef.current);
    document.addEventListener("fullscreenchange", onChange);
    return () => document.removeEventListener("fullscreenchange", onChange);
  }, []);

  const toggleFull = () => {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else boxRef.current?.requestFullscreen?.();
  };

  return (
    <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] overflow-hidden">
      <div className="px-4 py-2.5 flex items-center gap-2 border-b border-slate-100">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{slot}</span>
        <span className="text-sm font-semibold text-ink-900 truncate">{hallCamera ? hallCamera.name : "Not chosen"}</span>
        {seen && (
          <span className={`badge ml-1 ${seen.live && seen.verified ? "badge-success" : seen.live ? "badge-warning" : "badge-neutral"}`}>
            {seen.live ? (seen.verified ? `${employeeName} recognised here now` : `Possible ${employeeName} (appearance)`) : `${employeeName} last seen here ${clock(seen.last_seen)}`}
          </span>
        )}
      </div>
      <div ref={boxRef} className={`relative bg-ink-900 ${full ? "w-screen h-screen" : large ? "aspect-video max-h-[72vh] mx-auto" : "aspect-video"}`}>
        {camera ? <FeedCanvas camera={camera} full={full} large={large} onToggleFull={toggleFull} label={`${slot} · ${hallCamera.name}`} /> : (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-white/50 text-sm text-center px-6">
            <ShieldAlert size={26} />
            {hallCamera ? "This camera is not available to stream." : "Choose this camera with “Change cameras” above."}
          </div>
        )}
      </div>
    </section>
  );
}

// One live stream (the same websocket wiring as the Live Feed). Its status is
// what the stream reports: "Live" only while frames are actually arriving.
function FeedCanvas({ camera, full, large, onToggleFull, label }) {
  const { canvasRef, status, live } = useLiveCameraFeed(camera, { overlay: true, width: full || large ? undefined : 1280 });
  const state = live ? "live" : status === "connecting" ? "connecting" : status === "live" ? "waiting" : status === "disabled" ? "off" : "offline";
  const TEXT = { live: "LIVE", connecting: "CONNECTING", waiting: "CONNECTED · NO VIDEO", off: "FEED OFF", offline: "OFFLINE" };
  return (
    <>
      <canvas ref={canvasRef} className="w-full h-full object-contain" />
      {!live && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-white/50 text-sm text-center px-6">
          <ShieldAlert size={26} />
          {state === "connecting"
            ? "Connecting…"
            : state === "waiting"
              ? camera.protocol === "rtmp_push"
                ? "Connected, but the camera is not sending video (check its RTMP push)."
                : "Connected, waiting for video…"
              : state === "off"
                ? "The live feed is switched off for this camera."
                : "This camera is offline. The other feed is unaffected."}
        </div>
      )}
      <span className="absolute top-2.5 left-2.5 flex items-center gap-1.5 text-[11px] font-semibold text-white bg-black/55 rounded-full px-2.5 py-1 tracking-wide">
        <span className={`w-1.5 h-1.5 rounded-full ${live ? "bg-danger-500 animate-pulse" : state === "connecting" ? "bg-amber-400" : "bg-slate-400"}`} />
        {TEXT[state]}
      </span>
      {full && <span className="absolute top-2.5 left-1/2 -translate-x-1/2 text-xs text-white bg-black/55 rounded-full px-3 py-1">{label}</span>}
      <button
        type="button"
        onClick={onToggleFull}
        className="absolute top-2 right-2 w-8 h-8 rounded-full grid place-items-center bg-black/55 text-white hover:bg-black/75"
        title={full ? "Exit full screen" : "Full screen"}
        aria-label={full ? "Exit full screen" : "Full screen"}
      >
        {full ? <Shrink size={15} /> : <Expand size={15} />}
      </button>
    </>
  );
}

// ---- Corrections ------------------------------------------------------------

const KINDS = [
  ["presence_verified", "Presence verified", "They were present (e.g. confirmed on camera or in person). Marks them present from this time if no entry is recorded."],
  ["entry", "Entry time", "The time they actually came in (the recorded entry is kept)."],
  ["exit", "Exit time", "The time they actually left (the recorded exit is kept)."],
];

function toInput(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function CorrectionForm({ employee, day, initial = {}, attendance, onClose, onSaved }) {
  const [kind, setKind] = useState(initial.kind || "presence_verified");
  const [time, setTime] = useState(() => toInput(initial.ts));
  const [reason, setReason] = useState("");
  const [evidence, setEvidence] = useState(initial.evidence || "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function save(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const ts = new Date(`${day}T${time}:00`).getTime() / 1000;
      await api.hr.addCorrection({ employee_id: employee.employee_id, day, kind, ts, reason, evidence: evidence || null });
      onSaved();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  const help = KINDS.find((k) => k[0] === kind)[2];
  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/40 px-4">
      <form onSubmit={save} className="w-full max-w-md bg-white rounded-2xl shadow-xl p-6 space-y-3 text-sm">
        <div className="flex items-start justify-between">
          <div>
            <h3 className="text-base font-semibold text-ink-900">Attendance correction</h3>
            <p className="text-xs text-slate-500">
              {employee.name} · ID {employee.employee_id} · {day}
            </p>
          </div>
          <button type="button" onClick={onClose} className="text-slate-400 hover:text-slate-600" aria-label="Close">
            <X size={18} />
          </button>
        </div>
        <div className="grid grid-cols-3 gap-1 rounded-xl bg-slate-50 p-1">
          {KINDS.map(([k, label]) => (
            <button key={k} type="button" onClick={() => setKind(k)} className={`rounded-lg px-2 py-1.5 text-xs font-medium ${kind === k ? "bg-white shadow text-ink-900" : "text-slate-500"}`}>
              {label}
            </button>
          ))}
        </div>
        <p className="text-[11px] text-slate-500">{help}</p>
        {attendance && (
          <p className="text-[11px] text-slate-400">
            Recorded now: entry {clock(attendance.original_first_in ?? attendance.first_in)}, exit {clock(attendance.original_last_out ?? attendance.last_out)}, status {attendance.status}.
          </p>
        )}
        <label className="block">
          <span className="text-xs text-slate-500">{kind === "presence_verified" ? "Present at" : "Time"}</span>
          <input type="time" className="input-field mt-1" value={time} onChange={(e) => setTime(e.target.value)} required />
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">Reason (required)</span>
          <input className="input-field mt-1" value={reason} onChange={(e) => setReason(e.target.value)} placeholder="e.g. Seen at desk on Main Hall camera; gate missed the entry" required minLength={3} />
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">Evidence (optional)</span>
          <input className="input-field mt-1" value={evidence} onChange={(e) => setEvidence(e.target.value)} placeholder="e.g. recognised on CAM2 at 10:42 AM" />
        </label>
        <p className="text-[11px] text-slate-400">The original record is kept. This correction is saved with your name and can be withdrawn later.</p>
        {error && <p className="text-xs text-danger-500">{error}</p>}
        <div className="flex justify-end gap-2">
          <button type="button" className="btn-secondary text-sm" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn-primary text-sm" disabled={busy || reason.trim().length < 3}>
            {busy ? "Saving…" : "Save correction"}
          </button>
        </div>
      </form>
    </div>
  );
}

export function CorrectionHistory({ corrections, onChanged }) {
  const [error, setError] = useState("");
  async function withdraw(c) {
    const reason = window.prompt("Why is this correction being withdrawn?");
    if (!reason) return;
    try {
      await api.hr.withdrawCorrection(c.id, reason);
      setError("");
      onChanged?.();
    } catch (e) {
      setError(e.message);
    }
  }
  return (
    <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 text-sm">
      <h3 className="text-sm font-semibold text-ink-900 mb-2">Corrections & audit history</h3>
      {error && <p className="text-xs text-danger-500 mb-1">{error}</p>}
      {!corrections?.length ? (
        <p className="text-xs text-slate-400">No corrections for this day. The attendance shown is as recorded.</p>
      ) : (
        <ol className="space-y-2">
          {corrections.map((c) => (
            <li key={c.id} className={`rounded-lg px-3 py-2 ${c.voided ? "bg-slate-50 text-slate-400" : "bg-brand-50/50"}`}>
              <p className={`text-xs font-medium ${c.voided ? "line-through" : "text-ink-900"}`}>
                {c.label}: {clock(c.ts)}
                {c.original_ts ? <span className="font-normal text-slate-500"> (recorded {clock(c.original_ts)})</span> : null}
              </p>
              <p className="text-[11px]">“{c.reason}”{c.evidence ? ` · ${c.evidence}` : ""}</p>
              <p className="text-[11px] text-slate-400">
                {c.created_by || "admin"} · {new Date(c.created_at * 1000).toLocaleString()}
                {c.voided ? ` · withdrawn by ${c.voided_by || "admin"}: “${c.void_reason}”` : ""}
              </p>
              {!c.voided && onChanged && (
                <button type="button" className="text-[11px] text-danger-500 mt-0.5" onClick={() => withdraw(c)}>
                  Withdraw
                </button>
              )}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
