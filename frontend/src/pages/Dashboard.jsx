import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ResponsiveContainer, BarChart, Bar, XAxis, YAxis, Tooltip, CartesianGrid } from "recharts";
import {
  AlertTriangle,
  Building2,
  CalendarDays,
  ClipboardList,
  Footprints,
  Siren,
  Sparkles,
  UserCheck,
  Users,
} from "lucide-react";
import * as api from "../api/client";
import { useAuth } from "../context/AuthContext";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";

// Business dashboard: the day at a glance for the owner, from the same
// backend calculations the feature pages use (backend/app/insights.py
// business_overview). Every number is recorded data; "—" means not available
// (feature off, live-only figure on a past date, or nothing configured).

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

function minutes(seconds) {
  if (seconds == null) return "—";
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function hourShort(h) {
  const suffix = h < 12 ? "am" : "pm";
  return `${h % 12 === 0 ? 12 : h % 12}${suffix}`;
}

const TONE = { ok: "text-success-600", warn: "text-warning-600", bad: "text-danger-500" };
const DOT = { ok: "bg-success-500", warn: "bg-warning-500", bad: "bg-danger-500" };

function KpiCard({ label, value, sub, onClick, tone }) {
  return (
    <button type="button" onClick={onClick} className="stat-card text-left hover:ring-2 hover:ring-brand-500/30 transition-shadow w-full">
      <p className={`text-2xl font-semibold leading-none ${tone === "bad" ? "text-danger-500" : "text-ink-900"}`}>{value ?? "—"}</p>
      <p className="text-sm text-slate-500 mt-2">{label}</p>
      {sub ? <p className="text-xs mt-2 text-slate-400">{sub}</p> : null}
    </button>
  );
}

function Section({ icon: Icon, title, action, children }) {
  return (
    <div className="card p-5">
      <div className="flex items-center justify-between gap-2 mb-4">
        <h3 className="font-semibold text-ink-900 flex items-center gap-2">
          {Icon && <Icon size={16} className="text-brand-600" />} {title}
        </h3>
        {action}
      </div>
      {children}
    </div>
  );
}

function Figures({ items }) {
  return (
    <dl className="grid grid-cols-2 sm:grid-cols-3 gap-x-4 gap-y-3 mb-4">
      {items.map(([label, value, tone]) => (
        <div key={label}>
          <dt className="text-xs text-slate-500">{label}</dt>
          <dd className={`text-lg font-semibold ${tone === "bad" && value && value !== "0" ? "text-danger-500" : "text-ink-900"}`}>{value ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

function HourBars({ data, dataKey, name }) {
  if (!data?.length) return <p className="text-xs text-slate-400">No arrivals recorded.</p>;
  const rows = data.map((r) => ({ ...r, label: hourShort(r.hour) }));
  return (
    <div className="h-40">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows}>
          <CartesianGrid vertical={false} stroke="#eceef4" />
          <XAxis dataKey="label" tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} />
          <YAxis hide allowDecimals={false} />
          <Tooltip />
          <Bar dataKey={dataKey} name={name} fill="var(--color-brand-500)" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

function Link({ to, children }) {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate(to)} className="text-xs text-brand-600 hover:underline">
      {children}
    </button>
  );
}

export default function Dashboard() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const [date, setDate] = useState(todayIso);
  const [stats, setStats] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let alive = true;
    const load = () =>
      api
        .getDashboardStats(date)
        .then((s) => {
          if (!alive) return;
          setStats(s);
          setError("");
        })
        .catch(() => alive && setError("Couldn't load the dashboard. Check the backend is running on port 8822."));
    load();
    // Live figures refresh for today; a past date doesn't change.
    const t = date === todayIso() ? setVisibleInterval(load, 30000) : null;
    return () => {
      alive = false;
      if (t) clearVisibleInterval(t);
    };
  }, [date]);

  if (error && !stats) return <p className="text-sm text-danger-500">{error}</p>;
  if (!stats) return <p className="text-sm text-slate-400">Loading dashboard…</p>;

  const b = stats.business || {};
  const a = b.attendance;
  const v = b.visitors;
  const f = b.footfall;
  const st = b.staff;
  const isToday = b.is_today ?? date === todayIso();
  const q = `?date=${date}`;
  const gaps = b.staffing_gaps;

  const kpis = [
    a && { label: "Employees present", value: `${a.present} / ${a.total}`, sub: `${a.attendance_pct}% · ${a.absent} absent`, to: `/attendance${q}` },
    a && { label: "Late arrivals", value: String(a.late), sub: `after ${a.late_after}`, to: `/attendance${q}`, tone: a.late ? "bad" : undefined },
    v && { label: "Visitors", value: String(v.visitors), sub: isToday ? `${v.waiting_now} waiting now` : "that day", to: "/visitors" },
    v && { label: "Average visitor wait", value: v.visitors ? minutes(v.average_wait_seconds) : "—", sub: `${v.over_threshold} over ${v.threshold_minutes} min`, to: "/visitors" },
    f && {
      label: "Footfall (inward entries)",
      value: String(f.entries),
      sub: f.change_pct == null ? "no data the day before" : `${f.change_pct >= 0 ? "+" : ""}${f.change_pct}% vs day before`,
      to: "/footfall",
    },
    f && { label: "Employee vs visitor traffic", value: `${f.employee_entries} / ${f.visitor_entries}`, sub: "employees / visitors", to: "/footfall" },
    st && {
      label: isToday ? "Current staff count" : "Peak staff inside",
      value: isToday ? String(st.inside_now) : String(st.peak_inside),
      sub: `${st.entries} in · ${st.exits} out`,
      to: "/staff",
    },
    {
      label: "Areas with staffing gaps",
      value: gaps == null ? "—" : String(gaps.length),
      sub: gaps == null ? "live figure (today only)" : gaps.length ? gaps[0].area : "every monitored area covered",
      to: "/visitors",
      tone: gaps?.length ? "bad" : undefined,
    },
  ].filter(Boolean);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 className="text-lg font-semibold text-ink-900">Welcome, {user?.name?.split(" ")[0] || "there"}</h2>
          <p className="text-sm text-slate-500">
            {new Date(`${date}T12:00:00`).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" })}
            {isToday ? " · live" : ""}
          </p>
        </div>
        <div className="flex items-center gap-2">
          <CalendarDays size={16} className="text-slate-400" />
          <input type="date" value={date} max={todayIso()} onChange={(e) => e.target.value && setDate(e.target.value)} className="input-field !w-auto text-sm" />
          {!isToday && (
            <button type="button" className="btn-secondary text-sm" onClick={() => setDate(todayIso())}>
              Today
            </button>
          )}
        </div>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {kpis.map((k) => (
          <KpiCard key={k.label} {...k} onClick={() => navigate(k.to)} />
        ))}
      </div>

      {b.summary?.length > 0 && (
        <Section icon={ClipboardList} title="Daily management summary">
          <ul className="space-y-2">
            {b.summary.map((s, i) => (
              <li key={i}>
                <button type="button" onClick={() => navigate(s.link)} className="w-full text-left flex items-start gap-3 border border-[#f1f2f7] rounded-xl px-4 py-2.5 hover:bg-slate-50">
                  <span className={`mt-1.5 w-2 h-2 rounded-full shrink-0 ${DOT[s.tone] || DOT.ok}`} />
                  <span className="text-sm text-slate-700">
                    <span className={`font-medium ${TONE[s.tone] || ""}`}>{s.area}: </span>
                    {s.text}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <div className="grid lg:grid-cols-2 gap-5">
        {a && (
          <Section icon={UserCheck} title="Attendance overview" action={<Link to={`/attendance${q}`}>Open attendance</Link>}>
            <Figures
              items={[
                ["Present", String(a.present)],
                ["Absent", String(a.absent)],
                ["Late", String(a.late), "bad"],
                [`Late ${a.late_repeat_threshold}+ times (30 days)`, String(a.late_repeat), "bad"],
                [`Early departures (before ${a.shift_end})`, String(a.early_departures), "bad"],
                ["Overtime", a.overtime ? `${a.overtime} · ${minutes(a.overtime_minutes * 60)}` : "0"],
              ]}
            />
            <p className="text-xs font-medium text-slate-500 mb-2">Policy exceptions</p>
            {a.exceptions.length === 0 ? (
              <p className="text-sm text-slate-400">No attendance exceptions.</p>
            ) : (
              <ul className="space-y-1.5 max-h-56 overflow-y-auto pr-1">
                {a.exceptions.map((e) => (
                  <li key={e.employee_id} className="text-sm flex flex-wrap gap-x-2">
                    <span className="font-medium text-ink-900">{e.name}</span>
                    <span className="text-slate-500">{e.reasons.join(" · ")}</span>
                  </li>
                ))}
              </ul>
            )}
          </Section>
        )}

        {v && (
          <Section icon={Users} title="Visitor overview" action={<Link to="/visitors">Open visitor management</Link>}>
            <Figures
              items={[
                ["Visitors", String(v.visitors)],
                ["Average wait", v.visitors ? minutes(v.average_wait_seconds) : "—"],
                [`Waiting over ${v.threshold_minutes} min`, String(v.over_threshold), "bad"],
                ["Peak visitor hour", v.peak_label || "—"],
                ["Visitor alerts", String(v.alerts)],
                ["Repeat visitors", "Not tracked"],
              ]}
            />
            <HourBars data={v.hourly} dataKey="visitors" name="Visitors" />
            <p className="text-[11px] text-slate-400 mt-2">
              Repeat visitors can't be told apart: visitors aren't identified, only employees are. Returning people at the gates are on Footfall.
            </p>
          </Section>
        )}

        {f && (
          <Section icon={Footprints} title="Footfall analytics" action={<Link to="/footfall">Open footfall</Link>}>
            <Figures
              items={[
                ["Inward entries", String(f.entries)],
                ["Outward exits", String(f.exits)],
                ["Employee entries", String(f.employee_entries)],
                ["Visitor entries", String(f.visitor_entries)],
                ["Peak entry hour", f.peak_label || "—"],
                ["Day before", String(f.previous_day_entries)],
              ]}
            />
            <HourBars data={f.hourly} dataKey="entries" name="Entries" />
            {f.gates_without_entry_line > 0 && (
              <p className="text-[11px] text-warning-600 mt-2">
                {f.gates_without_entry_line} gate(s) have no entry line: they count everyone in their area and can't count exits. Draw the line on Footfall.
              </p>
            )}
          </Section>
        )}

        <Section icon={Building2} title="Staff & zone monitoring" action={<Link to="/staff">Open staff counting</Link>}>
          {st ? (
            <Figures
              items={[
                ["Inside now", isToday ? String(st.inside_now) : "—"],
                ["Employees inside", isToday ? String(st.employees_inside) : "—"],
                ["Other persons", isToday ? String(st.others_inside) : "—"],
                ["Peak inside", String(st.peak_inside)],
                ["Entries", String(st.entries)],
                ["Exits", String(st.exits)],
              ]}
            />
          ) : (
            <p className="text-sm text-slate-400 mb-4">Staff counting is not running.</p>
          )}
          <p className="text-xs font-medium text-slate-500 mb-2">Zone-wise occupancy {isToday ? "(live)" : ""}</p>
          {!isToday ? (
            <p className="text-sm text-slate-400 mb-3">Zone occupancy is live only. Pick today to see it.</p>
          ) : b.zones?.length ? (
            <table className="data-table mb-3">
              <thead>
                <tr>
                  <th>Area</th>
                  <th>Visitors</th>
                  <th>Employees</th>
                  <th>Staffing</th>
                </tr>
              </thead>
              <tbody>
                {b.zones.map((z) => {
                  const gap = z.visitors > 0 && z.employees === 0;
                  return (
                    <tr key={z.camera_id}>
                      <td>{z.area}</td>
                      <td>{z.visitors}</td>
                      <td>{z.employees}</td>
                      <td>
                        <span className={`badge ${gap ? "badge-danger" : "badge-success"}`}>{gap ? "No employee present" : z.online ? "Covered" : "Camera offline"}</span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : (
            <p className="text-sm text-slate-400 mb-3">No monitored areas. Mark a visitor area on Visitor Management.</p>
          )}
          {a && (
            <>
              <p className="text-xs font-medium text-slate-500 mb-2">Employee presence (where last seen)</p>
              {a.presence.length === 0 ? (
                <p className="text-sm text-slate-400">No employees seen.</p>
              ) : (
                <ul className="text-sm space-y-1 max-h-40 overflow-y-auto pr-1">
                  {a.presence.map((p) => (
                    <li key={p.name} className="flex justify-between gap-2">
                      <span className="text-ink-900">{p.name}</span>
                      <span className="text-slate-500">
                        {p.camera} · {p.last_seen}
                        {p.status === "On site" ? " · on site" : ""}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </Section>
      </div>

      <div className="grid md:grid-cols-2 gap-5">
        <Section icon={Siren} title={`Important events${b.open_alerts ? ` (${b.open_alerts} open)` : ""}`} action={<Link to="/alerts">Open alerts</Link>}>
          {!b.events?.length ? (
            <p className="text-sm text-slate-500">No open alerts for this day.</p>
          ) : (
            <div className="space-y-2">
              {b.events.map((e) => (
                <button key={e.id} type="button" onClick={() => navigate("/alerts")} className="w-full text-left flex items-center justify-between border border-[#f1f2f7] rounded-xl px-4 py-2.5 hover:bg-slate-50">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-ink-900 truncate">{e.event}</p>
                    <p className="text-xs text-slate-500 truncate">
                      {e.camera} · {e.time}
                      {e.message ? ` · ${e.message}` : ""}
                    </p>
                  </div>
                  <span className={`badge shrink-0 ${e.severity === "Critical" || e.severity === "High" ? "badge-danger" : "badge-warning"}`}>{e.severity}</span>
                </button>
              ))}
            </div>
          )}
        </Section>

        <Section icon={AlertTriangle} title={`System health · ${stats.admin.camerasOnline.value} cameras online`}>
          <div className="space-y-3">
            {stats.needsAttention.length === 0 && <p className="text-sm text-slate-500">Nothing needs attention right now.</p>}
            {stats.needsAttention.map((item, i) => (
              <div key={i} className="flex items-center justify-between border border-[#f1f2f7] rounded-xl px-4 py-3">
                <div>
                  <p className="text-sm font-medium text-ink-900">{item.label}</p>
                  {item.detail && <p className="text-xs text-slate-500 mt-0.5">{item.detail}</p>}
                </div>
                <span className={`badge ${item.tag === "Critical" ? "badge-danger" : "badge-warning"}`}>{item.tag}</span>
              </div>
            ))}
          </div>
          {stats.aiInsights?.length > 0 && (
            <ul className="space-y-2 mt-4">
              {stats.aiInsights.map((line, i) => (
                <li key={i} className="text-xs text-slate-600 flex gap-2">
                  <Sparkles size={13} className="text-brand-500 shrink-0 mt-0.5" /> {line}
                </li>
              ))}
            </ul>
          )}
        </Section>
      </div>
    </div>
  );
}
