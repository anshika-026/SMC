import { useEffect, useState } from "react";
import InsightCard from "../workforce/InsightCard";
import * as api from "../../api/client";
import { shiftIso, todayIso } from "../workforce/shared";

// The Intrusion page's three KPI cards, in the Attendance / Visitor Management card style
// (InsightCard; each card has one view, its period picker sets the chart span):
//   Intrusion Alerts Today  restricted-area / entry-gate alerts today vs yesterday -> filters the list
//   Loitering Alerts        loitering alerts today vs yesterday                    -> filters the list
//   Intrusion Trends        this period vs the previous one, peak time window      -> Historical Insights
// Real records only: /intrusion/alerts (last 30 days) and /intrusion/analytics.

const DAYS = 30;

function hourLabel(h) {
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}${h < 12 ? "am" : "pm"}`;
}

const dayOf = (ts) => {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

// "+2 vs yesterday" / "same as yesterday" (null when there is nothing to compare with).
function versus(now, before, word) {
  if (before === null || before === undefined) return null;
  if (now === before) return `same as ${word}`;
  const d = now - before;
  return `${d > 0 ? "+" : ""}${d} vs ${word}`;
}

export default function IntrusionKpiCards({ refreshKey, onToday, onLoitering, onTrends }) {
  const [period, setPeriod] = useState({ today: "week", loitering: "week", trends: "month" });
  const [alerts, setAlerts] = useState(null);
  const [trend, setTrend] = useState(null);

  useEffect(() => {
    const today = todayIso();
    api.intrusionApi.alerts({ start: shiftIso(today, -(DAYS - 1)), end: today }).then((d) => setAlerts(d.alerts)).catch(() => setAlerts([]));
  }, [refreshKey]);

  const trendDays = period.trends === "month" ? 30 : 7;
  useEffect(() => {
    const today = todayIso();
    api.intrusionApi.analytics({ start: shiftIso(today, -(trendDays - 1)), end: today }).then(setTrend).catch(() => setTrend(null));
  }, [refreshKey, trendDays]);

  const today = todayIso();
  const yesterday = shiftIso(today, -1);
  const span = (p) => (p === "month" ? DAYS : 7);
  const daily = (event, p) =>
    Array.from({ length: span(p) }, (_, i) => {
      const day = shiftIso(today, i - span(p) + 1);
      return { day, v: (alerts || []).filter((x) => x.event === event && dayOf(x.ts) === day).length };
    });
  const on = (event, day) => (alerts || []).filter((x) => x.event === event && dayOf(x.ts) === day);
  const plural = (x, one, many = `${one}s`) => `${x} ${x === 1 ? one : many}`;

  const intrToday = on("Intrusion Detected", today);
  const intrYest = on("Intrusion Detected", yesterday).length;
  const loitToday = on("Loitering Detected", today);
  const loitYest = on("Loitering Detected", yesterday).length;
  const openToday = intrToday.filter((x) => x.status !== "Resolved").length;
  const place = (list) => {
    const c = {};
    for (const x of list) {
      const k = x.area || x.zone_name || x.camera_name;
      c[k] = (c[k] || 0) + 1;
    }
    return Object.entries(c).sort((a, b) => b[1] - a[1]);
  };

  // Trends: this period vs the previous period of the same length.
  const tNow = trend?.total ?? null;
  const tPrev = trend?.previous?.total ?? null;
  const change = tNow === null || tPrev === null ? null : tPrev === 0 ? (tNow === 0 ? 0 : null) : Math.round(((tNow - tPrev) / tPrev) * 100);
  const direction = tNow === null ? "" : tNow > tPrev ? "rising" : tNow < tPrev ? "falling" : "steady";
  const peak = trend?.peak_hour;
  const window = peak === null || peak === undefined ? null : `${hourLabel(peak)}–${hourLabel((peak + 1) % 24)}`;
  const periodWord = trendDays === 30 ? "30 days" : "7 days";

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-stretch">
      <InsightCard
        title="Intrusion Alerts Today"
        tone="orange"
        period={period.today}
        onPeriod={(p) => setPeriod((x) => ({ ...x, today: p }))}
        metrics={[
          {
            key: "intrusion_today",
            label: "Restricted areas and entry gates",
            value: alerts ? intrToday.length : null,
            series: daily("Intrusion Detected", period.today),
            better: "down",
            people: alerts && intrToday.length ? place(intrToday).map(([name, n]) => ({ name: `${name} · ${n}`, warn: true })) : undefined,
            insight: alerts
              ? `${plural(intrToday.length, "intrusion")} today${versus(intrToday.length, intrYest, "yesterday") ? ` (${versus(intrToday.length, intrYest, "yesterday")})` : ""}; ${openToday} still open.`
              : "Loading…",
            onOpen: onToday,
          },
        ]}
      />
      <InsightCard
        title="Loitering Alerts"
        tone="slate"
        period={period.loitering}
        onPeriod={(p) => setPeriod((x) => ({ ...x, loitering: p }))}
        metrics={[
          {
            key: "loitering_today",
            label: "Stayed too long in a zone, today",
            value: alerts ? loitToday.length : null,
            series: daily("Loitering Detected", period.loitering),
            better: "down",
            people: alerts && loitToday.length ? place(loitToday).map(([name, n]) => ({ name: `${name} · ${n}`, warn: true })) : undefined,
            insight: alerts
              ? `${plural(loitToday.length, "loitering incident")} today${versus(loitToday.length, loitYest, "yesterday") ? ` (${versus(loitToday.length, loitYest, "yesterday")})` : ""}.`
              : "Loading…",
            onOpen: onLoitering,
          },
        ]}
      />
      <InsightCard
        title="Intrusion Trends"
        tone="blue"
        period={period.trends}
        onPeriod={(p) => setPeriod((x) => ({ ...x, trends: p }))}
        metrics={[
          {
            key: "trend",
            label: `Incidents, last ${periodWord}`,
            value: tNow,
            display: tNow === null ? null : change === null ? String(tNow) : `${tNow} (${change > 0 ? "+" : ""}${change}%)`,
            series: (trend?.per_day || []).map((d) => ({ day: d.date, v: d.count })),
            better: "down",
            insight: trend
              ? `${direction === "steady" ? "Steady" : direction === "rising" ? "Rising" : "Falling"}: ${plural(tNow, "incident")} vs ${tPrev} in the previous ${periodWord}.` +
                (window ? ` Most happen ${window}.` : "")
              : "Loading…",
            onOpen: () => onTrends(trendDays),
          },
        ]}
      />
    </div>
  );
}
