import { useCallback, useEffect, useState } from "react";
import InsightCard from "../workforce/InsightCard";
import * as api from "../../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../../lib/visibleInterval";
import { fmtTime } from "../VisitorAlerts";
import { fmtDuration } from "../VisitorHistory";
import { shiftIso, todayIso } from "../workforce/shared";

// The three Visitor Management KPI cards, in the Attendance card style
// (InsightCard: arrows / swipe page through a card's views, a click opens the
// detailed view of the view shown). Their numbers are what the cameras saw in
// the visitor area, the same data as the "People in visitor area" tab (one row
// per visit, visitors numbered #1, #2, ...; sittings that were an employee's are
// not visitors), except Footfall:
//   Visitor Activity              is a visitor there now, visitors today, returning
//   Visitor Trends                visits per day, average stay, most frequent
//   Footfall Analytics            people entering gates (/footfall/day, /footfall/history)
// The first view of each card is the one to see without paging.
// onOpen("visitor") opens People in visitor area filtered; onFootfall the Footfall view.

const DAYS = 30;
const REFRESH_MS = 15_000;

function dayOf(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

const label = (r) => (r.visitor_number ? `Visitor #${r.visitor_number}` : "Visitor");
const personKey = (r) => (r.visitor_number ? `n${r.visitor_number}` : r.visit);
const plural = (x, one, many = `${one}s`) => `${x} ${x === 1 ? one : many}`;
const hhmm = (ts) => fmtTime(ts).slice(0, 5);

function hourLabel(h) {
  if (h === null || h === undefined) return "—";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}${h < 12 ? "am" : "pm"}`;
}

export default function VisitorKpiCards({ onOpen, onFootfall, refreshKey }) {
  const [period, setPeriod] = useState({ activity: "week", trends: "month", footfall: "week" });
  const [visits, setVisits] = useState(null);
  const [ffDays, setFfDays] = useState([]);
  const [ffToday, setFfToday] = useState(null);

  const load = useCallback(() => {
    const today = todayIso();
    api.getVisitorVisits({ limit: 2000, start: shiftIso(today, -(DAYS - 1)), end: today })
      .then((d) => setVisits(d.visits || []))
      .catch(() => setVisits([]));
    api.getFootfallHistory(shiftIso(today, -(DAYS - 1)), today).then((d) => setFfDays(d.days)).catch(() => setFfDays([]));
    api.getFootfallDay(today).then(setFfToday).catch(() => setFfToday(null));
  }, []);

  useEffect(() => {
    load();
    const h = setVisibleInterval(load, REFRESH_MS);
    return () => clearVisibleInterval(h);
  }, [load, refreshKey]);

  const ready = visits !== null;
  const today = todayIso();
  const v = visits || [];
  const vToday = v.filter((r) => dayOf(r.arrived_at) === today);
  const n = (x) => (ready ? x : null);

  // Daily values of fn(rows of that day) for the last 7 / 30 days.
  const series = (list, fn, p) => {
    const days = p === "month" ? DAYS : 7;
    return Array.from({ length: days }, (_, i) => {
      const day = shiftIso(today, i - days + 1);
      return { day, v: fn(list.filter((r) => dayOf(r.arrived_at) === day)) };
    });
  };
  const people = (list) => new Set(list.map(personKey)).size;
  // A visitor who came before today: visits in all > their visits today.
  const returningOf = (list) => {
    const seen = new Map();
    list.forEach((r) => r.visitor_number && seen.set(r.visitor_number, (seen.get(r.visitor_number) || 0) + 1));
    return [...seen.entries()].filter(([num, k]) => (list.find((r) => r.visitor_number === num)?.visitor_visits || 0) > k).length;
  };
  const avgMin = (list) => (list.length ? Math.round(list.reduce((a, r) => a + r.duration_seconds, 0) / list.length / 60) : 0);

  const visitorsNow = vToday.filter((r) => r.status === "inside");
  const visitorsToday = people(vToday);
  const returningToday = returningOf(vToday);
  const avgToday = vToday.length ? vToday.reduce((a, r) => a + r.duration_seconds, 0) / vToday.length : 0;
  const longest = vToday.reduce((a, r) => (!a || r.duration_seconds > a.duration_seconds ? r : a), null);
  const monthVisits = v.length;
  const frequent = (() => {
    const c = new Map();
    v.forEach((r) => r.visitor_number && c.set(r.visitor_number, (c.get(r.visitor_number) || 0) + 1));
    return [...c.entries()].filter(([, k]) => k > 1).sort((a, b) => b[1] - a[1]);
  })();
  const ff = ffToday;
  const ffSeries = (key, p) => ffDays.slice(-(p === "month" ? DAYS : 7)).map((d) => ({ day: d.day, v: d[key] }));
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
            key: "now",
            label: "Visitor in visitor area now",
            value: n(visitorsNow.length),
            display: ready ? (visitorsNow.length ? `Yes · ${visitorsNow.length}` : "No") : null,
            total: Math.max(visitorsToday, 1),
            people: ready ? visitorsNow.map((r) => ({ name: `${label(r)} · since ${hhmm(r.arrived_at)}`, warn: true })) : undefined,
            insight: ready
              ? visitorsNow.length ? `${plural(visitorsNow.length, "visitor")} waiting in the visitor area right now.` : "No visitor in the visitor area right now."
              : "Loading…",
            onOpen: () => onOpen("visitor"),
          },
          {
            key: "today",
            label: "Visitors today",
            value: n(visitorsToday),
            series: series(v, people, period.activity),
            better: "up",
            insight: ready
              ? visitorsToday ? `${visitorsToday - returningToday} new, ${returningToday} came before · ${plural(vToday.length, "visit")} today.` : "No visitors today yet."
              : "Loading…",
            onOpen: () => onOpen("visitor"),
          },
          {
            key: "returning",
            label: "Returning visitors today",
            value: n(returningToday),
            series: series(v, returningOf, period.activity),
            insight: ready ? `${plural(returningToday, "visitor")} today had visited before.` : "Loading…",
            onOpen: () => onOpen("visitor"),
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
            key: "visits",
            label: `Visits, last ${DAYS} days`,
            value: n(monthVisits),
            series: series(v, (list) => list.length, period.trends),
            better: "up",
            insight: ready ? `${plural(monthVisits, "visit")} by ${plural(people(v), "visitor")} in the last ${DAYS} days.` : "Loading…",
            onOpen: () => onOpen("visitor"),
          },
          {
            key: "stay",
            label: "Average visitor stay today",
            value: n(Math.round(avgToday / 60)),
            display: ready ? (vToday.length ? fmtDuration(avgToday) : "—") : null,
            series: series(v, avgMin, period.trends),
            insight: ready
              ? longest ? `Longest today: ${label(longest)}, ${fmtDuration(longest.duration_seconds)}. Chart: average minutes per day.` : "No visits today yet."
              : "Loading…",
            onOpen: () => onOpen("visitor"),
          },
          {
            key: "frequent",
            label: "Most frequent visitors",
            value: n(frequent.length),
            total: Math.max(people(v), 1),
            people: ready ? frequent.map(([num, k]) => ({ name: `Visitor #${num} · ${k}`, hint: `${k} visits in the last ${DAYS} days` })) : undefined,
            insight: ready ? (frequent.length ? `Visitors with more than one visit in the last ${DAYS} days.` : `No repeat visitors in the last ${DAYS} days.`) : "Loading…",
            onOpen: () => onOpen("visitor"),
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
            label: "People today",
            value: ff ? ff.entries : null,
            series: ffSeries("entries", period.footfall),
            better: "up",
            insight: ff ? `${plural(ff.entries, "person", "people")} came today, each counted once (${week} person-days this week).` : "Loading…",
            onOpen: onFootfall,
          },
          {
            key: "ff_exits",
            label: "Left today",
            value: ff ? ff.exits : null,
            total: Math.max(ff?.entries || 0, 1),
            insight: ff ? `${plural(ff.exits, "person", "people")} of ${ff.entries} have left; ${ff.entries - ff.exits} still on site.` : "Loading…",
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
            label: "Employees / visitors",
            value: ff ? ff.visitor_entries : null,
            display: ff ? `${ff.employee_entries} / ${ff.visitor_entries}` : null,
            total: Math.max(ff?.entries || 0, 1),
            insight: ff ? "Employees recognised by face on any camera / visitors (Visitor #n)." : "Loading…",
            onOpen: onFootfall,
          },
        ]}
      />
    </div>
  );
}
