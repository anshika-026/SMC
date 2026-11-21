import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Camera, DoorOpen, Footprints, Pencil, Plus, Settings2, Trash2 } from "lucide-react";
import Modal from "../Modal";
import LiveCameraTile from "../LiveCameraTile";
import CameraViewerModal from "../CameraViewerModal";
import FootfallAddCamera from "../FootfallAddCamera";
import * as api from "../../api/client";
import { shiftIso, todayIso } from "../workforce/shared";

// Footfall inside Visitor Management (it no longer has its own sidebar
// entry). Everything is the existing footfall backend (footfall.py): today's
// counts, people and clips (the Footfall screen, embedded), plus reports over
// any date range (daily / weekly / monthly, entries and exits, kept across
// count resets), arrivals per hour for any day, and the counting cameras:
// live previews, status, counting area / entry line, add and remove.

const RANGES = [
  ["7", "Last 7 days", () => shiftIso(todayIso(), -6)],
  ["30", "Last 30 days", () => shiftIso(todayIso(), -29)],
  ["month", "This month", () => `${todayIso().slice(0, 7)}-01`],
  ["90", "Last 90 days", () => shiftIso(todayIso(), -89)],
  ["365", "Last 12 months", () => shiftIso(todayIso(), -364)],
];

function hourLabel(h) {
  if (h === null || h === undefined) return "—";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}${h < 12 ? "am" : "pm"}`;
}

function group(days, period) {
  if (period === "daily") return days.map((d) => ({ label: new Date(`${d.day}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short" }), ...d }));
  const out = new Map();
  for (const d of days) {
    const dt = new Date(`${d.day}T12:00:00`);
    let key;
    let label;
    if (period === "weekly") {
      const monday = new Date(dt);
      monday.setDate(dt.getDate() - ((dt.getDay() + 6) % 7));
      key = monday.toISOString().slice(0, 10);
      label = `w/c ${monday.toLocaleDateString([], { day: "numeric", month: "short" })}`;
    } else {
      key = d.day.slice(0, 7);
      label = dt.toLocaleDateString([], { month: "short", year: "2-digit" });
    }
    const g = out.get(key) || { label, entries: 0, exits: 0, employee_entries: 0, visitor_entries: 0 };
    g.entries += d.entries;
    g.exits += d.exits;
    g.visitor_entries += d.entries - d.employee_entries;
    g.employee_entries += d.employee_entries;
    out.set(key, g);
  }
  return [...out.values()];
}

export default function FootfallView() {
  const [feature, setFeature] = useState(null);       // the footfall on/off switch (was in the sidebar)
  const [range, setRange] = useState("30");
  const [from, setFrom] = useState(RANGES[1][2]());
  const [to, setTo] = useState(todayIso());
  const [period, setPeriod] = useState("daily");
  const [history, setHistory] = useState(null);
  const [day, setDay] = useState(todayIso());
  const [dayStats, setDayStats] = useState(null);
  const [summary, setSummary] = useState(null);
  const [cameras, setCameras] = useState([]);
  const [statuses, setStatuses] = useState({});
  const [editId, setEditId] = useState(undefined);    // undefined = closed, null = add, id = edit
  const [removing, setRemoving] = useState(null);
  const [viewing, setViewing] = useState(null);
  const [error, setError] = useState("");
  const [tick, setTick] = useState(0);

  const loadCameras = useCallback(() => {
    api.getFootfallSummary().then(setSummary).catch((e) => setError(e.message));
    api.getCameras().then(setCameras).catch(() => setCameras([]));
    api.getVisitorCameras().then((d) => setStatuses(Object.fromEntries(d.cameras.map((c) => [c.camera_id, c.online])))).catch(() => {});
  }, []);

  useEffect(() => {
    loadCameras();
    api.getAnalyticsSettings().then((s) => setFeature(s.features?.footfall ?? null)).catch(() => {});
  }, [loadCameras, tick]);

  useEffect(() => {
    setHistory(null);
    api.getFootfallHistory(from, to).then((d) => setHistory(d.days)).catch((e) => setError(e.message));
  }, [from, to, tick]);

  useEffect(() => {
    setDayStats(null);
    api.getFootfallDay(day).then(setDayStats).catch((e) => setError(e.message));
  }, [day, tick]);

  const totals = useMemo(() => {
    if (!history) return null;
    const entries = history.reduce((a, d) => a + d.entries, 0);
    const exits = history.reduce((a, d) => a + d.exits, 0);
    const employees = history.reduce((a, d) => a + d.employee_entries, 0);
    const busiest = history.reduce((b, d) => (d.entries > (b?.entries ?? -1) ? d : b), null);
    return { entries, exits, employees, avg: history.length ? Math.round(entries / history.length) : 0, busiest };
  }, [history]);

  const hourly = useMemo(() => {
    if (!dayStats) return [];
    const m = Object.fromEntries(dayStats.hourly.map((h) => [h.hour, h.entries]));
    const hours = dayStats.hourly.length ? dayStats.hourly.map((h) => h.hour) : [9, 18];
    const lo = Math.min(...hours, 8);
    const hi = Math.max(...hours, 19);
    return Array.from({ length: hi - lo + 1 }, (_, k) => ({ hour: hourLabel(lo + k), entries: m[lo + k] || 0 }));
  }, [dayStats]);

  async function toggleFeature() {
    try {
      await api.setAnalyticsFeature("footfall", !featureOn(feature));
      setTick((t) => t + 1);
    } catch (e) {
      setError(e.message);
    }
  }

  async function remove(g) {
    try {
      await api.removeFootfallCamera(g.camera_id);
      setRemoving(null);
      setTick((t) => t + 1);
    } catch (e) {
      setError(e.message);
    }
  }

  const gates = summary?.gates || [];

  return (
    <div className="space-y-5">
      <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 flex flex-wrap items-center gap-3">
        <span className="w-9 h-9 rounded-xl bg-emerald-50 text-emerald-600 grid place-items-center">
          <Footprints size={18} />
        </span>
        <div className="mr-auto">
          <h3 className="text-sm font-semibold text-ink-900">Footfall</h3>
          <p className="text-xs text-slate-500">People who came each day, each person counted once: employees recognised on any camera and visitors.</p>
        </div>
        {feature !== null && (
          <label className="flex items-center gap-2 text-xs text-slate-600 cursor-pointer">
            <input type="checkbox" checked={featureOn(feature)} onChange={toggleFeature} /> Counting on
          </label>
        )}
        <Link to="/footfall-uat" className="text-xs text-slate-400 hover:text-brand-600">Test tool</Link>
      </div>
      {error && <p className="text-sm text-danger-500">{error}</p>}

      {/* A. Reports over a date range */}
      <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 space-y-4">
        <div className="flex flex-wrap items-end gap-2">
          <h3 className="text-sm font-semibold text-ink-900 mr-auto">Footfall reports</h3>
          <div className="flex flex-wrap gap-1">
            {RANGES.map(([k, label, start]) => (
              <button key={k} type="button" onClick={() => { setRange(k); setFrom(start()); setTo(todayIso()); }}
                className={`px-3 py-1.5 rounded-full text-xs ${range === k ? "bg-brand-500 text-white" : "bg-slate-50 text-slate-600 hover:bg-slate-100"}`}>
                {label}
              </button>
            ))}
          </div>
          <input type="date" className="input-field !w-auto !py-1.5 text-xs" value={from} max={to} onChange={(e) => { setRange(""); setFrom(e.target.value); }} />
          <input type="date" className="input-field !w-auto !py-1.5 text-xs" value={to} min={from} max={todayIso()} onChange={(e) => { setRange(""); setTo(e.target.value); }} />
        </div>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <Tile label="People counted" value={totals?.entries} sub="each person once a day" />
          <Tile label="Employees / visitors" value={totals ? `${totals.employees} / ${totals.entries - totals.employees}` : null} sub="employee days / visitor days" />
          <Tile label="Average per day" value={totals?.avg} />
          <Tile label="Busiest day" value={totals?.busiest?.entries ? totals.busiest.entries : "—"}
            sub={totals?.busiest?.entries ? new Date(`${totals.busiest.day}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" }) : "no entries"} />
        </div>
        <div className="flex gap-1">
          {["daily", "weekly", "monthly"].map((p) => (
            <button key={p} type="button" onClick={() => setPeriod(p)}
              className={`px-2.5 py-1 rounded-full text-[11px] capitalize ${period === p ? "bg-ink-900 text-white" : "bg-slate-50 text-slate-600"}`}>
              {p}
            </button>
          ))}
        </div>
        <div className="h-64">
          {history ? (
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={group(history, period)}>
                <CartesianGrid vertical={false} stroke="#eef0f5" />
                <XAxis dataKey="label" tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} minTickGap={10} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} width={28} />
                <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} cursor={{ fill: "#f8fafc" }} />
                <Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar dataKey="employee_entries" name="Employees" stackId="p" fill="var(--color-brand-500)" maxBarSize={30} />
                <Bar dataKey="visitor_entries" name="Visitors" stackId="p" fill="#fbbf24" radius={[4, 4, 0, 0]} maxBarSize={30} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <p className="text-sm text-slate-400 text-center pt-20">Loading…</p>
          )}
        </div>
      </section>

      {/* Arrivals by hour, any day */}
      <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-ink-900 mr-auto">Arrivals by hour</h3>
          <input type="date" className="input-field !w-auto !py-1.5 text-xs" value={day} max={todayIso()} onChange={(e) => e.target.value && setDay(e.target.value)} />
        </div>
        {dayStats && (
          <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-600">
            <span><b className="text-ink-900 text-base font-semibold">{dayStats.entries}</b> people</span>
            <span><b className="text-ink-900 text-base font-semibold">{dayStats.exits}</b> left</span>
            <span>Peak: <b className="text-ink-900">{dayStats.peak_hour == null ? "—" : `${hourLabel(dayStats.peak_hour)}–${hourLabel((dayStats.peak_hour + 1) % 24)}`}</b></span>
            <span>{dayStats.employee_entries} employees · {dayStats.visitor_entries} others</span>
          </div>
        )}
        <div className="h-52">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={hourly}>
              <CartesianGrid vertical={false} stroke="#eef0f5" />
              <XAxis dataKey="hour" tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} />
              <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} width={28} />
              <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} cursor={{ fill: "#f8fafc" }} />
              <Bar dataKey="entries" name="Arrivals" fill="#34d399" radius={[4, 4, 0, 0]} maxBarSize={26} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </section>

      {/* B. Counting cameras */}
      <section className="space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold text-ink-900 mr-auto flex items-center gap-1.5">
            <Camera size={15} className="text-brand-600" /> Counting cameras
          </h3>
          <Link to="/cameras" className="btn-secondary text-xs inline-flex items-center gap-1.5">
            <Settings2 size={13} /> Camera settings
          </Link>
          <button type="button" className="btn-primary text-sm inline-flex items-center gap-1.5" onClick={() => setEditId(null)}>
            <Plus size={15} /> Add camera
          </button>
        </div>
        {gates.length === 0 ? (
          <p className="bg-white rounded-2xl p-6 text-sm text-slate-500">No camera is counting yet. Click “Add camera”, choose a camera and mark the gate.</p>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {gates.map((g) => {
              const cam = cameras.find((c) => c.id === g.camera_id);
              const online = statuses[g.camera_id];
              return (
                <div key={g.camera_id} className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] overflow-hidden flex flex-col">
                  {cam ? <LiveCameraTile camera={cam} overlay={false} onClick={() => setViewing(cam)} /> : <div className="aspect-video bg-slate-100" />}
                  <div className="p-3 space-y-2 text-xs">
                    <div className="flex items-center gap-1.5 flex-wrap">
                      <DoorOpen size={13} className="text-slate-400" />
                      <span className="text-sm font-medium text-ink-900">{g.name}</span>
                      <span className={`badge ${online ? "badge-success" : online === false ? "badge-danger" : "badge-neutral"}`}>
                        {online ? "Online" : online === false ? "Offline" : "Status unknown"}
                      </span>
                      {!g.counting && <span className="badge badge-warning">not counting</span>}
                    </div>
                    <p className="text-slate-600">
                      <b className="text-ink-900">{g.unique_today}</b> counted today ·{" "}
                      {g.entry_line ? "entry line set: only inward entries count" : <span className="text-amber-700">no entry line: everyone in the area counts</span>}
                    </p>
                    <div className="flex items-center gap-3 border-t border-slate-100 pt-2">
                      <button type="button" className="text-brand-600 font-medium inline-flex items-center gap-1" onClick={() => setEditId(g.camera_id)}>
                        <Pencil size={12} /> {g.entry_line ? "Edit gate" : "Draw entry line"}
                      </button>
                      <button type="button" className="ml-auto text-slate-400 hover:text-danger-500 inline-flex items-center gap-1" onClick={() => setRemoving(g)}>
                        <Trash2 size={12} /> Stop counting
                      </button>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </section>

      {/* C. Today: counts, people and their clips (the Footfall screen) */}
      <section className="space-y-2">
        <FootfallPeople reloadKey={tick} />
      </section>

      <FootfallAddCamera
        open={editId !== undefined}
        initialCameraId={editId ?? null}
        gates={gates}
        onClose={() => setEditId(undefined)}
        onDone={() => setTick((t) => t + 1)}
      />
      <Modal open={!!removing} onClose={() => setRemoving(null)} title="Stop counting at this camera?">
        <p className="text-sm text-slate-600 mb-4">
          {removing?.name} will no longer count footfall and its counting area is cleared. People already counted, and all past reports, stay.
        </p>
        <div className="flex justify-end gap-2">
          <button onClick={() => setRemoving(null)} className="btn-secondary text-sm">Cancel</button>
          <button onClick={() => remove(removing)} className="btn-primary text-sm">Stop counting</button>
        </div>
      </Modal>
      {viewing && <CameraViewerModal camera={viewing} overlay={false} onClose={() => setViewing(null)} />}
    </div>
  );
}

// The people counted on one day: one row per person (employees by face on any camera,
// visitors by their number), first and last sighting, the cameras that saw them.
function FootfallPeople({ reloadKey }) {
  const [date, setDate] = useState(todayIso());
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [show, setShow] = useState("all");
  useEffect(() => {
    setData(null);
    api.getFootfallSitePeople(date).then((d) => { setData(d.people); setError(""); }).catch((e) => setError(e.message));
  }, [date, reloadKey]);
  const time = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const rows = (data || []).filter((p) => show === "all" || p.kind === show);
  const count = (k) => (data || []).filter((p) => k === "all" || p.kind === k).length;
  return (
    <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-sm font-semibold text-ink-900 mr-auto">People counted</h3>
        <div className="inline-flex rounded-full bg-slate-100 p-0.5">
          {[["all", "All"], ["employee", "Employees"], ["visitor", "Visitors"]].map(([k, label]) => (
            <button key={k} type="button" onClick={() => setShow(k)}
              className={`px-3 py-1 rounded-full text-xs font-medium ${show === k ? "bg-white text-ink-900 shadow-sm" : "text-slate-500 hover:text-ink-900"}`}>
              {label}{data ? ` (${count(k)})` : ""}
            </button>
          ))}
        </div>
        <input type="date" className="input-field !w-auto !py-1.5 text-xs" value={date} max={todayIso()} onChange={(e) => e.target.value && setDate(e.target.value)} />
      </div>
      {error && <p className="text-sm text-danger-500">{error}</p>}
      {!data && !error && <p className="text-sm text-slate-400 py-6 text-center">Loading…</p>}
      {data && rows.length === 0 && <p className="text-sm text-slate-400 py-6 text-center">Nobody counted on this day.</p>}
      {rows.length > 0 && (
        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr><th>Type</th><th>Who</th><th>Arrived</th><th>Last seen</th><th>Cameras</th><th>Status</th></tr>
            </thead>
            <tbody>
              {rows.map((p) => (
                <tr key={p.key}>
                  <td><span className={`badge ${p.kind === "employee" ? "badge-success" : "badge-warning"}`}>{p.kind === "employee" ? "Employee" : "Visitor"}</span></td>
                  <td className="font-medium text-ink-900">{p.name}</td>
                  <td>{time(p.first_seen)}</td>
                  <td>{time(p.last_seen)}</td>
                  <td className="text-xs text-slate-500">{p.camera_names.join(", ")}</td>
                  <td><span className={`badge ${p.left ? "badge-neutral" : "badge-success"}`}>{p.left ? "Left" : "On site"}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function featureOn(f) {
  if (f === null || f === undefined) return false;
  if (typeof f === "boolean") return f;
  return !!(f.on ?? f.enabled);
}

function Tile({ label, value, sub }) {
  return (
    <div className="rounded-xl bg-slate-50 px-3 py-2">
      <p className="text-[11px] text-slate-500">{label}</p>
      <p className="text-xl font-semibold text-ink-900 tabular-nums">{value ?? "—"}</p>
      {sub && <p className="text-[10px] text-slate-400">{sub}</p>}
    </div>
  );
}
