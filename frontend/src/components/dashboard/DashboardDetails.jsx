import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { ArrowRight, Film, Image as ImageIcon } from "lucide-react";
import SidePanel from "../SidePanel";
import Modal from "../Modal";
import VisitorEventDetail from "../VisitorEventDetail";
import AlertClip from "../intrusion/AlertClip";
import * as api from "../../api/client";

// The detailed view behind each dashboard card, from the same day's business
// overview the card shows (no extra queries except the staffing minimums and
// evidence clips), with a link to the full feature page for everything else.

const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—");
const mins = (s) => (s === null || s === undefined ? "—" : s < 60 ? "<1 min" : s >= 3600 ? `${Math.floor(s / 3600)}h ${String(Math.round((s % 3600) / 60)).padStart(2, "0")}m` : `${Math.round(s / 60)} min`);
const STATUS_CLS = {
  Present: "badge-success", Late: "badge-warning", "Half day": "badge-warning", Absent: "badge-danger",
  "On leave": "badge-neutral", WFH: "badge-success", Holiday: "badge-neutral", "Weekly off": "badge-neutral", "Not in yet": "badge-neutral",
};

function Tiles({ items }) {
  return (
    <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
      {items.map((t) => (
        <div key={t.label} className="rounded-xl bg-slate-50 px-3 py-2">
          <p className="text-[11px] text-slate-500">{t.label}</p>
          <p className={`text-lg font-semibold tabular-nums ${t.tone === "bad" ? "text-rose-600" : t.tone === "warn" ? "text-amber-600" : "text-ink-900"}`}>{t.value ?? "—"}</p>
          {t.sub && <p className="text-[10px] text-slate-400">{t.sub}</p>}
        </div>
      ))}
    </div>
  );
}

function Block({ title, children, right }) {
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h4>
        {right}
      </div>
      {children}
    </section>
  );
}

function FullPage({ to, label }) {
  return (
    <Link to={to} className="btn-primary text-sm inline-flex items-center gap-1.5">
      {label} <ArrowRight size={14} />
    </Link>
  );
}

// ---------------------------------------------------------------- workforce

export function WorkforceDetail({ b, date, onClose }) {
  const w = b.workforce;
  const [filter, setFilter] = useState("");
  if (!w) return <Unavailable title="Workforce" onClose={onClose} />;
  const shown = w.employees.filter((e) => !filter || (filter === "late" ? e.late : e.status === filter));
  return (
    <SidePanel open onClose={onClose} width="max-w-4xl" title="Workforce">
      <div className="space-y-6 text-sm">
        <Tiles items={[
          { label: "Present", value: `${w.present}/${w.total}`, sub: w.vs_previous ? `${w.vs_previous.previous} the day before` : null },
          { label: "Absent", value: w.absent, tone: w.absent ? "bad" : null },
          { label: "Late arrivals", value: w.late, tone: w.late ? "warn" : null },
          { label: "Attendance", value: `${w.attendance_pct}%`, sub: `${w.on_leave} on leave / WFH` },
        ]} />

        <Block title="Employees" right={
          <select className="input-field !w-auto !py-1 text-xs" value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">All ({w.employees.length})</option>
            {["Present", "Late", "Half day", "Absent", "On leave", "WFH", "Not in yet"].map((s) => <option key={s}>{s}</option>)}
            <option value="late">Arrived late</option>
          </select>
        }>
          <div className="overflow-x-auto rounded-xl ring-1 ring-slate-100 max-h-80 overflow-y-auto">
            <table className="data-table">
              <thead><tr><th>Employee</th><th>Status</th><th>In</th><th>Out</th><th>Location</th></tr></thead>
              <tbody>
                {shown.length === 0 && <tr><td colSpan={5} className="text-center text-slate-400 py-6">Nobody.</td></tr>}
                {shown.map((e) => (
                  <tr key={e.employee_id}>
                    <td><span className="font-medium text-ink-900">{e.name}</span><span className="block text-[11px] text-slate-400">ID {e.employee_id}</span></td>
                    <td><span className={`badge ${STATUS_CLS[e.status] || "badge-neutral"}`}>{e.status}</span></td>
                    <td className="tabular-nums">{clock(e.first_in)}</td>
                    <td className="tabular-nums">{clock(e.last_out)}</td>
                    <td className="text-xs text-slate-500">{e.location || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Block>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-5">
          <Block title={`Repeated late arrivals (over ${w.late_allowance}x this month)`}>
            {w.repeat_late.length === 0 ? <p className="text-xs text-slate-400">Nobody.</p> : (
              <ul className="space-y-1">{w.repeat_late.map((x) => <li key={x.name} className="flex justify-between text-xs"><span>{x.name}</span><span className="text-amber-700">{x.count} times</span></li>)}</ul>
            )}
          </Block>
          <Block title="Face recognition & identification">
            <ul className="space-y-1 text-xs">
              <li className="flex justify-between"><span className="text-slate-500">Employees recognised this day</span><b>{w.recognition.recognised_today}</b></li>
              <li className="flex justify-between"><span className="text-slate-500">Enrolled for recognition</span><b>{w.recognition.enrolled}</b></li>
              <li className="flex justify-between"><span className="text-slate-500">Not enrolled yet</span><b className={w.recognition.not_enrolled ? "text-amber-700" : ""}>{w.recognition.not_enrolled}</b></li>
              <li className="flex justify-between"><span className="text-slate-500">Unmatched faces today</span><b>{w.recognition.unknown_faces ?? "—"}</b></li>
            </ul>
          </Block>
        </div>

        <Block title={`Attendance exceptions (${w.exceptions.length})`}>
          {w.exceptions.length === 0 ? <p className="text-xs text-slate-400">None.</p> : (
            <ul className="divide-y divide-slate-100 rounded-xl ring-1 ring-slate-100 max-h-64 overflow-y-auto">
              {w.exceptions.map((x) => (
                <li key={x.employee_id} className="px-3 py-2 flex items-start gap-3">
                  <span className="font-medium text-ink-900 w-40 shrink-0 truncate">{x.name}</span>
                  <span className="flex flex-wrap gap-1">{x.reasons.map((r) => <span key={r} className="text-[11px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600">{r}</span>)}</span>
                </li>
              ))}
            </ul>
          )}
        </Block>

        <div className="flex flex-wrap gap-2 pt-2 border-t border-slate-100">
          <FullPage to={`/attendance?date=${date}`} label="Open Attendance (reports & history)" />
          <Link to="/face-recognition" className="btn-secondary text-sm">Face Recognition</Link>
        </div>
      </div>
    </SidePanel>
  );
}

// ---------------------------------------------------------------- visitors

export function VisitorsDetail({ b, onClose }) {
  const v = b.visitors;
  if (!v) return <Unavailable title="Visitor Management" onClose={onClose} />;
  const inside = v.visits.filter((x) => x.status === "in");
  return (
    <SidePanel open onClose={onClose} width="max-w-4xl" title="Visitor Management">
      <div className="space-y-6 text-sm">
        <Tiles items={[
          { label: "Visitors", value: v.visitors, sub: `${v.first_time} first-time · ${v.returning} returning` },
          { label: "Inside now", value: v.inside_now },
          { label: "Waiting for host", value: v.waiting_now, tone: v.waiting.some((x) => x.over) ? "warn" : null, sub: `alert after ${v.waiting_threshold_minutes} min` },
          { label: "Average wait", value: mins(v.average_waiting_seconds), sub: `visit ${mins(v.average_duration_seconds)} on average` },
        ]} />

        <Block title="Premises status now">
          {v.inside_now === null ? <p className="text-xs text-slate-400">Only shown for today.</p> : inside.length === 0 ? <p className="text-xs text-slate-400">No visitor inside.</p> : (
            <ul className="space-y-1">
              {inside.map((x) => {
                const wait = v.waiting.find((wt) => wt.visit_id === x.id);
                return (
                  <li key={x.id} className="flex items-center gap-3 text-xs rounded-lg bg-slate-50 px-3 py-2">
                    <span className="font-medium text-ink-900 flex-1">{x.visitor_name}</span>
                    <span className="text-slate-500">to meet {x.host_name}</span>
                    {wait ? (
                      <span className={wait.over ? "text-amber-700 font-medium" : "text-slate-600"}>waiting {mins(wait.waiting_seconds)}</span>
                    ) : (
                      <span className="text-emerald-700">meeting since {clock(x.meeting_start_at)}</span>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </Block>

        <Block title={`Visitor register (${v.visits.length})`}>
          <div className="overflow-x-auto rounded-xl ring-1 ring-slate-100 max-h-96 overflow-y-auto">
            <table className="data-table">
              <thead><tr><th>Visitor</th><th>Host · purpose</th><th>Entry</th><th>Exit</th><th>Waiting</th><th>Duration</th><th>Status</th></tr></thead>
              <tbody>
                {v.visits.length === 0 && <tr><td colSpan={7} className="text-center text-slate-400 py-6">No visitors registered.</td></tr>}
                {v.visits.map((x) => (
                  <tr key={x.id}>
                    <td><span className="font-medium text-ink-900">{x.visitor_name}</span><span className="block text-[11px] text-slate-400">{x.first_visit ? "first visit" : "returning"}</span></td>
                    <td className="text-xs">{x.host_name}<span className="block text-slate-400">{x.purpose}</span></td>
                    <td className="tabular-nums">{clock(x.entry_at)}</td>
                    <td className="tabular-nums">{clock(x.exit_at)}</td>
                    <td>{mins(x.waiting_seconds)}</td>
                    <td>{x.status === "in" ? `${mins(x.elapsed_seconds)} so far` : mins(x.duration_seconds)}</td>
                    <td><span className={`badge ${x.status === "in" ? "badge-success" : x.status === "pending" ? "badge-warning" : "badge-neutral"}`}>{x.status === "in" ? "IN" : x.status === "out" ? "OUT" : "Pending"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Block>

        <div className="flex flex-wrap gap-2 pt-2 border-t border-slate-100">
          <FullPage to="/visitors" label="Open Visitor Management (profiles & reports)" />
          <Link to="/visitors?tab=analytics" className="btn-secondary text-sm">Visitor analytics</Link>
        </div>
      </div>
    </SidePanel>
  );
}

// ---------------------------------------------------------------- footfall & staffing

export function FootfallDetail({ b, onClose, onChanged }) {
  const f = b.footfall;
  const [editing, setEditing] = useState(false);
  const [targets, setTargets] = useState({});
  const [error, setError] = useState("");
  useEffect(() => {
    if (b.areas) setTargets(Object.fromEntries(b.areas.map((a) => [a.area, a.target || ""])));
  }, [b.areas]);

  async function saveTargets() {
    try {
      await api.staffingApi.setTargets(Object.fromEntries(Object.entries(targets).map(([k, n]) => [k, Number(n) || 0])));
      setEditing(false);
      setError("");
      onChanged();
    } catch (e) {
      setError(e.message);
    }
  }

  return (
    <SidePanel open onClose={onClose} width="max-w-4xl" title="Footfall & Staffing">
      <div className="space-y-6 text-sm">
        <Tiles items={[
          { label: "Entries", value: f?.entries, sub: f?.vs_previous ? `${f.vs_previous.previous} the day before` : null },
          { label: "Employees / visitors", value: f ? `${f.employee_entries} / ${f.visitor_entries}` : null },
          { label: "Peak hour", value: f?.peak_label || "—" },
          { label: "Staff seen now", value: b.staff_now?.count, sub: b.staff_now ? `recognised in the last ${b.staff_now.window_minutes} min` : "today only" },
        ]} />

        {f && (
          <Block title="Entries by hour">
            <div className="h-48">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={f.hourly}>
                  <CartesianGrid vertical={false} stroke="#eef0f5" />
                  <XAxis dataKey="hour" tickFormatter={(h) => `${h % 12 || 12}${h < 12 ? "a" : "p"}`} tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} />
                  <YAxis allowDecimals={false} tick={{ fontSize: 11, fill: "#94a3b8" }} axisLine={false} tickLine={false} width={26} />
                  <Tooltip contentStyle={{ fontSize: 12, borderRadius: 8 }} cursor={{ fill: "#f8fafc" }} />
                  <Bar dataKey="employees" name="Employees" stackId="a" fill="#818cf8" maxBarSize={24} />
                  <Bar dataKey="visitors" name="Visitors / others" stackId="a" fill="#34d399" radius={[4, 4, 0, 0]} maxBarSize={24} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </Block>
        )}

        {f?.gates && (
          <Block title="Gate-wise entries today">
            <ul className="space-y-1">
              {f.gates.map((g) => (
                <li key={g.name} className="flex justify-between text-xs rounded-lg bg-slate-50 px-3 py-2">
                  <span>{g.name}{!g.entry_line && <span className="text-amber-700"> · no entry line (counts everyone in its area)</span>}</span>
                  <b>{g.today}</b>
                </li>
              ))}
            </ul>
          </Block>
        )}

        <Block title="Staff by area (right now)" right={b.areas && (
          <button type="button" className="text-xs text-brand-600" onClick={() => setEditing((x) => !x)}>{editing ? "Cancel" : "Set minimum staff"}</button>
        )}>
          {!b.areas ? <p className="text-xs text-slate-400">Live staffing is only shown for today.</p> : b.areas.length === 0 ? (
            <p className="text-xs text-slate-400">No camera has a location yet. Set camera locations from an employee's “Verify Presence on Camera” screen.</p>
          ) : (
            <div className="overflow-x-auto rounded-xl ring-1 ring-slate-100">
              <table className="data-table">
                <thead><tr><th>Area</th><th>Cameras</th><th>Employees now</th><th>Minimum</th><th>Status</th></tr></thead>
                <tbody>
                  {b.areas.map((a) => (
                    <tr key={a.area}>
                      <td className="font-medium text-ink-900">{a.area}</td>
                      <td className="text-xs text-slate-500">{a.cameras.join(", ") || "—"}</td>
                      <td title={a.names.join(", ")}>{a.employees}{a.names.length > 0 && <span className="block text-[11px] text-slate-400 truncate max-w-56">{a.names.join(", ")}</span>}</td>
                      <td>
                        {editing ? (
                          <input type="number" min={0} max={500} className="input-field !w-20 !py-1 text-xs" value={targets[a.area] ?? ""} onChange={(e) => setTargets((t) => ({ ...t, [a.area]: e.target.value }))} />
                        ) : a.target ?? <span className="text-slate-400">not set</span>}
                      </td>
                      <td>{a.target ? (a.short_by ? <span className="badge badge-danger">short by {a.short_by}</span> : <span className="badge badge-success">covered</span>) : <span className="text-slate-400 text-xs">—</span>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          {editing && (
            <div className="flex items-center gap-2 justify-end">
              {error && <span className="text-xs text-danger-500 mr-auto">{error}</span>}
              <button type="button" className="btn-primary text-xs" onClick={saveTargets}>Save minimums</button>
            </div>
          )}
          <p className="text-[11px] text-slate-400">Employees recognised by face on each area's cameras in the last few minutes. Unrecognised people are not counted.</p>
        </Block>

        {b.understaffed?.length > 0 && (
          <Block title="Staffing gaps">
            <ul className="space-y-1">{b.understaffed.map((u) => <li key={u.area} className="text-xs rounded-lg bg-rose-50 text-rose-800 px-3 py-2"><b>{u.area}</b>: {u.detail}</li>)}</ul>
          </Block>
        )}

        {f && (
          <Block title="Last 7 days">
            <div className="overflow-x-auto rounded-xl ring-1 ring-slate-100">
              <table className="data-table">
                <thead><tr><th>Day</th><th>Entries</th><th>Employees</th><th>Visitors / others</th></tr></thead>
                <tbody>{f.trend.map((d) => <tr key={d.day}><td>{new Date(`${d.day}T12:00:00`).toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })}</td><td>{d.entries}</td><td>{d.employees}</td><td>{d.visitors}</td></tr>)}</tbody>
              </table>
            </div>
          </Block>
        )}

        <div className="flex flex-wrap gap-2 pt-2 border-t border-slate-100">
          <FullPage to="/visitors?tab=footfall" label="Open Footfall (reports & cameras)" />
          <Link to="/staff" className="btn-secondary text-sm">Staff counting</Link>
        </div>
      </div>
    </SidePanel>
  );
}

// ---------------------------------------------------------------- security

export function SecurityDetail({ b, onClose }) {
  const s = b.security;
  const [clip, setClip] = useState(null);       // visitor-area event id (clip)
  const [alertClip, setAlertClip] = useState(null); // intrusion / loitering alert with an evidence clip
  const [picture, setPicture] = useState(null); // {url|null, error, title}

  async function openPicture(x) {
    setPicture({ title: `${x.event} · ${x.camera} · ${x.time}`, url: null, error: "" });
    try {
      setPicture({ title: `${x.event} · ${x.camera} · ${x.time}`, url: await api.fetchAlertSnapshotObjectUrl(x.id), error: "" });
    } catch {
      setPicture({ title: `${x.event} · ${x.camera} · ${x.time}`, url: null, error: "This picture is no longer available." });
    }
  }
  useEffect(() => () => picture?.url && URL.revokeObjectURL(picture.url), [picture?.url]);

  return (
    <SidePanel open onClose={onClose} width="max-w-4xl" title="Security & Safety">
      <div className="space-y-6 text-sm">
        <Tiles items={[
          { label: "Active alerts", value: s.active, tone: s.high_or_critical ? "bad" : s.active ? "warn" : null, sub: `${s.high_or_critical} high or critical` },
          { label: "Unknown persons", value: s.unknown_persons, tone: s.unknown_persons ? "warn" : null },
          ...(s.intrusion == null
            ? [{ label: "Intrusion / loitering", value: "Not set up", sub: "not included in this installation" }]
            : [
              // Intrusion = restricted-area / entry-gate alerts; loitering = stayed too long (intrusion.py).
              { label: "Intrusion", value: s.restricted_violations, tone: s.restricted_violations ? "bad" : null,
                sub: `${s.restricted_entries ?? 0} detections logged · ${s.intrusion_with_clip} with video` },
              { label: "Loitering", value: s.loitering, tone: s.loitering ? "warn" : null, sub: `${s.intrusion_open} intrusion / loitering open` },
            ]),
        ]} />

        <Block title={`Unresolved alerts (${s.open.length})`}>
          {s.open.length === 0 ? <p className="text-xs text-slate-400">None.</p> : (
            <ul className="space-y-1">
              {s.open.map((a) => (
                <li key={a.id} className="flex items-center gap-3 text-xs rounded-lg bg-slate-50 px-3 py-2">
                  <span className={`badge ${a.severity === "Critical" || a.severity === "High" ? "badge-danger" : "badge-warning"}`}>{a.severity}</span>
                  <span className="font-medium text-ink-900">{a.event}</span>
                  <span className="text-slate-500 flex-1 truncate">{a.camera} · {a.message}</span>
                  <span className="text-slate-500 tabular-nums">{a.time}</span>
                </li>
              ))}
            </ul>
          )}
        </Block>

        <Block title={`Recent incidents (${s.incidents.length})`}>
          {s.incidents.length === 0 ? <p className="text-xs text-slate-400">No incidents on this day.</p> : (
            <div className="overflow-x-auto rounded-xl ring-1 ring-slate-100 max-h-96 overflow-y-auto">
              <table className="data-table">
                <thead><tr><th>Time</th><th>Event</th><th>Camera</th><th>Status</th><th>Evidence</th></tr></thead>
                <tbody>
                  {s.incidents.map((x) => (
                    <tr key={`${x.kind}-${x.id}`}>
                      <td className="tabular-nums whitespace-nowrap">{x.time}</td>
                      <td>{x.event}{x.message && <span className="block text-[11px] text-slate-400 truncate max-w-64">{x.message}</span>}</td>
                      <td>{x.camera}</td>
                      <td className="text-xs">{x.status}</td>
                      <td>
                        {x.clip ? (
                          <button type="button" className="text-brand-600 text-xs inline-flex items-center gap-1" onClick={() => setClip(x.id)}><Film size={12} /> Play clip</button>
                        ) : x.alert_clip ? (
                          <button type="button" className="text-brand-600 text-xs inline-flex items-center gap-1" onClick={() => setAlertClip(x)}><Film size={12} /> Play clip</button>
                        ) : x.picture ? (
                          <button type="button" className="text-brand-600 text-xs inline-flex items-center gap-1" onClick={() => openPicture(x)}><ImageIcon size={12} /> Picture</button>
                        ) : <span className="text-slate-300">—</span>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="text-[11px] text-slate-400">
            Unknown people in the visitor seating area have ~10 s clips (kept 24 h); intrusion and loitering alerts have ~10 s evidence clips; other alerts keep a picture.
          </p>
        </Block>

        <div className="flex flex-wrap gap-2 pt-2 border-t border-slate-100">
          <FullPage to="/alerts" label="Open Alerts & Events (history)" />
          {s.intrusion != null && <FullPage to="/intrusion" label="Open Intrusion" />}
          <Link to="/visitors?tab=zones" className="btn-secondary text-sm">Zones &amp; areas</Link>
        </div>
      </div>
      {clip !== null && <VisitorEventDetail key={clip} eventId={clip} onClose={() => setClip(null)} onDeleted={() => setClip(null)} />}
      <Modal open={!!alertClip} onClose={() => setAlertClip(null)} title={alertClip ? `${alertClip.event}${alertClip.zone ? ` · ${alertClip.zone}` : ""} · ${alertClip.camera} · ${alertClip.time}` : ""} width="max-w-2xl">
        {alertClip && <AlertClip alertId={alertClip.id} clipStatus="ready" label={`${alertClip.event} ${alertClip.zone || ""}`} />}
      </Modal>
      <Modal open={!!picture} onClose={() => setPicture(null)} title={picture?.title || ""} width="max-w-2xl">
        {picture?.url ? <img src={picture.url} alt="" className="w-full rounded-lg" /> : <p className="text-sm text-slate-500">{picture?.error || "Loading…"}</p>}
      </Modal>
    </SidePanel>
  );
}

function Unavailable({ title, onClose }) {
  return (
    <SidePanel open onClose={onClose} title={title}>
      <p className="text-sm text-slate-500">This feature is not set up in this installation.</p>
    </SidePanel>
  );
}
