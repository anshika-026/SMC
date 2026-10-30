import { useCallback, useEffect, useMemo, useState } from "react";
import { CalendarClock, Search, X } from "lucide-react";
import PageHeader from "../components/PageHeader";
import Modal from "../components/Modal";
import SidePanel from "../components/SidePanel";
import RequestsTab from "../components/workforce/RequestsTab";
import BalancesTab from "../components/workforce/BalancesTab";
import HolidaysPanel from "../components/workforce/HolidaysPanel";
import PolicyPanel from "../components/workforce/PolicyPanel";
import { DayBadge, clock, dur, shiftIso, todayIso } from "../components/workforce/shared";
import InsightCard from "../components/workforce/InsightCard";
import { hr } from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";

// Attendance dashboard: four consolidated KPI cards over the Daily Attendance
// Log. All figures come from the backend's classification of each employee's
// day (backend/app/workforce.py overview()): gate crossings, face sightings,
// holidays, weekly offs, approved leave/WFH and the stored policy. Requests,
// leave balances, holidays and the policy open from the header.

const REFRESH_MS = 30000;
const WORKED = ["Present", "Late", "Half day"];
const STATUSES = ["Present", "Late", "Half day", "Absent", "WFH", "On leave", "Holiday", "Weekly off", "Not in yet"];

// Quick filters a card's sub-metric applies to the log (same rules as the
// backend's KPI_FILTERS, applied to the rows it returned).
const QUICK = {
  inside: { label: "Present inside", test: (r) => r.inside_now },
  outside: { label: "Currently outside", test: (r) => r.outside_now },
  wfh: { label: "Working from home", test: (r) => r.status === "WFH" },
  leave: { label: "On leave", test: (r) => r.status === "On leave" || !!r.half_day_leave },
  late: { label: "Late arrivals", test: (r) => r.late },
  absent: { label: "Absent", test: (r) => r.status === "Absent" },
  half_day: { label: "Half-day", test: (r) => r.half_day },
  late_limit: { label: "Over late-arrival limit", test: (r) => r.over_late_limit },
  half_day_limit: { label: "Over half-day limit", test: (r) => r.over_half_day_limit },
  overtime: { label: "Overtime", test: (r) => r.overtime_minutes > 0 },
  long_outside: { label: "Long time outside", test: (r) => r.long_outside },
  missing: { label: "Missing entry/exit", test: (r) => r.missing_entry || r.missing_exit },
  exceptions: { label: "Attendance exceptions", test: (r) => r.late || r.half_day || r.status === "Absent" },
  compliance: { label: "Policy issues", test: (r) => r.over_late_limit || r.over_half_day_limit || r.overtime_minutes > 0 || r.long_outside },
  present: { label: "Present today", test: (r) => ["Present", "Late", "Half day"].includes(r.status) },
};

// Where someone is right now (today only), from the backend: a confirmed gate
// crossing, or which camera recognised them last.
const LOCATION = {
  inside: { label: "Inside", cls: "badge-success" },
  outside: { label: "Outside", cls: "badge-neutral" },
  likely_outside: { label: "Likely outside", cls: "badge-warning" },
  at_gate: { label: "At the gate", cls: "badge-neutral" },
  unknown: { label: "Not seen recently", cls: "badge-neutral" },
};

function LocationCell({ r }) {
  const l = LOCATION[r.location];
  if (!l) return <span className="text-slate-300">—</span>;
  return (
    <div className="min-w-0">
      <span className={`badge ${l.cls}`}>{l.label}</span>
      {r.location_detail && <span className="block text-[11px] text-slate-400 mt-0.5 max-w-56 leading-snug">{r.location_detail}</span>}
    </div>
  );
}

function rowExceptions(r) {
  const out = [];
  if (r.late) out.push("Late");
  if (r.half_day) out.push(r.half_day_leave ? `Half-day leave (${r.half_day_leave} half)` : "Half day");
  if (r.early_departure) out.push("Left early");
  if (r.overtime_minutes) out.push(`Overtime ${dur(r.overtime_minutes * 60)}`);
  if (r.long_outside) out.push("Long outside");
  if (r.missing_entry) out.push("No gate entry");
  if (r.location === "likely_outside") out.push("Left without a gate exit - verify");
  else if (r.missing_exit) out.push("Exit not confirmed");
  if (r.over_late_limit) out.push(`Late ${r.month.late}× this month`);
  if (r.over_half_day_limit) out.push(`${r.month.half_day} half days this month`);
  return out;
}

// Hours worked: time inside from confirmed gate crossings; for someone only
// seen by face, the span from first to last sighting (shown as approximate).
function workedSeconds(r) {
  if (r.source === "gate") return r.inside_seconds;
  if (r.first_in && r.last_seen) return Math.max(0, r.last_seen - r.first_in);
  return 0;
}

export default function Attendance() {
  const [date, setDate] = useState(() => {
    // Opened from the Dashboard with ?date=YYYY-MM-DD: show that day.
    const q = new URLSearchParams(window.location.search).get("date");
    return q && /^\d{4}-\d{2}-\d{2}$/.test(q) ? q : todayIso();
  });
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [quick, setQuick] = useState("");
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [manage, setManage] = useState(null);   // Requests | Leave balances | Holidays | Policy
  const [selected, setSelected] = useState(null);
  const [history, setHistory] = useState(null);
  const [period, setPeriod] = useState({ workforce: "week", exceptions: "week", compliance: "week", tracking: "week" });
  const [rangeRows, setRangeRows] = useState([]);   // last 30 days, classified (charts)

  const load = useCallback(() => {
    hr.overview({ date })
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(e.message));
  }, [date]);

  useEffect(() => {
    load();
    if (date !== todayIso()) return undefined;
    const t = setVisibleInterval(load, REFRESH_MS);
    return () => clearVisibleInterval(t);
  }, [load, date]);

  // The charts: every employee's last 30 days, from the same classification.
  useEffect(() => {
    hr.range({ from: shiftIso(date, -29), to: date })
      .then((d) => setRangeRows(d.rows))
      .catch(() => setRangeRows([]));
  }, [date]);

  // The selected employee's last 14 days, from the same classification.
  useEffect(() => {
    if (!selected) return;
    setHistory(null);
    hr.range({ from: shiftIso(date, -13), to: date, employeeId: selected.employee_id })
      .then((d) => setHistory([...d.rows].reverse()))
      .catch(() => setHistory([]));
  }, [selected, date]);

  const rows = useMemo(() => data?.rows || [], [data]);
  const c = useMemo(() => Object.fromEntries((data?.cards || []).map((x) => [x.key, x])), [data]);
  const count = (key) => c[key]?.count ?? 0;
  const presentToday = rows.filter(QUICK.present.test).length;
  const exceptions = rows.filter(QUICK.exceptions.test).length;
  const compliance = rows.filter(QUICK.compliance.test).length;
  const nextHoliday = c.holidays?.next;
  const outsideConfirmed = rows.filter((r) => r.outside_confirmed).length;
  // Names for the Workforce card (hover a name for where/why).
  const people = (test, warn) =>
    rows.filter(test).map((r) => ({ name: r.name, hint: r.location_detail || r.status, warn: warn ? warn(r) : false }));

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    return rows.filter(
      (r) =>
        (!status || r.status === status) &&
        (!quick || QUICK[quick].test(r)) &&
        (!q || r.name.toLowerCase().includes(q) || String(r.employee_id).toLowerCase().includes(q))
    );
  }, [rows, status, quick, search]);

  const toggleQuick = (key) => setQuick((k) => (k === key ? "" : key));
  // A card's arrow: show exactly those employees in the log.
  const openFilter = (key) => {
    setQuick(key);
    document.getElementById("attendance-log")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  // Daily counts for the last 7 or 30 days ending at the chosen date.
  const inDept = useMemo(() => new Set(rows.map((r) => r.employee_id)), [rows]);
  const series = (test, p) => {
    const days = p === "month" ? 30 : 7;
    return Array.from({ length: days }, (_, k) => {
      const day = shiftIso(date, k - days + 1);
      return { day, v: rangeRows.filter((r) => r.day === day && inDept.has(r.employee_id) && test(r)).length };
    });
  };
  const pctText = (n, total) => (total ? ` (${Math.round((n / total) * 100)}%)` : "");
  const filtersOn = status || quick || search;
  const pending = count("pending");

  const MANAGE = ["Requests", "Leave balances", "Holidays", "Policy"];

  return (
    <div className="space-y-5">
      <PageHeader
        title="Attendance"
        action={
          <div className="flex flex-wrap gap-2">
            {MANAGE.map((m) => (
              <button key={m} type="button" onClick={() => setManage(m)} className="btn-secondary text-sm inline-flex items-center gap-1.5">
                {m}
                {m === "Requests" && pending > 0 && <span className="badge badge-warning !px-1.5 !py-0">{pending}</span>}
              </button>
            ))}
          </div>
        }
      />

      {error && <p className="text-sm text-danger-500">{error}</p>}

      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 items-stretch">
        <InsightCard
          title="Workforce Status"
          tone="orange"
          period={period.workforce}
          onPeriod={(p) => setPeriod((x) => ({ ...x, workforce: p }))}
          metrics={[
            {
              key: "present",
              label: "Present today",
              value: presentToday,
              display: data ? `${presentToday}/${count("total")}` : null,
              series: series((r) => WORKED.includes(r.status), period.workforce),
              better: "up",
              insight: data ? `${presentToday} of ${count("total")} employees came in${pctText(presentToday, count("total"))}.` : "Loading…",
              onOpen: () => openFilter("present"),
            },
            {
              key: "inside",
              label: "Present inside now",
              value: count("inside"),
              people: people((r) => r.inside_now),
              total: count("total"),
              insight: `${count("inside")} employee${count("inside") === 1 ? " is" : "s are"} inside the premises right now.`,
              onOpen: () => openFilter("inside"),
            },
            {
              key: "outside",
              label: "Currently outside",
              value: count("outside"),
              people: people((r) => r.outside_now, (r) => r.location === "likely_outside"),
              total: Math.max(presentToday, 1),
              insight: count("outside")
                ? `${outsideConfirmed} left through the gate${count("outside") - outsideConfirmed ? `, ${count("outside") - outsideConfirmed} likely outside (last seen at the gate) - verify` : ""}.`
                : "Nobody who came in is outside right now.",
              onOpen: () => openFilter("outside"),
            },
            {
              key: "wfh",
              label: "Working from home",
              value: count("wfh"),
              people: people((r) => r.status === "WFH"),
              series: series((r) => r.status === "WFH", period.workforce),
              insight: `${count("wfh")} on approved work from home today.`,
              onOpen: () => openFilter("wfh"),
            },
            {
              key: "leave",
              label: "On leave",
              value: count("leave"),
              people: people((r) => r.status === "On leave" || !!r.half_day_leave),
              series: series((r) => r.status === "On leave" || !!r.half_day_leave, period.workforce),
              insight: `${count("leave")} on approved leave today (full or half day).`,
              onOpen: () => openFilter("leave"),
            },
          ]}
        />
        <InsightCard
          title="Attendance Exceptions"
          tone="blue"
          period={period.exceptions}
          onPeriod={(p) => setPeriod((x) => ({ ...x, exceptions: p }))}
          metrics={[
            {
              key: "exceptions",
              label: "Late, absent or half day",
              value: exceptions,
              series: series((r) => r.late || r.half_day || r.status === "Absent", period.exceptions),
              curve: "basis",
              better: "down",
              insight: data ? `${count("late")} late, ${count("absent")} absent and ${count("half_day")} on a half day.` : "Loading…",
              onOpen: () => openFilter("exceptions"),
            },
            {
              key: "late",
              label: "Late arrivals",
              value: count("late"),
              series: series((r) => r.late, period.exceptions),
              curve: "basis",
              better: "down",
              insight: `${count("late")} arrived after the ${data?.policy.grace_minutes ?? 0}-minute grace period.`,
              onOpen: () => openFilter("late"),
            },
            {
              key: "absent",
              label: "Absent",
              value: count("absent"),
              series: series((r) => r.status === "Absent", period.exceptions),
              curve: "basis",
              better: "down",
              insight: `${count("absent")} not seen and not on approved leave or WFH.`,
              onOpen: () => openFilter("absent"),
            },
            {
              key: "half_day",
              label: "Half-day",
              value: count("half_day"),
              series: series((r) => r.half_day, period.exceptions),
              curve: "basis",
              better: "down",
              insight: `${count("half_day")} came in after ${data?.policy.half_day_after ?? "the cut-off"} or took half-day leave.`,
              onOpen: () => openFilter("half_day"),
            },
          ]}
        />
        <InsightCard
          title="Policy Compliance"
          tone="slate"
          period={period.compliance}
          onPeriod={(p) => setPeriod((x) => ({ ...x, compliance: p }))}
          metrics={[
            {
              key: "compliance",
              label: "Employees outside policy",
              value: compliance,
              total: count("total"),
              insight: `${compliance} of ${count("total")} employees broke a time or attendance rule this month.`,
              onOpen: () => openFilter("compliance"),
            },
            {
              key: "late_limit",
              label: "Over late-arrival limit",
              value: count("late_limit"),
              total: count("total"),
              insight: `Late more than ${data?.policy.late_allowance_per_month ?? 0} times this month.`,
              onOpen: () => openFilter("late_limit"),
            },
            {
              key: "half_day_limit",
              label: "Over half-day limit",
              value: count("half_day_limit"),
              total: count("total"),
              insight: `More than ${data?.policy.half_day_allowance_per_month ?? 0} half days this month.`,
              onOpen: () => openFilter("half_day_limit"),
            },
            {
              key: "overtime",
              label: "Overtime",
              value: count("overtime"),
              series: series((r) => r.overtime_minutes > 0, period.compliance),
              insight: `${count("overtime")} stayed past ${data?.policy.shift_end ?? "the shift end"} (+${data?.policy.overtime_after_minutes ?? 0} min).`,
              onOpen: () => openFilter("overtime"),
            },
            {
              key: "long_outside",
              label: "Long time outside",
              value: count("long_outside"),
              series: series((r) => r.long_outside, period.compliance),
              better: "down",
              insight: `Outside for more than ${data?.policy.long_outside_minutes ?? 0} minutes at a time.`,
              onOpen: () => openFilter("long_outside"),
            },
          ]}
        />
        <InsightCard
          title="Tracking & Requests"
          tone="teal"
          period={period.tracking}
          onPeriod={(p) => setPeriod((x) => ({ ...x, tracking: p }))}
          metrics={[
            {
              key: "missing",
              label: "Missing entry/exit",
              value: count("missing"),
              series: series((r) => r.missing_entry || r.missing_exit, period.tracking),
              better: "down",
              insight: `${count("missing")} record${count("missing") === 1 ? "" : "s"} to check: seen by face but not at the gate, or exit not confirmed.`,
              onOpen: () => openFilter("missing"),
            },
            {
              key: "pending",
              label: "Pending leave/WFH requests",
              value: pending,
              total: Math.max(pending, 1),
              insight: pending ? `${pending} request${pending === 1 ? "" : "s"} waiting for a decision.` : "No requests waiting for a decision.",
              onOpen: () => setManage("Requests"),
            },
            {
              key: "holidays",
              label: "Upcoming holidays",
              value: count("holidays"),
              total: Math.max(count("holidays"), 1),
              insight: nextHoliday
                ? `Next: ${nextHoliday.name}, ${new Date(`${nextHoliday.day}T12:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" })}.`
                : "No holidays in the next 60 days.",
              onOpen: () => setManage("Holidays"),
            },
          ]}
        />
      </div>

      <div className="card" id="attendance-log">
        <div className="flex flex-wrap items-center gap-2 p-4 border-b border-[#f1f2f7]">
          <h3 className="text-sm font-semibold text-ink-900 mr-auto flex items-center gap-2">
            <CalendarClock size={16} className="text-brand-600" /> Daily attendance log
            <span className="text-xs font-normal text-slate-400">
              {shown.length} of {rows.length}
            </span>
          </h3>
          <input type="date" value={date} max={todayIso()} onChange={(e) => e.target.value && setDate(e.target.value)} className="input-field !w-auto !py-1.5 text-sm" />
          <select value={status} onChange={(e) => setStatus(e.target.value)} className="input-field !w-auto !py-1.5 text-sm">
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          <label className="relative">
            <Search size={14} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search name or ID" className="input-field !w-48 !py-1.5 !pl-8 text-sm" />
          </label>
        </div>

        {filtersOn && (
          <div className="flex flex-wrap items-center gap-2 px-4 pt-3 text-xs">
            {quick && <span className="badge badge-neutral">{QUICK[quick].label}</span>}
            {status && <span className="badge badge-neutral">Status: {status}</span>}
            {search && <span className="badge badge-neutral">“{search}”</span>}
            <button
              type="button"
              className="text-brand-600 inline-flex items-center gap-1"
              onClick={() => {
                setQuick("");
                setStatus("");
                setSearch("");
              }}
            >
              <X size={12} /> Clear filters
            </button>
          </div>
        )}

        <div className="overflow-x-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Employee</th>
                <th>Entry</th>
                <th>Exit</th>
                <th>Location</th>
                <th>Working hours</th>
                <th>Status</th>
                <th>Exceptions</th>
              </tr>
            </thead>
            <tbody>
              {!data && !error && (
                <tr>
                  <td colSpan={7} className="text-center text-slate-400 py-10">
                    Loading attendance…
                  </td>
                </tr>
              )}
              {data && shown.length === 0 && (
                <tr>
                  <td colSpan={7} className="text-center text-slate-400 py-10">
                    No employees match these filters.
                  </td>
                </tr>
              )}
              {shown.map((r) => {
                const ex = rowExceptions(r);
                const worked = workedSeconds(r);
                return (
                  <tr key={r.employee_id} className="cursor-pointer" onClick={() => setSelected(r)}>
                    <td>
                      <span className="font-medium text-ink-900">{r.name}</span>
                      <span className="block text-[11px] text-slate-400">ID {r.employee_id}</span>
                    </td>
                    <td className="tabular-nums">{clock(r.first_in)}</td>
                    <td className="tabular-nums">{r.inside_now ? "—" : clock(r.last_out)}</td>
                    <td>
                      <LocationCell r={r} />
                    </td>
                    <td className="tabular-nums">
                      {worked ? dur(worked) : "—"}
                      {worked > 0 && r.source === "face" && <span className="text-[11px] text-slate-400"> (approx.)</span>}
                    </td>
                    <td>
                      <DayBadge status={r.status} />
                      {r.holiday && <span className="block text-[11px] text-slate-400 mt-0.5">{r.holiday}</span>}
                    </td>
                    <td>
                      {ex.length === 0 ? (
                        <span className="text-slate-300">—</span>
                      ) : (
                        <div className="flex flex-wrap gap-1">
                          {ex.map((e) => (
                            <span key={e} className="text-[11px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 whitespace-nowrap">
                              {e}
                            </span>
                          ))}
                        </div>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {data && (
          <p className="text-[11px] text-slate-400 px-4 py-3 border-t border-[#f1f2f7]">
            Shift {data.policy.shift_start}–{data.policy.shift_end}, late after {data.policy.grace_minutes} min grace, half day after{" "}
            {data.policy.half_day_after}. Times come from confirmed main-gate crossings; employees only seen by face show approximate hours
            and a “No gate entry” note.
          </p>
        )}
      </div>

      <SidePanel open={!!selected} onClose={() => setSelected(null)} title={selected ? `${selected.name} · ${date}` : ""}>
        {selected && (
          <div className="space-y-5 text-sm">
            <div className="flex items-center gap-2">
              <DayBadge status={selected.status} />
              <span className="text-slate-500">
                ID {selected.employee_id}
                {selected.leave_type ? ` · ${selected.leave_type}` : ""}
              </span>
            </div>
            <div>
              <p className="text-xs font-semibold text-slate-500 mb-2">Entry / exit timeline</p>
              {selected.sessions.length === 0 ? (
                <p className="text-slate-400">No gate crossings{selected.source === "face" ? " (seen by face only)" : ""}.</p>
              ) : (
                <ol className="space-y-1.5">
                  {selected.sessions.map((s, i) => (
                    <li key={i} className="flex justify-between gap-3">
                      <span>
                        In {clock(s.entry)} → {s.inside ? "still inside" : `out ${clock(s.exit)}`}
                      </span>
                      <span className="text-xs text-slate-400">
                        {s.exit && !s.exit_confirmed ? "exit not confirmed" : dur((s.exit || Date.now() / 1000) - s.entry)}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </div>
            <dl className="grid grid-cols-2 gap-y-1.5">
              <dt className="text-slate-500">Time inside</dt>
              <dd>{dur(selected.inside_seconds)}</dd>
              <dt className="text-slate-500">Time outside</dt>
              <dd>
                {dur(selected.outside_seconds)}
                {selected.outside_periods ? ` (${selected.outside_periods}×)` : ""}
              </dd>
              <dt className="text-slate-500">This month</dt>
              <dd>
                {selected.month.late} late · {selected.month.half_day} half · {selected.month.absent} absent · {selected.month.wfh} WFH
              </dd>
            </dl>
            <div>
              <p className="text-xs font-semibold text-slate-500 mb-2">Last 14 days</p>
              {!history ? (
                <p className="text-slate-400">Loading…</p>
              ) : (
                <ul className="divide-y divide-[#f1f2f7]">
                  {history.map((h) => (
                    <li key={h.day} className="flex items-center justify-between py-1.5">
                      <span className="text-slate-600">
                        {new Date(`${h.day}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" })}
                      </span>
                      <span className="flex items-center gap-2">
                        <span className="text-xs text-slate-400 tabular-nums">
                          {clock(h.first_in)} – {clock(h.last_out)}
                        </span>
                        <DayBadge status={h.status} />
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </SidePanel>

      <Modal open={!!manage} onClose={() => { setManage(null); load(); }} title={manage || ""} width="max-w-6xl">
        {manage === "Requests" && <RequestsTab />}
        {manage === "Leave balances" && <BalancesTab />}
        {manage === "Holidays" && <HolidaysPanel />}
        {manage === "Policy" && <PolicyPanel />}
      </Modal>
    </div>
  );
}
