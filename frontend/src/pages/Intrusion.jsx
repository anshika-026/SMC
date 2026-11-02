import { useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from "recharts";
import { DoorOpen, Download, Film, Plus, ShieldAlert } from "lucide-react";
import PageHeader from "../components/PageHeader";
import StatCard from "../components/StatCard";
import DataTable from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import ToggleSwitch from "../components/ToggleSwitch";
import Tabs from "../components/Tabs";
import SidePanel from "../components/SidePanel";
import AlertClip from "../components/intrusion/AlertClip";
import IntrusionKpiCards from "../components/intrusion/IntrusionKpiCards";
import IntrusionZoneEditor from "../components/intrusion/IntrusionZoneEditor";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import { formatDuration, zoneAlertLabel, zoneTypeLabel } from "../lib/zoneSettings";

// Intrusion & loitering (backend/app/intrusion.py, ported from MAINDV): restricted areas and entry
// gates alert when someone's feet are inside them during their active hours; loitering zones alert
// when someone stays longer than the zone's duration. Every alert gets a ~10 s evidence clip.
// One page, reached from the dashboard's Intrusion card, Security & Safety > Intrusion and
// Analytics > Intrusion analytics (?tab=analytics).

const TABS = ["Alerts", "Zones & schedule", "Analytics"];
const TAB_PARAM = { alerts: "Alerts", zones: "Zones & schedule", analytics: "Analytics" };
const BAR = "#4f5fea";
const REFRESH_MS = 20000;

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const daysAgo = (n) => {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return iso(d);
};
const when = (ts) => new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "-");

export default function Intrusion() {
  const [params, setParams] = useSearchParams();
  const tab = TAB_PARAM[params.get("tab")] || "Alerts";
  const pickTab = (t) => setParams({ tab: Object.keys(TAB_PARAM).find((k) => TAB_PARAM[k] === t) }, { replace: true });

  const [zones, setZones] = useState([]);
  const [cameras, setCameras] = useState([]);
  const [today, setToday] = useState(null);         // today's alerts (cards)
  const [processing, setProcessing] = useState(null); // the "intrusion" analytics switch
  const [error, setError] = useState("");
  const [editor, setEditor] = useState(null);       // {zone?}

  const load = useCallback(() => {
    const d = iso(new Date());
    Promise.all([api.intrusionApi.zones(), api.intrusionApi.alerts({ start: d, end: d })])
      .then(([z, a]) => {
        setZones(z);
        setToday(a.alerts);
        setError("");
      })
      .catch((e) => setError(e.message || "Couldn't load intrusion data."));
    api.getAnalyticsSettings().then((r) => setProcessing(r.features?.intrusion || null)).catch(() => setProcessing(null));
  }, []);

  useEffect(() => {
    load();
    api.getCameras().then(setCameras).catch(() => setCameras([]));
    const t = setVisibleInterval(load, REFRESH_MS);
    return () => clearVisibleInterval(t);
  }, [load]);

  async function toggleDetection(on) {
    try {
      await api.setAnalyticsFeature("intrusion", on);
    } finally {
      load();
    }
  }


  return (
    <div className="space-y-5">
      <PageHeader
        title="Intrusion"
        action={
          <button onClick={() => setEditor({})} className="btn-primary text-sm inline-flex items-center gap-1.5">
            <Plus size={15} /> New zone
          </button>
        }
      />
      {error && <p className="text-sm text-danger-500">{error}</p>}

      {processing && (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <span className={`badge ${processing.effective ? "badge-success" : processing.on ? "badge-warning" : "badge-neutral"}`}>
            Detection {processing.effective ? "running" : processing.on ? "paused" : "off"}
          </span>
          <span className="text-slate-500">
            {processing.effective
              ? "zones inside their active hours are being guarded"
              : processing.on
                ? "switched on but waiting on what it needs (license or deployment)"
                : "switch it on to start detecting"}
          </span>
          <span className="flex items-center gap-2 text-xs text-slate-500 ml-auto">
            Intrusion detection <ToggleSwitch checked={!!processing.on} onChange={toggleDetection} />
          </span>
        </div>
      )}

      {/* KPI cards in the Attendance / Visitor Management style; a click opens the matching tab. */}
      <IntrusionKpiCards
        zones={zones}
        refreshKey={today}
        onAlerts={() => pickTab("Alerts")}
        onZones={() => pickTab("Zones & schedule")}
        onAnalytics={() => pickTab("Analytics")}
      />

      <Tabs tabs={TABS} active={tab} onChange={pickTab} />

      {tab === "Alerts" && <AlertsTab zones={zones} cameras={cameras} onChanged={load} />}
      {tab === "Zones & schedule" && <ZonesTab zones={zones} cameras={cameras} onEdit={(z) => setEditor({ zone: z })} onChanged={load} />}
      {tab === "Analytics" && <AnalyticsTab zones={zones} cameras={cameras} />}

      <IntrusionZoneEditor open={!!editor} zone={editor?.zone || null} cameras={cameras} onClose={() => setEditor(null)} onChanged={load} />
    </div>
  );
}

// ---------------------------------------------------------------- alerts

function AlertsTab({ zones, cameras, onChanged }) {
  const [f, setF] = useState({ start: daysAgo(6), end: iso(new Date()), camera_id: "", zone_id: "", status: "", q: "" });
  const [rows, setRows] = useState(null);
  const [recent, setRecent] = useState([]);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);

  const load = useCallback(() => {
    const filters = Object.fromEntries(Object.entries(f).filter(([, v]) => v !== ""));
    api.intrusionApi
      .alerts(filters)
      .then((d) => {
        setRows(d.alerts);
        setError("");
      })
      .catch((e) => setError(e.message));
    api.intrusionApi.events(daysAgo(0), iso(new Date())).then((e) => setRecent(e.slice(0, 8))).catch(() => setRecent([]));
  }, [f]);
  useEffect(() => {
    load();
    const t = setVisibleInterval(load, REFRESH_MS);
    return () => clearVisibleInterval(t);
  }, [load]);

  useEffect(() => {
    if (selected && rows) {
      const fresh = rows.find((r) => r.id === selected.id);
      if (fresh && (fresh.status !== selected.status || fresh.clip_status !== selected.clip_status)) setSelected(fresh);
    }
  }, [rows, selected]);

  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }));
  const guarding = zones.filter((z) => z.enabled && z.active_now);

  async function act(kind) {
    try {
      if (kind === "ack") await api.acknowledgeAlert(selected.id);
      else {
        const reason = window.prompt("Why is this alert resolved?", "Checked on camera");
        if (!reason) return;
        await api.resolveAlert(selected.id, reason);
      }
      load();
      onChanged();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid lg:grid-cols-3 gap-4">
        <div className="card p-4">
          <h3 className="font-semibold text-ink-900 text-sm mb-2">Guarding now</h3>
          {guarding.length === 0 ? (
            <p className="text-xs text-slate-400">No zone is inside its active hours right now.</p>
          ) : (
            <ul className="space-y-1 text-sm">
              {guarding.map((z) => (
                <li key={z.id} className="flex items-center gap-2">
                  {z.is_entry_gate ? <DoorOpen size={13} className="text-rose-500" /> : <ShieldAlert size={13} className="text-rose-500" />}
                  <span className="flex-1 truncate">{z.area ? `${z.area} · ` : ""}{z.name}</span>
                  <span className="text-xs text-slate-400">{z.camera_name}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
        <div className="card p-4 lg:col-span-2">
          <h3 className="font-semibold text-ink-900 text-sm mb-2">Live activity today (detections)</h3>
          {recent.length === 0 ? (
            <p className="text-xs text-slate-400">No one detected inside a zone today.</p>
          ) : (
            <div className="divide-y divide-[#f1f2f7]">
              {recent.map((e) => (
                <div key={e.id} className="flex items-center gap-3 py-1.5 text-sm">
                  <span className="text-slate-500 w-20 shrink-0 tabular-nums">{clock(e.ts)}</span>
                  <span className="flex-1 truncate">
                    {e.people} {e.people === 1 ? "person" : "people"} in {e.zone}
                  </span>
                  <span className="text-xs text-slate-400">{e.camera}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="card p-4 flex flex-wrap items-end gap-3 text-sm">
        <label className="block">
          <span className="text-xs text-slate-500">From</span>
          <input type="date" value={f.start} max={f.end} onChange={set("start")} className="input-field mt-1" />
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">To</span>
          <input type="date" value={f.end} min={f.start} max={iso(new Date())} onChange={set("end")} className="input-field mt-1" />
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">Camera</span>
          <select value={f.camera_id} onChange={set("camera_id")} className="input-field mt-1">
            <option value="">All cameras</option>
            {cameras.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label} (#{c.id})
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">Zone</span>
          <select value={f.zone_id} onChange={set("zone_id")} className="input-field mt-1">
            <option value="">All zones</option>
            {zones.map((z) => (
              <option key={z.id} value={z.id}>
                {z.area ? `${z.area} · ` : ""}{z.name} ({z.camera_name})
              </option>
            ))}
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">Status</span>
          <select value={f.status} onChange={set("status")} className="input-field mt-1">
            <option value="">Any</option>
            {["Active", "Acknowledged", "Resolved"].map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <label className="block flex-1 min-w-[160px]">
          <span className="text-xs text-slate-500">Search</span>
          <input value={f.q} onChange={set("q")} placeholder="Camera, area, zone or message" className="input-field mt-1" />
        </label>
        <button type="button" className="btn-secondary text-sm inline-flex items-center gap-1.5" onClick={() => api.intrusionApi.exportXlsx(f.start, f.end).catch((e) => setError(e.message))}>
          <Download size={14} /> Export
        </button>
      </div>
      {error && <p className="text-sm text-danger-500">{error}</p>}

      {!rows ? (
        <p className="text-sm text-slate-400 py-6 text-center">Loading alerts…</p>
      ) : (
        <DataTable
          rows={rows}
          onRowClick={setSelected}
          emptyLabel="No intrusion alerts in this range."
          columns={[
            { key: "ts", label: "Detected", render: (r) => <span className="tabular-nums whitespace-nowrap">{when(r.ts)}</span> },
            { key: "event", label: "Event", render: (r) => (r.event === "Loitering Detected" ? "Loitering" : "Intrusion") },
            { key: "camera", label: "Camera", render: (r) => <span>{r.camera_name} <span className="text-xs text-slate-400">#{r.camera_id}</span></span> },
            {
              key: "zone",
              label: "Area · zone",
              render: (r) => (
                <span>
                  {r.area ? `${r.area} · ` : ""}
                  {r.zone_name || "-"}
                  {r.is_entry_gate && <span className="badge badge-neutral text-[10px] ml-1">Entry gate</span>}
                </span>
              ),
            },
            { key: "duration", label: "Duration", render: (r) => formatDuration(r.duration_seconds) },
            { key: "severity", label: "Severity", render: (r) => <StatusBadge value={r.severity} /> },
            { key: "status", label: "Status", render: (r) => <StatusBadge value={r.status} /> },
            {
              key: "clip",
              label: "Clip",
              render: (r) =>
                r.has_clip ? (
                  <span className="text-brand-600 text-xs inline-flex items-center gap-1"><Film size={12} /> Play</span>
                ) : (
                  <span className="text-xs text-slate-400">{r.clip_status || "-"}</span>
                ),
            },
          ]}
        />
      )}

      <SidePanel open={!!selected} onClose={() => setSelected(null)} width="max-w-xl" title={selected ? `${selected.event} · ${selected.zone_name || ""}` : ""}>
        {selected && (
          <div className="space-y-4 text-sm">
            <AlertClip alertId={selected.id} clipStatus={selected.clip_status} label={`${selected.event} ${selected.zone_name}`} onPending={load} />
            {selected.message && <p className="text-ink-900">{selected.message}</p>}
            <dl className="grid grid-cols-3 gap-y-2">
              {[
                ["Camera", `${selected.camera_name} (#${selected.camera_id})`],
                ["Area", selected.area || "-"],
                ["Zone", `${selected.zone_name || "-"}${selected.is_entry_gate ? " (entry gate)" : ""}`],
                ["Detected", when(selected.ts)],
                ["Last seen", when(selected.last_seen)],
                ["Duration", formatDuration(selected.duration_seconds)],
                ["Detections", selected.occurrences],
                ["Severity", selected.severity],
                ["Status", selected.status],
              ].map(([k, v]) => (
                <div key={k} className="contents">
                  <dt className="text-slate-500">{k}</dt>
                  <dd className="col-span-2 text-ink-900">{v}</dd>
                </div>
              ))}
            </dl>
            {selected.status !== "Resolved" && (
              <div className="flex gap-2">
                {selected.status === "Active" && (
                  <button type="button" className="btn-secondary flex-1" onClick={() => act("ack")}>
                    Acknowledge
                  </button>
                )}
                <button type="button" className="btn-primary flex-1" onClick={() => act("resolve")}>
                  Resolve
                </button>
              </div>
            )}
          </div>
        )}
      </SidePanel>
    </div>
  );
}

// ---------------------------------------------------------------- zones

function ZonesTab({ zones, onEdit, onChanged }) {
  const [confirmDelete, setConfirmDelete] = useState(null);
  // The same area + zone name on several cameras is one zone watched by those cameras.
  const watchers = useMemo(() => {
    const m = {};
    for (const z of zones) {
      const k = `${z.area}|${z.name}`.toLowerCase();
      m[k] = (m[k] || 0) + 1;
    }
    return m;
  }, [zones]);

  async function setEnabled(zone, enabled) {
    await api.intrusionApi.updateZone(zone.id, { enabled });
    onChanged();
  }
  async function remove(zone) {
    await api.intrusionApi.deleteZone(zone.id);
    setConfirmDelete(null);
    onChanged();
  }

  return (
    <DataTable
      rows={[...zones].sort((a, b) => `${a.area}|${a.name}|${a.camera_name}`.localeCompare(`${b.area}|${b.name}|${b.camera_name}`))}
      emptyLabel="No intrusion zones yet. Use “New zone” to draw one on a camera."
      columns={[
        { key: "area", label: "Area", render: (z) => z.area || <span className="text-slate-400">-</span> },
        {
          key: "name",
          label: "Zone",
          render: (z) => (
            <span>
              {z.name}
              {z.is_entry_gate && <span className="badge badge-neutral text-[10px] ml-1">Entry gate</span>}
              {watchers[`${z.area}|${z.name}`.toLowerCase()] > 1 && (
                <span className="block text-[11px] text-slate-400">watched by {watchers[`${z.area}|${z.name}`.toLowerCase()]} cameras</span>
              )}
            </span>
          ),
        },
        { key: "camera_name", label: "Camera", render: (z) => <span>{z.camera_name} <span className="text-xs text-slate-400">#{z.camera_id}</span></span> },
        {
          key: "zone_type",
          label: "Type / alerts",
          render: (z) => (
            <span>
              {zoneTypeLabel(z)}
              <span className="block text-xs text-slate-400">{zoneAlertLabel(z)}</span>
            </span>
          ),
        },
        { key: "hours", label: "Active hours", render: (z) => (z.active_from ? `${z.active_from} – ${z.active_to}` : "Always") },
        { key: "state", label: "Now", render: (z) => <StatusBadge value={!z.enabled ? "Off" : z.active_now ? "Guarding" : "Outside hours"} /> },
        { key: "intrusions_today", label: "Detections today", render: (z) => (z.zone_type === "loitering" ? "-" : z.intrusions_today) },
        { key: "last", label: "Last", render: (z) => clock(z.last_intrusion) },
        { key: "enabled", label: "Enabled", render: (z) => <ToggleSwitch checked={z.enabled} onChange={(v) => setEnabled(z, v)} /> },
        {
          key: "edit",
          label: "",
          render: (z) => (
            <button type="button" onClick={() => onEdit(z)} className="text-brand-600 text-sm">
              Edit
            </button>
          ),
        },
        {
          key: "action",
          label: "",
          render: (z) =>
            confirmDelete === z.id ? (
              <span className="text-sm flex items-center gap-2">
                <button type="button" onClick={() => remove(z)} className="text-danger-500 font-medium">
                  Delete
                </button>
                <button type="button" onClick={() => setConfirmDelete(null)} className="text-slate-500">
                  Keep
                </button>
              </span>
            ) : (
              <button type="button" onClick={() => setConfirmDelete(z.id)} className="text-slate-500 text-sm">
                Remove
              </button>
            ),
        },
      ]}
    />
  );
}

// ---------------------------------------------------------------- analytics

const PRESETS = [
  ["7", "Last 7 days"],
  ["30", "Last 30 days"],
  ["90", "Last 90 days"],
  ["custom", "Custom"],
];

function weekKey(dateIso) {
  const d = new Date(`${dateIso}T12:00:00`);
  const day = (d.getDay() + 6) % 7; // Monday = 0
  d.setDate(d.getDate() - day);
  return iso(d);
}

function AnalyticsTab({ zones, cameras }) {
  const [preset, setPreset] = useState("30");
  const [range, setRange] = useState({ start: daysAgo(29), end: iso(new Date()) });
  const [cameraId, setCameraId] = useState("");
  const [zoneId, setZoneId] = useState("");
  const [period, setPeriod] = useState("Daily");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (preset !== "custom") setRange({ start: daysAgo(Number(preset) - 1), end: iso(new Date()) });
  }, [preset]);

  useEffect(() => {
    const filters = { ...range, ...(cameraId ? { camera_id: cameraId } : {}), ...(zoneId ? { zone_id: zoneId } : {}) };
    setData(null);
    api.intrusionApi
      .analytics(filters)
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(e.message));
  }, [range, cameraId, zoneId]);

  const trend = useMemo(() => {
    if (!data) return [];
    if (period === "Daily") return data.per_day.map((d) => ({ label: d.date.slice(5), count: d.count }));
    const m = new Map();
    for (const d of data.per_day) {
      const k = period === "Weekly" ? `w/c ${weekKey(d.date).slice(5)}` : d.date.slice(0, 7);
      m.set(k, (m.get(k) || 0) + d.count);
    }
    return [...m].map(([label, count]) => ({ label, count }));
  }, [data, period]);

  const chart = (rows, key = "label", height = "h-56") => (
    <div className={height}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="#eceef4" />
          <XAxis dataKey={key} tickLine={false} axisLine={false} tick={{ fontSize: 11, fill: "#94a3b8" }} />
          <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fontSize: 11, fill: "#94a3b8" }} />
          <Tooltip />
          <Bar dataKey="count" name="Intrusion alerts" fill={BAR} radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );

  const ranked = (title, items) => (
    <div className="card p-4">
      <h3 className="font-semibold text-ink-900 text-sm mb-3">{title}</h3>
      {!items?.length ? (
        <p className="text-xs text-slate-400">No intrusions in this range.</p>
      ) : (
        <ul className="space-y-2">
          {items.slice(0, 8).map((x) => (
            <li key={x.name} className="text-sm">
              <div className="flex justify-between">
                <span className="truncate">{x.name}</span>
                <span className="tabular-nums font-medium">{x.count}</span>
              </div>
              <div className="h-1.5 rounded-full bg-slate-100 mt-1 overflow-hidden">
                <div className="h-full rounded-full bg-indigo-400" style={{ width: `${(x.count / items[0].count) * 100}%` }} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );

  return (
    <div className="space-y-4">
      <div className="card p-4 flex flex-wrap items-end gap-3 text-sm">
        <div className="flex flex-wrap gap-1">
          {PRESETS.map(([k, label]) => (
            <button key={k} type="button" onClick={() => setPreset(k)}
              className={`px-3 py-1.5 rounded-full text-xs ${preset === k ? "bg-ink-900 text-white" : "bg-slate-50 text-slate-600 hover:bg-slate-100"}`}>
              {label}
            </button>
          ))}
        </div>
        {preset === "custom" && (
          <>
            <input type="date" value={range.start} max={range.end} onChange={(e) => setRange((r) => ({ ...r, start: e.target.value }))} className="input-field !w-auto" />
            <input type="date" value={range.end} min={range.start} max={iso(new Date())} onChange={(e) => setRange((r) => ({ ...r, end: e.target.value }))} className="input-field !w-auto" />
          </>
        )}
        <select value={cameraId} onChange={(e) => setCameraId(e.target.value)} className="input-field !w-auto">
          <option value="">All cameras</option>
          {cameras.map((c) => (
            <option key={c.id} value={c.id}>
              {c.label}
            </option>
          ))}
        </select>
        <select value={zoneId} onChange={(e) => setZoneId(e.target.value)} className="input-field !w-auto">
          <option value="">All zones</option>
          {zones.map((z) => (
            <option key={z.id} value={z.id}>
              {z.area ? `${z.area} · ` : ""}{z.name}
            </option>
          ))}
        </select>
        <button type="button" className="btn-secondary text-sm inline-flex items-center gap-1.5 ml-auto" onClick={() => api.intrusionApi.exportXlsx(range.start, range.end).catch((e) => setError(e.message))}>
          <Download size={14} /> Report (.xlsx)
        </button>
      </div>
      {error && <p className="text-sm text-danger-500">{error}</p>}
      {!data ? (
        <p className="text-sm text-slate-400 py-10 text-center">Loading analytics…</p>
      ) : (
        <>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <StatCard label="Intrusion events" value={data.total} sub={`${data.intrusions} intrusion · ${data.loitering} loitering`} subTone="neutral" />
            <StatCard label="Still open" value={data.open} sub="not resolved" subTone="danger" />
            <StatCard label="Peak time" value={data.peak_hour === null ? "—" : `${String(data.peak_hour).padStart(2, "0")}:00`} sub="hour with the most alerts" subTone="neutral" />
            <StatCard label="Average duration" value={data.average_duration_seconds == null ? "—" : formatDuration(data.average_duration_seconds)} sub={`${data.detections} detections logged`} subTone="neutral" />
          </div>
          <div className="grid lg:grid-cols-3 gap-4">
            <div className="card p-4 lg:col-span-2">
              <div className="flex items-center justify-between mb-2">
                <h3 className="font-semibold text-ink-900 text-sm">Trend</h3>
                <div className="flex gap-1">
                  {["Daily", "Weekly", "Monthly"].map((p) => (
                    <button key={p} type="button" onClick={() => setPeriod(p)}
                      className={`px-2.5 py-1 rounded-full text-xs ${period === p ? "bg-indigo-50 text-indigo-700" : "text-slate-500 hover:bg-slate-50"}`}>
                      {p}
                    </button>
                  ))}
                </div>
              </div>
              {chart(trend)}
            </div>
            {ranked("By area", data.by_area)}
          </div>
          <div className="card p-4">
            <h3 className="font-semibold text-ink-900 text-sm mb-2">Peak intrusion timings (by hour of day)</h3>
            {chart(data.by_hour, "label", "h-48")}
          </div>
          <div className="grid lg:grid-cols-2 gap-4">
            {ranked("By camera", data.by_camera)}
            {ranked("By zone", data.by_zone)}
          </div>
          <p className="text-[11px] text-slate-400">
            Counts are intrusion and loitering alerts ({data.start} to {data.end}); repeat detections while an intrusion is still open count against the same alert.
            {data.with_clip ? ` ${data.with_clip} have an evidence clip.` : ""}
          </p>
        </>
      )}
    </div>
  );
}

