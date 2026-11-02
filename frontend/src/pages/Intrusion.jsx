import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer } from "recharts";
import { ArrowDownRight, ArrowUpRight, Download, Film, MapPinned, Minus } from "lucide-react";
import PageHeader from "../components/PageHeader";
import DataTable from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import ToggleSwitch from "../components/ToggleSwitch";
import SidePanel from "../components/SidePanel";
import AlertClip from "../components/intrusion/AlertClip";
import IntrusionKpiCards from "../components/intrusion/IntrusionKpiCards";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import { formatDuration } from "../lib/zoneSettings";

// Intrusion Detection (backend/app/intrusion.py, ported from MAINDV): restricted areas and entry
// gates alert when someone's feet are inside them during their active hours; loitering zones alert
// when someone stays longer than the zone's limit. Every alert gets a ~10 s evidence clip.
//
// One page: 3 KPI cards, then Intrusion Activity (every alert, with its video) and, below it,
// Historical Insights. One set of filters drives both sections. Zones (outline, active hours,
// timer) are managed in Visitor Management > Zones & areas.

const ZONES_PAGE = "/visitors?tab=zones";
const BAR = "#4f5fea";
const REFRESH_MS = 20000;
const TYPE_LABEL = { "Intrusion Detected": "Intrusion", "Loitering Detected": "Loitering" };

const iso = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const daysAgo = (n) => {
  const d = new Date();
  d.setDate(d.getDate() - n);
  return iso(d);
};
const when = (ts) => new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
const hourLabel = (h) => `${h % 12 === 0 ? 12 : h % 12}${h < 12 ? "am" : "pm"}`;
const plural = (x, one, many = `${one}s`) => `${x} ${x === 1 ? one : many}`;

const DEFAULT_FILTERS = () => ({ start: daysAgo(6), end: iso(new Date()), camera_id: "", type: "", status: "", q: "" });

export default function Intrusion() {
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [cameras, setCameras] = useState([]);
  const [processing, setProcessing] = useState(null); // the "intrusion" analytics switch
  const [tick, setTick] = useState(0);                 // bumps on each refresh, for the cards
  const activityRef = useRef(null);
  const insightsRef = useRef(null);

  const refresh = useCallback(() => {
    setTick((t) => t + 1);
    api.getAnalyticsSettings().then((r) => setProcessing(r.features?.intrusion || null)).catch(() => setProcessing(null));
  }, []);

  useEffect(() => {
    refresh();
    api.getCameras().then(setCameras).catch(() => setCameras([]));
    const t = setVisibleInterval(refresh, REFRESH_MS);
    return () => clearVisibleInterval(t);
  }, [refresh]);

  async function toggleDetection(on) {
    try {
      await api.setAnalyticsFeature("intrusion", on);
    } finally {
      refresh();
    }
  }

  const scrollTo = (ref) => setTimeout(() => ref.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 50);
  const today = iso(new Date());

  return (
    <div className="space-y-5">
      <PageHeader
        title="Intrusion Detection"
        action={
          <Link to={ZONES_PAGE} className="btn-secondary text-sm inline-flex items-center gap-1.5">
            <MapPinned size={15} /> Manage zones
          </Link>
        }
      />
      <div className="flex flex-wrap items-center gap-3 text-sm -mt-2">
        <p className="text-slate-500 mr-auto">
          Who entered a restricted area or entry gate, who stayed too long, and the video of each incident.
        </p>
        {processing && (
          <span className="flex items-center gap-2 text-xs text-slate-500">
            <span className={`badge ${processing.effective ? "badge-success" : processing.on ? "badge-warning" : "badge-neutral"}`}>
              Detection {processing.effective ? "running" : processing.on ? "paused" : "off"}
            </span>
            <ToggleSwitch checked={!!processing.on} onChange={toggleDetection} />
          </span>
        )}
      </div>

      <IntrusionKpiCards
        refreshKey={tick}
        onToday={() => {
          setFilters((f) => ({ ...f, start: today, end: today, type: "intrusion" }));
          scrollTo(activityRef);
        }}
        onLoitering={() => {
          setFilters((f) => ({ ...f, start: today, end: today, type: "loitering" }));
          scrollTo(activityRef);
        }}
        onTrends={(days) => {
          setFilters((f) => ({ ...f, start: daysAgo(days - 1), end: today }));
          scrollTo(insightsRef);
        }}
      />

      <Filters filters={filters} setFilters={setFilters} cameras={cameras} />

      <section ref={activityRef} className="scroll-mt-4 space-y-3">
        <h2 className="text-base font-semibold text-ink-900">Intrusion Activity</h2>
        <Activity filters={filters} tick={tick} />
      </section>

      <section ref={insightsRef} className="scroll-mt-4 space-y-3">
        <h2 className="text-base font-semibold text-ink-900">Historical Insights</h2>
        <Insights filters={filters} tick={tick} />
      </section>
    </div>
  );
}

// ---------------------------------------------------------------- filters (both sections)

function Filters({ filters: f, setFilters, cameras }) {
  const set = (k) => (e) => setFilters((x) => ({ ...x, [k]: e.target.value }));
  const [error, setError] = useState("");
  const today = iso(new Date());
  const presets = [
    ["Today", today, today],
    ["7 days", daysAgo(6), today],
    ["30 days", daysAgo(29), today],
    ["90 days", daysAgo(89), today],
  ];
  return (
    <div className="card p-4 space-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-1">
        {presets.map(([label, s, e]) => (
          <button key={label} type="button" onClick={() => setFilters((x) => ({ ...x, start: s, end: e }))}
            className={`px-3 py-1.5 rounded-full text-xs ${f.start === s && f.end === e ? "bg-ink-900 text-white" : "bg-slate-50 text-slate-600 hover:bg-slate-100"}`}>
            {label}
          </button>
        ))}
        {(f.type || f.camera_id || f.status || f.q) && (
          <button type="button" className="text-xs text-brand-600 ml-2" onClick={() => setFilters((x) => ({ ...DEFAULT_FILTERS(), start: x.start, end: x.end }))}>
            Clear filters
          </button>
        )}
        <button type="button" className="btn-secondary text-xs inline-flex items-center gap-1.5 ml-auto"
          onClick={() => api.intrusionApi.exportXlsx(f.start, f.end, { camera_id: f.camera_id, type: f.type }).catch((e) => setError(e.message))}>
          <Download size={13} /> Report (.xlsx)
        </button>
      </div>
      <div className="flex flex-wrap items-end gap-3">
        <label className="block">
          <span className="text-xs text-slate-500">From</span>
          <input type="date" value={f.start} max={f.end} onChange={set("start")} className="input-field mt-1" />
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">To</span>
          <input type="date" value={f.end} min={f.start} max={today} onChange={set("end")} className="input-field mt-1" />
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">Alert type</span>
          <select value={f.type} onChange={set("type")} className="input-field mt-1">
            <option value="">Intrusion and loitering</option>
            <option value="intrusion">Intrusion</option>
            <option value="loitering">Loitering</option>
          </select>
        </label>
        <label className="block">
          <span className="text-xs text-slate-500">Camera</span>
          <select value={f.camera_id} onChange={set("camera_id")} className="input-field mt-1">
            <option value="">All cameras</option>
            {cameras.map((c) => (
              <option key={c.id} value={c.id}>
                {c.label}
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
      </div>
      {error && <p className="text-xs text-danger-500">{error}</p>}
    </div>
  );
}

const clean = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== "" && v !== null && v !== undefined));

// ---------------------------------------------------------------- activity (alerts)

function Activity({ filters: f, tick }) {
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState(null);

  const load = useCallback(() => {
    api.intrusionApi
      .alerts(clean(f))
      .then((d) => {
        setRows(d.alerts);
        setError("");
      })
      .catch((e) => setError(e.message));
  }, [f]);
  useEffect(() => {
    load();
  }, [load, tick]);

  useEffect(() => {
    if (selected && rows) {
      const fresh = rows.find((r) => r.id === selected.id);
      if (fresh && (fresh.status !== selected.status || fresh.clip_status !== selected.clip_status)) setSelected(fresh);
    }
  }, [rows, selected]);

  async function act(kind) {
    try {
      if (kind === "ack") await api.acknowledgeAlert(selected.id);
      else {
        const reason = window.prompt("Why is this alert resolved?", "Checked on camera");
        if (!reason) return;
        await api.resolveAlert(selected.id, reason);
      }
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  const n = (event) => (rows || []).filter((r) => r.event === event).length;
  const withVideo = (rows || []).filter((r) => r.has_clip).length;

  return (
    <>
      {error && <p className="text-sm text-danger-500">{error}</p>}
      {!rows ? (
        <p className="text-sm text-slate-400 py-6 text-center">Loading alerts…</p>
      ) : (
        <>
          <p className="text-xs text-slate-500">
            {plural(rows.length, "alert")} · {n("Intrusion Detected")} intrusion · {n("Loitering Detected")} loitering
            {withVideo ? ` · ${withVideo} with video` : ""}
          </p>
          <DataTable
            rows={rows}
            onRowClick={setSelected}
            emptyLabel="No intrusion or loitering alerts for these filters."
            columns={[
              { key: "ts", label: "Detected", render: (r) => <span className="tabular-nums whitespace-nowrap">{when(r.ts)}</span> },
              {
                key: "event",
                label: "Type",
                render: (r) => <span className={`badge ${r.event === "Loitering Detected" ? "badge-warning" : "badge-danger"}`}>{TYPE_LABEL[r.event] || r.event}</span>,
              },
              { key: "camera", label: "Camera", render: (r) => r.camera_name },
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
                label: "Video",
                render: (r) =>
                  r.has_clip ? (
                    <span className="text-brand-600 text-xs inline-flex items-center gap-1"><Film size={12} /> Play</span>
                  ) : (
                    <span className="text-xs text-slate-400">{r.clip_status === "pending" ? "preparing" : r.clip_status || "-"}</span>
                  ),
              },
            ]}
          />
        </>
      )}

      <SidePanel open={!!selected} onClose={() => setSelected(null)} width="max-w-xl" title={selected ? `${TYPE_LABEL[selected.event] || selected.event} · ${selected.zone_name || ""}` : ""}>
        {selected && (
          <div className="space-y-4 text-sm">
            <AlertClip alertId={selected.id} clipStatus={selected.clip_status} label={`${selected.event} ${selected.zone_name}`} onPending={load} />
            {selected.message && <p className="text-ink-900">{selected.message}</p>}
            <dl className="grid grid-cols-3 gap-y-2">
              {[
                ["Camera", selected.camera_name],
                ["Area", selected.area || "-"],
                ["Zone", `${selected.zone_name || "-"}${selected.is_entry_gate ? " (entry gate)" : ""}`],
                ["Detected", when(selected.ts)],
                ["Last seen", when(selected.last_seen)],
                ["Duration", formatDuration(selected.duration_seconds)],
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
    </>
  );
}

// ---------------------------------------------------------------- historical insights

function weekKey(dateIso) {
  const d = new Date(`${dateIso}T12:00:00`);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7)); // Monday
  return iso(d);
}

function Insights({ filters: f, tick }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [period, setPeriod] = useState("Daily");

  useEffect(() => {
    api.intrusionApi
      .analytics(clean({ start: f.start, end: f.end, camera_id: f.camera_id, type: f.type }))
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(e.message));
  }, [f.start, f.end, f.camera_id, f.type, tick]);

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

  if (error) return <p className="text-sm text-danger-500">{error}</p>;
  if (!data) return <p className="text-sm text-slate-400 py-10 text-center">Loading insights…</p>;

  const prev = data.previous || { total: 0, intrusions: 0, loitering: 0, start: "-", end: "-" };
  const delta = data.total - prev.total;
  const pct = prev.total ? Math.round((delta / prev.total) * 100) : null;
  const Arrow = delta > 0 ? ArrowUpRight : delta < 0 ? ArrowDownRight : Minus;
  const tone = delta > 0 ? "text-rose-600 bg-rose-50" : delta < 0 ? "text-emerald-700 bg-emerald-50" : "text-slate-600 bg-slate-100";
  const topCam = data.by_camera[0];
  const areas = data.by_area.some((a) => a.name !== "No area") ? data.by_area.filter((a) => a.name !== "No area") : data.by_zone;
  const topArea = areas[0];
  const peak = data.peak_hour;
  const split = data.intrusions + data.loitering;

  const chart = (rows, height = "h-56") => (
    <div className={height}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 8, right: 8, left: -20, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke="#eceef4" />
          <XAxis dataKey="label" tickLine={false} axisLine={false} tick={{ fontSize: 11, fill: "#94a3b8" }} />
          <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fontSize: 11, fill: "#94a3b8" }} />
          <Tooltip />
          <Bar dataKey="count" name="Incidents" fill={BAR} radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );

  const ranked = (title, items) => (
    <div className="card p-4">
      <h3 className="font-semibold text-ink-900 text-sm mb-3">{title}</h3>
      {!items?.length ? (
        <p className="text-xs text-slate-400">No incidents in this range.</p>
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
      {/* The answer in one line: how many, which way, when and where. */}
      <div className="card p-4 flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <span>
          <span className="text-2xl font-light text-ink-900 tabular-nums mr-1.5">{data.total}</span>
          {data.total === 1 ? "incident" : "incidents"} from {data.start} to {data.end}
        </span>
        <span className={`inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium ${tone}`}>
          <Arrow size={13} />
          {delta === 0 ? "Same as" : `${delta > 0 ? "+" : ""}${delta}${pct !== null ? ` (${pct > 0 ? "+" : ""}${pct}%)` : ""} vs`} the previous period ({prev.total}, {prev.start} to {prev.end})
        </span>
        <span className="text-slate-600">
          Peak time: <b className="font-medium text-ink-900">{peak === null ? "—" : `${hourLabel(peak)}–${hourLabel((peak + 1) % 24)}`}</b>
        </span>
        <span className="text-slate-600">
          Most incidents: <b className="font-medium text-ink-900">{topCam ? `${topCam.name} (${topCam.count})` : "—"}</b>
          {topArea ? <> · <b className="font-medium text-ink-900">{topArea.name}</b></> : null}
        </span>
      </div>

      <div className="grid lg:grid-cols-3 gap-4">
        <div className="card p-4 lg:col-span-2">
          <div className="flex items-center justify-between mb-2">
            <h3 className="font-semibold text-ink-900 text-sm">Incidents over time</h3>
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
        <div className="card p-4">
          <h3 className="font-semibold text-ink-900 text-sm mb-3">Intrusion vs loitering</h3>
          {split === 0 ? (
            <p className="text-xs text-slate-400">No incidents in this range.</p>
          ) : (
            <div className="space-y-3 text-sm">
              {[
                ["Intrusion", data.intrusions, prev.intrusions, "bg-rose-400"],
                ["Loitering", data.loitering, prev.loitering, "bg-amber-400"],
              ].map(([label, count, before, cls]) => (
                <div key={label}>
                  <div className="flex justify-between">
                    <span>{label}</span>
                    <span className="tabular-nums">
                      <b className="font-medium">{count}</b>{" "}
                      <span className="text-xs text-slate-400">({Math.round((count / split) * 100)}% · previous period {before})</span>
                    </span>
                  </div>
                  <div className="h-2 rounded-full bg-slate-100 mt-1 overflow-hidden">
                    <div className={`h-full rounded-full ${cls}`} style={{ width: `${(count / split) * 100}%` }} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="card p-4">
        <h3 className="font-semibold text-ink-900 text-sm mb-2">Peak intrusion hours</h3>
        {chart(data.by_hour.map((h) => ({ label: h.label, count: h.count })), "h-44")}
      </div>

      <div className="grid lg:grid-cols-2 gap-4">
        {ranked("By camera", data.by_camera)}
        {ranked("By area / zone", areas)}
      </div>
    </div>
  );
}
