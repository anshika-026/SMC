import { useCallback, useEffect, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ArrowDownRight, ArrowUpRight, CalendarDays, ChevronRight, ClipboardList, Footprints, Minus, ShieldAlert, TrendingUp, UserCheck, Users } from "lucide-react";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import { FootfallDetail, SecurityDetail, VisitorsDetail, WorkforceDetail } from "../components/dashboard/DashboardDetails";

// Business dashboard: four questions, four cards - is my workforce in, are
// visitors handled well, is activity and staffing normal, is anything unsafe -
// each opening its detailed view. Every number comes from the same backend
// calculations the feature pages use (insights.business_overview); "—" means
// not available (feature off, a live-only figure on a past date, or not set up).

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
export const mins = (s) => (s === null || s === undefined ? "—" : s < 60 ? "<1 min" : `${Math.round(s / 60)} min`);

const CARD_TONE = {
  indigo: { icon: "bg-indigo-50 text-indigo-600", bar: "from-indigo-500/70" },
  violet: { icon: "bg-violet-50 text-violet-600", bar: "from-violet-500/70" },
  emerald: { icon: "bg-emerald-50 text-emerald-600", bar: "from-emerald-500/70" },
  rose: { icon: "bg-rose-50 text-rose-600", bar: "from-rose-500/70" },
};
const DOT = { ok: "bg-emerald-500", warn: "bg-amber-500", bad: "bg-rose-500", none: "bg-slate-300" };

export default function Dashboard() {
  const [date, setDate] = useState(todayIso);
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [open, setOpen] = useState(null);   // workforce | visitors | footfall | security

  const load = useCallback(() => {
    api
      .getDashboardStats(date)
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(e.message || "Could not load the dashboard"));
  }, [date]);

  useEffect(() => {
    setData(null);
    load();
    // Live figures refresh for today; a past date doesn't change.
    const t = date === todayIso() ? setVisibleInterval(load, 30000) : null;
    return () => t && clearVisibleInterval(t);
  }, [date, load]);

  const b = data?.business;
  const isToday = date === todayIso();
  const w = b?.workforce;
  const v = b?.visitors;
  const f = b?.footfall;
  const s = b?.security;
  const short = b?.understaffed;
  const staffNow = b?.staff_now?.count ?? null;
  const waitingOver = v?.waiting?.filter((x) => x.over).length || 0;

  const cards = [
    {
      key: "workforce", title: "Workforce", icon: UserCheck, tone: "indigo", available: !!w,
      primary: w ? `${w.present}/${w.total}` : "—", primaryLabel: "employees present", delta: w?.vs_previous, better: "up",
      rows: w ? [
        { label: "Absent", value: w.absent, tone: w.absent ? "bad" : "ok" },
        { label: "Late arrivals", value: w.late, tone: w.late ? "warn" : "ok" },
        { label: "Attendance", value: `${w.attendance_pct}%`, tone: w.attendance_pct >= 90 ? "ok" : w.attendance_pct >= 75 ? "warn" : "bad" },
      ] : [],
    },
    {
      key: "visitors", title: "Visitor Management", icon: Users, tone: "violet", available: !!v,
      primary: v ? v.visitors : "—", primaryLabel: "visitors today", delta: v?.vs_previous, better: null,
      rows: v ? [
        { label: "Inside now", value: v.inside_now ?? "—", tone: "none" },
        { label: "Waiting", value: v.waiting_now ?? "—", tone: waitingOver ? "warn" : "ok", hint: waitingOver ? `${waitingOver} over ${v.waiting_threshold_minutes} min` : null },
        { label: "Avg waiting time", value: mins(v.average_waiting_seconds), tone: "none" },
      ] : [],
    },
    {
      key: "footfall", title: "Footfall & Staffing", icon: Footprints, tone: "emerald", available: !!f || !!b?.areas,
      primary: f ? f.entries : "—", primaryLabel: "entries today", delta: f?.vs_previous, better: null,
      rows: [
        { label: "Employees / visitors", value: f ? `${f.employee_entries} / ${f.visitor_entries}` : "—", tone: "none" },
        { label: "Staff seen now", value: staffNow ?? "—", tone: "none" },
        { label: "Short-staffed areas", value: short ? short.length : "—", tone: short ? (short.length ? "bad" : "ok") : "none" },
      ],
    },
    {
      key: "security", title: "Security & Safety", icon: ShieldAlert, tone: "rose", available: !!s,
      primary: s ? s.active : "—", primaryLabel: "active alerts", delta: s ? { ...s.vs_previous, label: "raised" } : null, better: "down",
      rows: s ? [
        { label: "Unknown persons", value: s.unknown_persons, tone: s.unknown_persons ? "warn" : "ok" },
        // Intrusion = restricted-area / entry-gate alerts; loitering = stayed too long (intrusion.py).
        { label: "Intrusion", value: s.intrusion == null ? "Not set up" : s.restricted_violations, tone: s.intrusion == null ? "none" : s.restricted_violations ? "bad" : "ok" },
        { label: "Loitering", value: s.loitering ?? "Not set up", tone: s.loitering == null ? "none" : s.loitering ? "warn" : "ok" },
      ] : [],
    },
  ];

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end gap-3">
        <div className="mr-auto">
          <h2 className="text-lg font-semibold text-ink-900">Dashboard</h2>
          <p className="text-sm text-slate-500">
            {new Date(`${date}T12:00:00`).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" })}
            {!isToday && " · past day: live figures (inside now, waiting, staffing) are not shown"}
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm text-slate-500">
          <CalendarDays size={16} />
          <input type="date" value={date} max={todayIso()} onChange={(e) => e.target.value && setDate(e.target.value)} className="input-field !w-auto text-sm" />
        </label>
        {!isToday && (
          <button type="button" className="btn-secondary text-sm" onClick={() => setDate(todayIso())}>
            Today
          </button>
        )}
      </div>

      {error && <p className="text-sm text-danger-500">{error}</p>}

      {/* The four questions. One row on desktop; swipe sideways on a phone. */}
      <div className="flex lg:grid lg:grid-cols-4 gap-4 overflow-x-auto lg:overflow-visible snap-x snap-mandatory pb-1 -mx-1 px-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
        {cards.map((c) => (
          <KpiCard key={c.key} card={c} loading={!b} onOpen={() => setOpen(c.key)} />
        ))}
      </div>

      {b && <ActivityOverview f={f} />}

      {b && (
        <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-5">
          <h3 className="text-sm font-semibold text-ink-900 flex items-center gap-2 mb-3">
            <ClipboardList size={16} className="text-brand-600" /> Daily management summary
          </h3>
          <ul className="space-y-2">
            {b.summary.map((x, i) => (
              <li key={i} className="flex items-start gap-3 text-sm">
                <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${DOT[x.tone] || DOT.none}`} />
                <span className="w-24 shrink-0 text-slate-500">{x.area}</span>
                <span className="text-ink-900 flex-1">{x.text}</span>
                <RouterLink to={x.link} className="text-xs text-brand-600 shrink-0 hover:underline">Open</RouterLink>
              </li>
            ))}
          </ul>
          {data?.cameras && (
            <p className="text-[11px] text-slate-400 mt-4">
              Cameras: {data.cameras.online} of {data.cameras.total} online{data.cameras.offline ? `, ${data.cameras.offline} offline` : ""}.
            </p>
          )}
        </section>
      )}

      {b && open === "workforce" && <WorkforceDetail b={b} date={date} onClose={() => setOpen(null)} />}
      {b && open === "visitors" && <VisitorsDetail b={b} onClose={() => setOpen(null)} />}
      {b && open === "footfall" && <FootfallDetail b={b} onClose={() => setOpen(null)} onChanged={load} />}
      {b && open === "security" && <SecurityDetail b={b} onClose={() => setOpen(null)} />}
    </div>
  );
}

function KpiCard({ card, loading, onOpen }) {
  const t = CARD_TONE[card.tone];
  return (
    <button
      type="button"
      onClick={onOpen}
      className="group relative text-left bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.06)] hover:shadow-[0_6px_20px_rgba(15,23,42,0.08)] hover:-translate-y-0.5 transition p-5 overflow-hidden min-w-[82%] sm:min-w-[46%] lg:min-w-0 snap-start focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/40"
    >
      <span className={`absolute inset-x-0 top-0 h-1 bg-gradient-to-r ${t.bar} to-transparent`} />
      <div className="flex items-center gap-2.5">
        <span className={`w-9 h-9 rounded-xl grid place-items-center ${t.icon}`}>
          <card.icon size={18} />
        </span>
        <h3 className="text-sm font-semibold text-ink-900 flex-1">{card.title}</h3>
        <span className="w-7 h-7 rounded-full grid place-items-center bg-slate-50 text-slate-400 group-hover:bg-brand-50 group-hover:text-brand-600 transition" aria-hidden="true">
          <ChevronRight size={15} />
        </span>
      </div>
      <div className="mt-4 flex items-end gap-2">
        <span className="text-[34px] leading-none font-light tracking-tight text-ink-900 tabular-nums">{loading ? "…" : card.primary}</span>
        <Delta delta={card.delta} better={card.better} />
      </div>
      <p className="text-xs text-slate-500 mt-1">{card.primaryLabel}</p>
      {!loading && !card.available ? (
        <p className="text-xs text-slate-400 mt-4">Not set up in this installation.</p>
      ) : (
        <ul className="mt-4 space-y-2 border-t border-slate-100 pt-3">
          {card.rows.map((r) => (
            <li key={r.label} className="flex items-center gap-2 text-xs">
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${DOT[r.tone] || DOT.none}`} />
              <span className="text-slate-500 flex-1 min-w-0 truncate">
                {r.label}
                {r.hint && <span className="text-amber-600"> · {r.hint}</span>}
              </span>
              <span className="font-semibold text-ink-900 tabular-nums">{loading ? "…" : r.value}</span>
            </li>
          ))}
        </ul>
      )}
    </button>
  );
}

// Today vs the previous day, only when both are recorded.
function Delta({ delta, better }) {
  if (!delta || delta.change === undefined || delta.change === null) return null;
  const c = delta.change;
  const Icon = c > 0 ? ArrowUpRight : c < 0 ? ArrowDownRight : Minus;
  const good = better ? (better === "up" ? c > 0 : c < 0) : null;
  const cls = c === 0 || good === null ? "bg-slate-100 text-slate-600" : good ? "bg-emerald-50 text-emerald-700" : "bg-rose-50 text-rose-600";
  return (
    <span className={`mb-1 inline-flex items-center gap-0.5 rounded-full px-2 py-0.5 text-[11px] font-medium ${cls}`} title={`${delta.label ? `${delta.label}: ` : ""}previous day ${delta.previous}`}>
      <Icon size={12} />
      {c > 0 ? `+${c}` : c} vs yesterday
    </span>
  );
}

function ActivityOverview({ f }) {
  return (
    <section className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-5">
      <div className="flex flex-wrap items-center gap-2 mb-3">
        <h3 className="text-sm font-semibold text-ink-900 flex items-center gap-2 mr-auto">
          <TrendingUp size={16} className="text-brand-600" /> Overview
        </h3>
        {f?.peak_label && <span className="text-xs text-slate-500">Peak hour: <b className="text-ink-900">{f.peak_label}</b></span>}
      </div>
      {!f ? (
        <p className="text-sm text-slate-400 py-10 text-center">Footfall is not set up, so there is no activity data.</p>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
          <div className="lg:col-span-3">
            <p className="text-xs text-slate-500 mb-1">Entries by hour</p>
            <div className="h-56">
              {f.hourly.length === 0 ? (
                <p className="text-sm text-slate-400 pt-20 text-center">Nobody counted on this day.</p>
              ) : (
                <ResponsiveContainer width="100%" height="100%">
                  <BarChart data={f.hourly}>
                    <CartesianGrid vertical={false} stroke="#eef0f5" />
                    <XAxis dataKey="hour" tickFormatter={(h) => `${h % 12 || 12}${h < 12 ? "a" : "p"}`} tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} />
                    <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} width={26} />
                    <Tooltip labelFormatter={(h) => f.hourly.find((x) => x.hour === h)?.label} contentStyle={{ fontSize: 12, borderRadius: 8 }} cursor={{ fill: "#f8fafc" }} />
                    <Legend wrapperStyle={{ fontSize: 11 }} />
                    <Bar dataKey="employees" name="Employees" stackId="a" fill="#818cf8" maxBarSize={28} />
                    <Bar dataKey="visitors" name="Visitors / others" stackId="a" fill="#34d399" radius={[4, 4, 0, 0]} maxBarSize={28} />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </div>
          </div>
          <div className="lg:col-span-2">
            <p className="text-xs text-slate-500 mb-1">Employee vs visitor traffic, last 7 days</p>
            <div className="h-56">
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={f.trend}>
                  <CartesianGrid vertical={false} stroke="#eef0f5" />
                  <XAxis dataKey="day" tickFormatter={(d) => new Date(`${d}T12:00:00`).toLocaleDateString([], { weekday: "short" })} tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} width={26} />
                  <Tooltip labelFormatter={(d) => new Date(`${d}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short" })} contentStyle={{ fontSize: 12, borderRadius: 8 }} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Line type="monotone" dataKey="employees" name="Employees" stroke="#818cf8" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="visitors" name="Visitors / others" stroke="#34d399" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
