import { Activity, Building, Clock3, TrendingUp } from "lucide-react";
import { dur } from "./shared";

// The four consolidated KPI cards. Every number is from visit records
// (/visitor-records/kpis); a row with onClick filters the records table.

export default function VisitorKpiCards({ k, onFilter, onOpenVisitor }) {
  const v = (x) => (k ? x : null);
  const cards = [
    {
      title: "Visitor Activity", icon: Activity, tone: "text-brand-600 bg-brand-50",
      rows: [
        { label: "Total visitors today", value: v(k?.visitors_today), onClick: () => onFilter({ preset: "today" }) },
        { label: "First-time visitors", value: v(k?.first_time_today), onClick: () => onFilter({ preset: "today", type: "first" }) },
        { label: "Repeat visitors", value: v(k?.repeat_today), onClick: () => onFilter({ preset: "today", type: "repeat" }) },
      ],
    },
    {
      title: "Current Premises Status", icon: Building, tone: "text-emerald-600 bg-emerald-50",
      rows: [
        { label: "Currently inside", value: v(k?.inside_now), strong: true, onClick: () => onFilter({ preset: "all", status: "in" }), names: k?.inside_names },
        { label: "Checked out", value: v(k?.checked_out_today), onClick: () => onFilter({ preset: "today", status: "out" }) },
        { label: "Pending check-in", value: v(k?.pending_today), onClick: () => onFilter({ preset: "today", status: "pending" }) },
      ],
    },
    {
      title: "Visit Duration", icon: Clock3, tone: "text-violet-600 bg-violet-50",
      rows: [
        { label: "Average visit duration", value: k ? dur(k.average_duration_seconds, "No data") : null, sub: k ? `${k.duration_measured} completed visit${k.duration_measured === 1 ? "" : "s"}` : "" },
        { label: "Average waiting time", value: k ? dur(k.average_waiting_seconds, "Not recorded") : null, sub: k ? `${k.waiting_measured} with a meeting start` : "" },
        { label: `Long-staying (> ${k ? k.long_stay_minutes : 120} min)`, value: v(k?.long_staying), warn: true, names: k?.long_staying_names },
      ],
    },
    {
      title: "Visitor Trends", icon: TrendingUp, tone: "text-sky-600 bg-sky-50",
      rows: [
        { label: "Total visits this month", value: v(k?.visits_this_month), onClick: () => onFilter({ preset: "month" }) },
        { label: "Repeat visits this month", value: v(k?.repeat_visits_this_month), onClick: () => onFilter({ preset: "month", type: "repeat" }) },
      ],
      frequent: k?.most_frequent,
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4 mb-5">
      {cards.map((c) => (
        <div key={c.title} className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)] p-4 min-w-0 flex flex-col">
          <div className="flex items-center gap-2 mb-3">
            <span className={`w-7 h-7 rounded-lg grid place-items-center ${c.tone}`}><c.icon size={15} /></span>
            <h3 className="text-[13px] font-semibold text-ink-900">{c.title}</h3>
          </div>
          <div className="space-y-2.5 flex-1">
            {c.rows.map((r) => (
              <button
                key={r.label}
                type="button"
                disabled={!r.onClick}
                onClick={r.onClick}
                className={`w-full text-left flex items-baseline justify-between gap-2 rounded-lg -mx-1.5 px-1.5 py-0.5 ${r.onClick ? "hover:bg-slate-50" : "cursor-default"}`}
              >
                <span className="text-xs text-slate-500 min-w-0">
                  {r.label}
                  {r.sub && <span className="block text-[10px] text-slate-400">{r.sub}</span>}
                </span>
                <span className={`tabular-nums font-semibold ${r.strong ? "text-2xl" : "text-lg"} ${r.strong ? "text-emerald-700" : r.warn && r.value > 0 ? "text-amber-600" : "text-ink-900"}`}>
                  {r.value ?? "—"}
                </span>
              </button>
            ))}
            {c.rows.some((r) => r.names?.length) && (
              <div className="flex flex-wrap gap-1 pt-1">
                {c.rows.flatMap((r) => (r.names || []).map((n) => (
                  <span key={`${r.label}-${n}`} className={`text-[11px] px-2 py-0.5 rounded-full ${r.warn ? "bg-amber-50 text-amber-700" : "bg-emerald-50 text-emerald-700"}`}>{n}</span>
                )))}
              </div>
            )}
            {c.frequent !== undefined && (
              <div className="pt-1">
                <p className="text-xs text-slate-500 mb-1">Most frequent visitors</p>
                {!c.frequent?.length ? (
                  <p className="text-[11px] text-slate-400">No repeat visitors this month yet.</p>
                ) : (
                  <ul className="space-y-0.5">
                    {c.frequent.slice(0, 3).map((f) => (
                      <li key={f.visitor_id}>
                        <button type="button" onClick={() => onOpenVisitor(f.visitor_id)} className="w-full flex justify-between text-xs hover:text-brand-600">
                          <span className="truncate text-ink-900">{f.name}</span>
                          <span className="text-slate-500 shrink-0">{f.visits} visits</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
