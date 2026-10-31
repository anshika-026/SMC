import { useRef, useState } from "react";
import { ArrowDownRight, ArrowUpRight, ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { Area, AreaChart, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis } from "recharts";

// A clean analytics card: title + period, one metric at a time, a big number
// with a trend arrow, a soft chart of that metric's real daily values (or a
// donut when it only exists for today) and a one-line insight.
//
// The card is a small carousel of its metrics: the left/right arrow buttons, a
// touch swipe (left = next, right = previous) and the keyboard's arrow keys all
// move the same page index. Clicking anywhere else on the card (or Enter) opens
// the detailed view of the metric currently shown (metric.onOpen); the arrows
// and a swipe never do.
//
// metric: { key, label, value, display?, total?, series?: [{day, v}], better: "up" | "down" | null,
//           insight, onOpen, people?: [{name, hint?, warn?}] (names shown above the chart) }

const TONES = {
  // Light chart lines (300-400 shades) so the graphs stay soft.
  orange: { stroke: "#fdba74", fill: "#fed7aa", dot: "#fb923c", text: "text-orange-600", soft: "bg-orange-50 text-orange-600" },
  blue: { stroke: "#93c5fd", fill: "#bfdbfe", dot: "#60a5fa", text: "text-blue-600", soft: "bg-blue-50 text-blue-600" },
  slate: { stroke: "#cbd5e1", fill: "#e2e8f0", dot: "#94a3b8", text: "text-slate-600", soft: "bg-slate-100 text-slate-600" },
  teal: { stroke: "#5eead4", fill: "#99f6e4", dot: "#2dd4bf", text: "text-teal-600", soft: "bg-teal-50 text-teal-600" },
};

const MAX_NAMES = 6;   // name chips shown before "+N more"
const SWIPE_PX = 40;   // horizontal travel that counts as a swipe

function shortDay(iso) {
  return new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

function Trend({ metric, tone }) {
  const s = metric.series;
  if (!s || s.length < 2) return null;
  const now = s[s.length - 1].v;
  const before = s[s.length - 2].v;
  if (now === before) return null;
  const up = now > before;
  const good = metric.better ? (metric.better === "up") === up : null;
  const Icon = up ? ArrowUpRight : ArrowDownRight;
  const color = good === null ? tone.text : good ? "text-emerald-600" : "text-rose-500";
  return <Icon size={18} className={`${color} shrink-0 mt-0.5`} aria-label={up ? "up from the day before" : "down from the day before"} />;
}

function Chart({ metric, tone, id }) {
  if (metric.series?.length) {
    const data = metric.series.map((p) => ({ ...p, label: shortDay(p.day) }));
    const last = data.length - 1;
    return (
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 10, right: 6, bottom: 0, left: 6 }}>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={tone.fill} stopOpacity={0.45} />
              <stop offset="100%" stopColor={tone.fill} stopOpacity={0} />
            </linearGradient>
          </defs>
          <XAxis dataKey="label" hide />
          <Tooltip
            cursor={{ stroke: "#e2e8f0" }}
            contentStyle={{ fontSize: 11, borderRadius: 8, border: "1px solid #eef0f5", boxShadow: "0 4px 14px rgba(15,23,42,.06)" }}
            formatter={(v) => [v, metric.label]}
          />
          <Area
            type={metric.curve || "monotone"}
            dataKey="v"
            stroke={tone.stroke}
            strokeWidth={1.5}
            fill={`url(#${id})`}
            isAnimationActive={false}
            dot={(p) =>
              p.index === last ? <circle key="last" cx={p.cx} cy={p.cy} r={4.5} fill={tone.dot} stroke="#fff" strokeWidth={2} /> : <g key={p.index} />
            }
          />
        </AreaChart>
      </ResponsiveContainer>
    );
  }
  const total = Math.max(metric.total || 0, metric.value || 0, 1);
  const data = [
    { name: metric.label, v: metric.value || 0 },
    { name: "rest", v: total - (metric.value || 0) },
  ];
  return (
    <div className="relative w-full h-full">
      <ResponsiveContainer width="100%" height="100%">
        <PieChart>
          <defs>
            <linearGradient id={id} x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor={tone.fill} stopOpacity={0.9} />
              <stop offset="100%" stopColor={tone.stroke} stopOpacity={0.55} />
            </linearGradient>
          </defs>
          <Pie data={data} dataKey="v" innerRadius="0%" outerRadius="88%" startAngle={90} endAngle={-270} stroke="none" isAnimationActive={false}>
            <Cell fill={`url(#${id})`} />
            <Cell fill="#f1f5f9" />
          </Pie>
        </PieChart>
      </ResponsiveContainer>
      <span className="absolute bottom-0 right-1 text-[11px] text-slate-400">
        {metric.value || 0} of {total}
      </span>
    </div>
  );
}

// Controls inside the card change the card, never open its detail.
const stop = (e) => e.stopPropagation();
const STOP = { onClick: stop, onKeyDown: stop, onTouchStart: stop, onTouchEnd: stop };

export default function InsightCard({ title, tone = "blue", metrics, period, onPeriod }) {
  const [i, setI] = useState(0);
  const [dir, setDir] = useState("next");   // which way the last page change went (slide animation)
  const touch = useRef(null);               // where a touch started
  const swipedAt = useRef(0);               // when the last swipe ended: a click right after it is part of the swipe
  const t = TONES[tone];
  const count = metrics.length;
  const idx = Math.min(i, count - 1);
  const m = metrics[idx];
  const chartId = `ic-${title.replace(/\W+/g, "")}-${m.key}`;
  const prevM = metrics[(idx - 1 + count) % count];
  const nextM = metrics[(idx + 1) % count];

  const go = (step) => {
    if (count < 2) return;
    setDir(step > 0 ? "next" : "prev");
    setI((x) => (Math.min(x, count - 1) + step + count) % count);
  };
  const open = () => m.onOpen?.();

  const onTouchStart = (e) => {
    const p = e.touches[0];
    touch.current = { x: p.clientX, y: p.clientY };
  };
  const onTouchEnd = (e) => {
    const start = touch.current;
    touch.current = null;
    if (!start) return;
    const p = e.changedTouches[0];
    const dx = p.clientX - start.x;
    const dy = p.clientY - start.y;
    // Mostly sideways and far enough: a swipe. A vertical drag stays a page scroll.
    if (Math.abs(dx) >= SWIPE_PX && Math.abs(dx) > Math.abs(dy) * 1.5) {
      swipedAt.current = Date.now();
      go(dx < 0 ? 1 : -1);
    }
  };
  const onClick = () => {
    // Some browsers fire a click at the end of a swipe; never treat it as "open".
    if (Date.now() - swipedAt.current < 500) return;
    open();
  };
  const onKeyDown = (e) => {
    if (e.key === "ArrowRight") {
      e.preventDefault();
      go(1);
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      go(-1);
    } else if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      open();
    }
  };

  return (
    <div
      role="button"
      tabIndex={0}
      aria-roledescription="carousel"
      aria-label={`${title}: ${m.label}, ${idx + 1} of ${count}. Left and right arrow keys change the view; Enter opens the details.`}
      onClick={onClick}
      onKeyDown={onKeyDown}
      onTouchStart={onTouchStart}
      onTouchEnd={onTouchEnd}
      className="group bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.035)] p-4 xl:p-5 flex flex-col h-full min-h-[300px] min-w-0 cursor-pointer select-none overflow-hidden transition-shadow hover:shadow-[0_4px_16px_rgba(15,23,42,0.08)] focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/40 [touch-action:pan-y]"
    >
      {/* No outline: a white card on the page's light background, with only a very faint shadow. */}
      {/* The title is never cut off: it wraps above a compact period picker. */}
      <div className="flex items-start justify-between gap-2">
        <h3 className="text-[13px] font-semibold text-ink-900 leading-snug min-w-0 break-words">{title}</h3>
        <label className="relative shrink-0" {...STOP}>
          <select
            value={period}
            onChange={(e) => onPeriod(e.target.value)}
            className="appearance-none text-[11px] text-slate-600 bg-slate-50 hover:bg-slate-100 rounded-full pl-2.5 pr-6 py-1 border-0 cursor-pointer focus:outline-none focus:ring-2 focus:ring-slate-200"
          >
            <option value="week">Weekly</option>
            <option value="month">Monthly</option>
          </select>
          <ChevronDown size={11} className="absolute right-2 top-1/2 -translate-y-1/2 text-slate-500 pointer-events-none" />
        </label>
      </div>

      {/* The current view; it slides in from the side it came from. */}
      <div key={m.key} className={`flex flex-col flex-1 min-h-0 ${dir === "next" ? "kpi-in-next" : "kpi-in-prev"}`}>
        <p className="text-[11px] text-slate-400 mt-3">{m.label}</p>
        <div className="flex items-start gap-0.5 mt-1">
          <span className="text-[28px] xl:text-[34px] leading-none font-light tracking-tight text-ink-900 tabular-nums">{m.display ?? m.value ?? "—"}</span>
          <Trend metric={m} tone={t} />
        </div>

        {/* Who it is: names above the chart (metric.people), with "+N more". */}
        {m.people && (
          <div className="flex flex-wrap gap-1 mt-2.5" aria-label={`${m.label}: names`}>
            {m.people.length === 0 ? (
              <span className="text-[11px] text-slate-400">Nobody right now</span>
            ) : (
              <>
                {m.people.slice(0, MAX_NAMES).map((p) => (
                  <span
                    key={p.name}
                    title={p.hint || p.name}
                    className={`text-[11px] leading-none px-2 py-1 rounded-full whitespace-nowrap ${p.warn ? "bg-amber-50 text-amber-700" : "bg-slate-100 text-slate-600"}`}
                  >
                    {p.name}
                  </span>
                ))}
                {m.people.length > MAX_NAMES && (
                  <span title={m.people.slice(MAX_NAMES).map((p) => p.name).join(", ")} className="text-[11px] leading-none px-2 py-1 rounded-full bg-white text-slate-500 border border-slate-200">
                    +{m.people.length - MAX_NAMES} more
                  </span>
                )}
              </>
            )}
          </div>
        )}

        <div className={`${m.people ? "h-[72px]" : "h-[100px]"} mt-3 -mx-1`}>
          <Chart metric={m} tone={t} id={chartId} />
        </div>

        <p className="text-xs text-slate-500 leading-snug mt-auto pt-3">{m.insight}</p>
      </div>

      <div className="flex items-center justify-between gap-2 mt-3">
        <span className={`text-[11px] font-medium ${t.text} opacity-60 group-hover:opacity-100 transition-opacity`}>{m.onOpen ? "View details" : ""}</span>
        {count > 1 && (
          <div className="flex items-center gap-1" {...STOP}>
            <button
              type="button"
              aria-label={`Previous view: ${prevM.label}`}
              title={prevM.label}
              onClick={() => go(-1)}
              className={`w-8 h-8 rounded-full grid place-items-center ${t.soft} hover:scale-105 active:scale-95 transition-transform`}
            >
              <ChevronLeft size={15} />
            </button>
            <span className="text-[11px] text-slate-500 tabular-nums w-9 text-center" aria-live="polite">
              {idx + 1} / {count}
            </span>
            <button
              type="button"
              aria-label={`Next view: ${nextM.label}`}
              title={nextM.label}
              onClick={() => go(1)}
              className={`w-8 h-8 rounded-full grid place-items-center ${t.soft} hover:scale-105 active:scale-95 transition-transform`}
            >
              <ChevronRight size={15} />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
