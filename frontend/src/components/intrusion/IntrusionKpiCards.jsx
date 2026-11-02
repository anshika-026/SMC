import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import InsightCard from "../workforce/InsightCard";
import * as api from "../../api/client";
import { shiftIso, todayIso } from "../workforce/shared";
import { formatDuration } from "../../lib/zoneSettings";

// The three Intrusion KPI cards, in the same style as Attendance and Visitor
// Management (InsightCard: arrows / swipe page through a card's views, a click
// opens the tab for the view shown):
//   Intrusion Activity  today's intrusion / loitering alerts  -> Alerts
//   Zone Monitoring     zones guarding now, detections        -> Visitor Management > Zones & areas
//   Intrusion Trends    alerts over 7 / 30 days, peak, camera -> Analytics
// Real records only: /intrusion/alerts and /intrusion/events for the last 30 days.

const DAYS = 30;

function hourLabel(h) {
  if (h === null || h === undefined) return "—";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}${h < 12 ? "am" : "pm"}`;
}

const dayOf = (ts) => {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

export default function IntrusionKpiCards({ zones, refreshKey, onAlerts, zonesPage, onAnalytics }) {
  const navigate = useNavigate();
  const onZones = () => navigate(zonesPage);
  const [period, setPeriod] = useState({ activity: "week", zones: "week", trends: "month" });
  const [alerts, setAlerts] = useState(null);
  const [events, setEvents] = useState([]);

  useEffect(() => {
    const today = todayIso();
    const start = shiftIso(today, -(DAYS - 1));
    api.intrusionApi.alerts({ start, end: today }).then((d) => setAlerts(d.alerts)).catch(() => setAlerts([]));
    api.intrusionApi.events(start, today).then(setEvents).catch(() => setEvents([]));
  }, [refreshKey]);

  const today = todayIso();
  const span = (p) => (p === "month" ? DAYS : 7);
  // Daily values of fn(items of that day) for the last 7 / 30 days.
  const series = (items, fn, p) => {
    const days = span(p);
    return Array.from({ length: days }, (_, i) => {
      const day = shiftIso(today, i - days + 1);
      return { day, v: fn(items.filter((x) => dayOf(x.ts) === day)) };
    });
  };
  const count = (test = () => true) => (list) => list.filter(test).length;
  const plural = (x, one, many = `${one}s`) => `${x} ${x === 1 ? one : many}`;

  const a = alerts || [];
  const todays = a.filter((x) => dayOf(x.ts) === today);
  const n = (x) => (alerts ? x : null);
  const open = todays.filter((x) => x.status !== "Resolved");
  const loiter = todays.filter((x) => x.event === "Loitering Detected");
  const clips = todays.filter((x) => x.has_clip);
  const detectionsToday = events.filter((e) => dayOf(e.ts) === today).length;

  const enabled = zones.filter((z) => z.enabled);
  const guarding = enabled.filter((z) => z.active_now);
  const gates = zones.filter((z) => z.is_entry_gate);

  // Trends over the chosen period.
  const inPeriod = a.filter((x) => dayOf(x.ts) >= shiftIso(today, -(span(period.trends) - 1)));
  const byHour = Array.from({ length: 24 }, (_, h) => inPeriod.filter((x) => new Date(x.ts * 1000).getHours() === h).length);
  const peak = inPeriod.length ? byHour.indexOf(Math.max(...byHour)) : null;
  const byCam = {};
  for (const x of inPeriod) byCam[x.camera_name] = (byCam[x.camera_name] || 0) + 1;
  const topCams = Object.entries(byCam).sort((p, q) => q[1] - p[1]);
  const avg = inPeriod.length ? inPeriod.reduce((s, x) => s + x.duration_seconds, 0) / inPeriod.length : null;
  const periodWord = period.trends === "month" ? "the last 30 days" : "the last 7 days";

  return (
    <div className="grid grid-cols-1 md:grid-cols-3 gap-4 items-stretch">
      <InsightCard
        title="Intrusion Activity"
        tone="orange"
        period={period.activity}
        onPeriod={(p) => setPeriod((x) => ({ ...x, activity: p }))}
        metrics={[
          {
            key: "alerts",
            label: "Intrusion alerts today",
            value: n(todays.length),
            series: series(a, count(), period.activity),
            better: "down",
            insight: alerts ? (todays.length ? `${plural(todays.length, "alert")} raised today; ${open.length} still open.` : "No intrusion alerts today.") : "Loading…",
            onOpen: onAlerts,
          },
          {
            key: "open",
            label: "Still open",
            value: n(open.length),
            series: series(a, count((x) => x.status !== "Resolved"), period.activity),
            better: "down",
            people: alerts ? open.slice(0, 6).map((x) => ({ name: `${x.zone_name || "Zone"} · ${x.camera_name}`, warn: true })) : undefined,
            insight: alerts ? (open.length ? `${plural(open.length, "alert")} waiting to be acknowledged or resolved.` : "Every alert today has been resolved.") : "Loading…",
            onOpen: onAlerts,
          },
          {
            key: "loitering",
            label: "Loitering alerts today",
            value: n(loiter.length),
            series: series(a, count((x) => x.event === "Loitering Detected"), period.activity),
            better: "down",
            insight: alerts ? (loiter.length ? `${plural(loiter.length, "person", "people")} stayed in a zone longer than its limit.` : "Nobody stayed in a loitering zone too long today.") : "Loading…",
            onOpen: onAlerts,
          },
          {
            key: "clips",
            label: "Alerts with a video clip",
            value: n(clips.length),
            total: Math.max(todays.length, 1),
            insight: alerts ? (todays.length ? `${clips.length} of ${plural(todays.length, "alert")} today have a ~10 s evidence clip.` : "Each alert gets a ~10 s evidence clip.") : "Loading…",
            onOpen: onAlerts,
          },
        ]}
      />
      <InsightCard
        title="Zone Monitoring"
        tone="slate"
        period={period.zones}
        onPeriod={(p) => setPeriod((x) => ({ ...x, zones: p }))}
        metrics={[
          {
            key: "guarding",
            label: "Zones guarding now",
            value: guarding.length,
            total: Math.max(enabled.length, 1),
            people: guarding.slice(0, 6).map((z) => ({ name: `${z.area ? `${z.area} · ` : ""}${z.name}`, hint: z.camera_name })),
            insight: zones.length ? `${guarding.length} of ${plural(enabled.length, "enabled zone")} inside their active hours.` : "No zones yet. Draw one in Visitor Management > Zones & areas.",
            onOpen: onZones,
          },
          {
            key: "detections",
            label: "Detections today",
            value: detectionsToday,
            series: series(events, (list) => list.length, period.zones),
            better: "down",
            insight: detectionsToday ? `${plural(detectionsToday, "detection")} of people inside a zone today (logged at most every 30 s per zone).` : "No one detected inside a zone today.",
            onOpen: onZones,
          },
          {
            key: "enabled",
            label: "Enabled zones",
            value: enabled.length,
            total: Math.max(zones.length, 1),
            insight: zones.length ? `${enabled.length} of ${plural(zones.length, "zone")} switched on across ${new Set(zones.map((z) => z.camera_id)).size} camera(s).` : "No zones yet.",
            onOpen: onZones,
          },
          {
            key: "gates",
            label: "Entry gates",
            value: gates.length,
            total: Math.max(zones.length, 1),
            people: gates.slice(0, 6).map((z) => ({ name: z.name, hint: z.camera_name })),
            insight: gates.length ? `${plural(gates.length, "entry gate")} watched, e.g. ${gates[0].name}.` : "No entry gate marked yet.",
            onOpen: onZones,
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
            key: "total",
            label: period.trends === "month" ? "Alerts, last 30 days" : "Alerts, last 7 days",
            value: n(inPeriod.length),
            series: series(a, count(), period.trends),
            better: "down",
            insight: alerts ? `${plural(inPeriod.length, "intrusion alert")} in ${periodWord}. Open for daily, weekly and monthly reports.` : "Loading…",
            onOpen: onAnalytics,
          },
          {
            key: "peak",
            label: "Peak intrusion time",
            value: peak === null ? 0 : byHour[peak],
            display: alerts ? (peak === null ? "—" : `${hourLabel(peak)}–${hourLabel((peak + 1) % 24)}`) : null,
            total: Math.max(inPeriod.length, 1),
            insight: alerts ? (peak === null ? `No intrusions in ${periodWord}.` : `${plural(byHour[peak], "alert")} in the busiest hour over ${periodWord}.`) : "Loading…",
            onOpen: onAnalytics,
          },
          {
            key: "camera",
            label: "Most affected camera",
            value: topCams.length ? topCams[0][1] : 0,
            display: alerts ? (topCams.length ? topCams[0][0] : "—") : null,
            total: Math.max(inPeriod.length, 1),
            people: topCams.slice(0, 6).map(([name, c]) => ({ name: `${name} · ${c}` })),
            insight: alerts ? (topCams.length ? `${topCams[0][0]} raised ${plural(topCams[0][1], "alert")} in ${periodWord}.` : `No intrusions in ${periodWord}.`) : "Loading…",
            onOpen: onAnalytics,
          },
          {
            key: "duration",
            label: "Average intrusion duration",
            value: avg === null ? 0 : Math.round(avg),
            display: alerts ? (avg === null ? "—" : formatDuration(avg)) : null,
            total: Math.max(...inPeriod.map((x) => x.duration_seconds), 1),
            insight: alerts ? (avg === null ? `No intrusions in ${periodWord}.` : `From first detection to the last one counted against each alert, over ${periodWord}.`) : "Loading…",
            onOpen: onAnalytics,
          },
        ]}
      />
    </div>
  );
}
