import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { Download, FileSpreadsheet } from "lucide-react";
import * as api from "../../api/client";
import { shiftIso, todayIso } from "../workforce/shared";
import { dur } from "./shared";

// Who visits, how often, why and whom they meet, for a date range. All from
// visit records (/visitor-records/analytics); exportable as CSV / Excel.

const RANGES = [
  { key: "30", label: "Last 30 days", from: () => shiftIso(todayIso(), -29) },
  { key: "month", label: "This month", from: () => `${todayIso().slice(0, 7)}-01` },
  { key: "90", label: "Last 90 days", from: () => shiftIso(todayIso(), -89) },
  { key: "365", label: "Last 12 months", from: () => shiftIso(todayIso(), -364) },
];
const PERIODS = { daily: ["daily", "day"], weekly: ["weekly", "week"], monthly: ["monthly", "month"] };

export default function VisitorAnalytics({ onOpenVisitor }) {
  const [from, setFrom] = useState(RANGES[0].from());
  const [to, setTo] = useState(todayIso());
  const [period, setPeriod] = useState("daily");
  const [d, setD] = useState(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState("");

  useEffect(() => {
    setD(null);
    api.vr.analytics(from, to).then((r) => { setD(r); setError(""); }).catch((e) => setError(e.message));
  }, [from, to]);

  async function download(format) {
    setBusy(format);
    try {
      await api.vr.download(format, { from, to });
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  const [series, key] = PERIODS[period];
  const label = (x) =>
    period === "monthly"
      ? new Date(`${x}-15T12:00:00`).toLocaleDateString([], { month: "short", year: "2-digit" })
      : new Date(`${x}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short" });

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 flex flex-wrap items-end gap-3 text-xs">
        <div className="flex gap-1 flex-wrap">
          {RANGES.map((r) => (
            <button key={r.key} type="button" onClick={() => { setFrom(r.from()); setTo(todayIso()); }}
              className={`px-3 py-1.5 rounded-full ${from === r.from() && to === todayIso() ? "bg-brand-500 text-white" : "bg-slate-50 text-slate-600 hover:bg-slate-100"}`}>
              {r.label}
            </button>
          ))}
        </div>
        <label>From <input type="date" className="input-field text-xs w-auto ml-1" value={from} max={to} onChange={(e) => setFrom(e.target.value)} /></label>
        <label>To <input type="date" className="input-field text-xs w-auto ml-1" value={to} min={from} onChange={(e) => setTo(e.target.value)} /></label>
        <div className="ml-auto flex gap-2">
          <button type="button" className="btn-secondary text-xs inline-flex items-center gap-1.5" disabled={!!busy} onClick={() => download("csv")}><Download size={13} /> CSV</button>
          <button type="button" className="btn-secondary text-xs inline-flex items-center gap-1.5" disabled={!!busy} onClick={() => download("xlsx")}><FileSpreadsheet size={13} /> Excel</button>
        </div>
      </div>
      {error && <p className="text-sm text-danger-500">{error}</p>}
      {!d && !error && <p className="text-sm text-slate-400 py-10 text-center">Loading…</p>}
      {d && (
        <>
          <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
            <Tile label="Visits" value={d.total_visits} />
            <Tile label="Unique visitors" value={d.unique_visitors} />
            <Tile label="Repeat visitors" value={d.repeat_visitors} sub="came more than once" />
            <Tile label="Average visit duration" value={dur(d.average_duration_seconds, "No data")} sub={`${d.duration_measured} completed visits`} />
            <Tile label="Average waiting time" value={dur(d.average_waiting_seconds, "Not recorded")} sub={`${d.waiting_measured} with a meeting start`} />
          </div>

          <Card title="Visitor count" right={
            <div className="flex gap-1">
              {Object.keys(PERIODS).map((p) => (
                <button key={p} type="button" onClick={() => setPeriod(p)}
                  className={`px-2.5 py-1 rounded-full text-[11px] capitalize ${period === p ? "bg-ink-900 text-white" : "bg-slate-50 text-slate-600"}`}>{p}</button>
              ))}
            </div>
          }>
            <div className="h-60">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={d[series].map((x) => ({ ...x, label: label(x[key]) }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#eef0f5" vertical={false} />
                  <XAxis dataKey="label" tick={{ fontSize: 11, fill: "#94a3b8" }} tickLine={false} axisLine={false} minTickGap={12} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "#94a3b8" }} tickLine={false} axisLine={false} width={28} />
                  <Tooltip cursor={{ fill: "#f8fafc" }} contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                  <Bar dataKey="visits" name="Visits" fill="var(--color-brand-500)" radius={[4, 4, 0, 0]} maxBarSize={36} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Card>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
            <Card title="Repeat visitor frequency">
              <Bars rows={d.frequency.map((f) => ({ label: f.label, visits: f.visitors }))} unit="visitors" color="bg-violet-400" />
            </Card>
            <Card title="Most frequent visitors">
              {d.top_visitors.length === 0 ? <Empty /> : (
                <ul className="space-y-1.5">
                  {d.top_visitors.map((t) => (
                    <li key={t.visitor_id}>
                      <button type="button" className="w-full flex items-center justify-between text-xs hover:text-brand-600" onClick={() => onOpenVisitor(t.visitor_id)}>
                        <span className="truncate text-left">
                          <span className="text-ink-900 font-medium">{t.name}</span>
                          <span className="text-slate-400"> · {t.company || t.visitor_code}</span>
                        </span>
                        <span className="text-slate-600 shrink-0 tabular-nums">{t.visits}</span>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </Card>
            <Card title="Visitors by purpose">
              <Bars rows={d.by_purpose} color="bg-sky-400" />
            </Card>
            <Card title="Visitors by department">
              <Bars rows={d.by_department} color="bg-emerald-400" />
            </Card>
            <Card title="Visitors by host" className="lg:col-span-2">
              <Bars rows={d.by_host.slice(0, 12)} color="bg-brand-500" />
            </Card>
          </div>
        </>
      )}
    </div>
  );
}

function Card({ title, right, children, className = "" }) {
  return (
    <div className={`bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 ${className}`}>
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-[13px] font-semibold text-ink-900">{title}</h3>
        {right}
      </div>
      {children}
    </div>
  );
}

function Tile({ label, value, sub }) {
  return (
    <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] px-4 py-3">
      <p className="text-[11px] text-slate-500">{label}</p>
      <p className="text-xl font-semibold text-ink-900 tabular-nums">{value}</p>
      {sub && <p className="text-[10px] text-slate-400">{sub}</p>}
    </div>
  );
}

function Bars({ rows, unit = "visits", color }) {
  if (!rows.length || rows.every((r) => !r.visits)) return <Empty />;
  const max = Math.max(...rows.map((r) => r.visits), 1);
  return (
    <ul className="space-y-2">
      {rows.map((r) => (
        <li key={r.label} className="text-xs">
          <div className="flex justify-between mb-0.5">
            <span className="text-slate-600 truncate">{r.label}</span>
            <span className="text-slate-500 tabular-nums">{r.visits} {unit}</span>
          </div>
          <div className="h-1.5 rounded-full bg-slate-100">
            <div className={`h-1.5 rounded-full ${color}`} style={{ width: `${(r.visits / max) * 100}%` }} />
          </div>
        </li>
      ))}
    </ul>
  );
}

const Empty = () => <p className="text-xs text-slate-400 py-4 text-center">No visits in this range.</p>;
