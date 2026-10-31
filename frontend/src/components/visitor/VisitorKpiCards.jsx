import { useEffect, useState } from "react";
import InsightCard from "../workforce/InsightCard";
import * as api from "../../api/client";
import { shiftIso, todayIso } from "../workforce/shared";

// The three Visitor Management KPI cards, in the Attendance card style
// (InsightCard: arrows / swipe page through a card's views, a click opens the
// detailed view of the view shown):
//   Visitor Activity   today's visitors       -> the visitor records, filtered
//   Visitor Trends     visits over time       -> Analytics
//   Footfall Analytics people entering gates  -> the Footfall view
// Visitor numbers: /visitor-records/kpis and the last 30 days of visits.
// Footfall numbers: /footfall/day (today) and /footfall/history (30 days).

const DAYS = 30;

function hourLabel(h) {
  if (h === null || h === undefined) return "—";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}${h < 12 ? "am" : "pm"}`;
}

export default function VisitorKpiCards({ k, onFilter, onAnalytics, onFootfall, refreshKey }) {
  const [period, setPeriod] = useState({ activity: "week", trends: "month", footfall: "week" });
  const [rows, setRows] = useState([]);
  const [ffDays, setFfDays] = useState([]);
  const [ffToday, setFfToday] = useState(null);

  useEffect(() => {
    const today = todayIso();
    api.vr
      .visits({ from: shiftIso(today, -(DAYS - 1)), to: today, limit: 5000 })
      .then((d) => setRows(d.visits))
      .catch(() => setRows([]));
    api.getFootfallHistory(shiftIso(today, -(DAYS - 1)), today).then((d) => setFfDays(d.days)).catch(() => setFfDays([]));
    api.getFootfallDay(today).then(setFfToday).catch(() => setFfToday(null));
  }, [k, refreshKey]);

  const span = (p) => (p === "month" ? DAYS : 7);
  // Daily values of fn(visits of that day) for the last 7 / 30 days.
  const series = (fn, p) => {
    const days = span(p);
    const today = todayIso();
    return Array.from({ length: days }, (_, i) => {
      const day = shiftIso(today, i - days + 1);
      return { day, v: fn(rows.filter((r) => r.visit_date === day)) };
    });
  };
  const ffSeries = (key, p) => ffDays.slice(-span(p)).map((d) => ({ day: d.day, v: d[key] }));
  const count = (test) => (list) => list.filter(test).length;
  const visitors = (test = () => true) => (list) => new Set(list.filter(test).map((r) => r.visitor_id)).size;
  const n = (x) => (k ? x : null);
  const plural = (x, one, many = `${one}s`) => `${x} ${x === 1 ? one : many}`;
  const ff = ffToday;
  const peakCount = ff && ff.peak_hour != null ? ff.hourly.find((h) => h.hour === ff.peak_hour)?.entries ?? 0 : 0;
  const week = ffDays.slice(-7).reduce((a, d) => a + d.entries, 0);

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-stretch mb-5">
      <InsightCard
        title="Visitor Activity"
        tone="blue"
        period={period.activity}
        onPeriod={(p) => setPeriod((x) => ({ ...x, activity: p }))}
        metrics={[
          {
            key: "visitors",
            label: "Visitors today",
            value: n(k?.visitors_today),
            series: series(visitors(), period.activity),
            better: "up",
            insight: k ? `${plural(k.visitors_today, "visitor")} registered for today; ${k.inside_now} inside now.` : "Loading…",
            onOpen: () => onFilter({ preset: "today" }),
          },
          {
            key: "first",
            label: "First-time visitors",
            value: n(k?.first_time_today),
            series: series(visitors((r) => r.first_visit), period.activity),
            insight: k ? `${plural(k.first_time_today, "visitor")} came for the first time today.` : "Loading…",
            onOpen: () => onFilter({ preset: "today", type: "first" }),
          },
          {
            key: "repeat",
            label: "Repeat visitors",
            value: n(k?.repeat_today),
            series: series(visitors((r) => !r.first_visit), period.activity),
            insight: k ? `${plural(k.repeat_today, "visitor")} had visited before.` : "Loading…",
            onOpen: () => onFilter({ preset: "today", type: "repeat" }),
          },
          {
            key: "pending",
            label: "Expected, not checked in",
            value: n(k?.pending_today),
            series: series(count((r) => r.status === "pending"), period.activity),
            better: "down",
            people: k ? (k.pending_names || []).map((name) => ({ name })) : undefined,
            insight: k ? (k.pending_today ? `${plural(k.pending_today, "registered visitor")} not checked in yet.` : "Everyone registered today has checked in.") : "Loading…",
            onOpen: () => onFilter({ preset: "today", status: "pending" }),
          },
        ]}
      />
      <InsightCard
        title="Visitor Trends"
        tone="slate"
        period={period.trends}
        onPeriod={(p) => setPeriod((x) => ({ ...x, trends: p }))}
        metrics={[
          {
            key: "month",
            label: "Visits this month",
            value: n(k?.visits_this_month),
            series: series((list) => list.length, period.trends),
            better: "up",
            insight: k ? `${plural(k.visits_this_month, "visit")} so far this month. Open for daily, weekly and monthly reports.` : "Loading…",
            onOpen: onAnalytics,
          },
          {
            key: "repeat_month",
            label: "Repeat visits this month",
            value: n(k?.repeat_visits_this_month),
            series: series(count((r) => !r.first_visit), period.trends),
            insight: k ? `${plural(k.repeat_visits_this_month, "visit")} by people who had visited before.` : "Loading…",
            onOpen: onAnalytics,
          },
          {
            key: "frequent",
            label: "Most frequent visitors",
            value: n(k?.most_frequent?.length),
            total: Math.max(k?.most_frequent?.length || 0, 1),
            people: k ? k.most_frequent.map((f) => ({ name: `${f.name} · ${f.visits}`, hint: `${f.visits} visits this month` })) : undefined,
            insight: k ? (k.most_frequent.length ? "Visitors with more than one visit this month." : "No repeat visitors this month yet.") : "Loading…",
            onOpen: onAnalytics,
          },
        ]}
      />
      <InsightCard
        title="Footfall Analytics"
        tone="teal"
        period={period.footfall}
        onPeriod={(p) => setPeriod((x) => ({ ...x, footfall: p }))}
        metrics={[
          {
            key: "ff_entries",
            label: "People entering today",
            value: ff ? ff.entries : null,
            series: ffSeries("entries", period.footfall),
            better: "up",
            insight: ff ? `${plural(ff.entries, "person", "people")} counted at the gates today (${week} this week).` : "Loading…",
            onOpen: onFootfall,
          },
          {
            key: "ff_exits",
            label: "Exits today",
            value: ff ? ff.exits : null,
            series: ffSeries("exits", period.footfall),
            insight: ff ? (ff.gates_without_entry_line ? "Exits are counted only at gates with an entry line." : `${plural(ff.exits, "exit")} through the gates today.`) : "Loading…",
            onOpen: onFootfall,
          },
          {
            key: "ff_peak",
            label: "Peak hour today",
            value: peakCount,
            display: ff ? (ff.peak_hour == null ? "—" : `${hourLabel(ff.peak_hour)}–${hourLabel((ff.peak_hour + 1) % 24)}`) : null,
            total: Math.max(ff?.entries || 0, 1),
            insight: ff ? (ff.peak_hour == null ? "Nobody counted yet today." : `${plural(peakCount, "arrival")} in the busiest hour.`) : "Loading…",
            onOpen: onFootfall,
          },
          {
            key: "ff_split",
            label: "Employees / others",
            value: ff ? ff.visitor_entries : null,
            display: ff ? `${ff.employee_entries} / ${ff.visitor_entries}` : null,
            total: Math.max(ff?.entries || 0, 1),
            insight: ff ? "Employees are those named by face at the gate; everyone else counts as others." : "Loading…",
            onOpen: onFootfall,
          },
        ]}
      />
    </div>
  );
}
