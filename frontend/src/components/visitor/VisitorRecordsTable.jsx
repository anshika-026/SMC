import { useEffect, useMemo, useState } from "react";
import { Download, FileSpreadsheet, Search } from "lucide-react";
import * as api from "../../api/client";
import { clock, dur, MediaImage, niceDate, StatusPill } from "./shared";

// The main visitor records table: one row per visit, newest first, with the
// filters the KPI cards also drive. Name -> profile; row -> visit panel.

export default function VisitorRecordsTable({ filters, setFilters, rows, error, onOpenVisit, onOpenVisitor }) {
  const [hosts, setHosts] = useState([]);
  const [busy, setBusy] = useState("");
  const [exportError, setExportError] = useState("");

  useEffect(() => {
    api.vr.hosts().then((d) => setHosts(d.hosts)).catch(() => {});
  }, []);
  const departments = useMemo(() => [...new Set(hosts.map((h) => h.department).filter(Boolean))].sort(), [hosts]);
  const set = (k) => (e) => setFilters((f) => ({ ...f, [k]: e.target.value }));

  async function download(format) {
    setBusy(format);
    setExportError("");
    try {
      await api.vr.download(format, apiFilters(filters));
    } catch (e) {
      setExportError(e.message);
    } finally {
      setBusy("");
    }
  }

  const active = ["status", "type", "host", "department", "q"].some((k) => filters[k]);

  return (
    <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)]">
      <div className="p-4 border-b border-slate-100 space-y-3">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div>
            <h3 className="text-sm font-semibold text-ink-900">Visitor records</h3>
            <p className="text-xs text-slate-500">{rows ? `${rows.length} visit${rows.length === 1 ? "" : "s"}` : "Loading…"} · click a name for the visitor's profile, a row for the visit</p>
          </div>
          <div className="flex gap-2">
            <button type="button" className="btn-secondary text-xs inline-flex items-center gap-1.5" disabled={!!busy} onClick={() => download("csv")}>
              <Download size={13} /> {busy === "csv" ? "Exporting…" : "CSV"}
            </button>
            <button type="button" className="btn-secondary text-xs inline-flex items-center gap-1.5" disabled={!!busy} onClick={() => download("xlsx")}>
              <FileSpreadsheet size={13} /> {busy === "xlsx" ? "Exporting…" : "Excel"}
            </button>
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 xl:grid-cols-8 gap-2 text-xs">
          <label className="col-span-2 relative">
            <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
            <input className="input-field text-xs" style={{ paddingLeft: "2rem" }} placeholder="Name, Visitor ID, Visit ID, company" value={filters.q} onChange={set("q")} />
          </label>
          <input type="date" className="input-field text-xs" title="From" value={filters.from} onChange={set("from")} />
          <input type="date" className="input-field text-xs" title="To" value={filters.to} onChange={set("to")} />
          <select className="input-field text-xs" value={filters.department} onChange={set("department")}>
            <option value="">All depts.</option>
            {departments.map((d) => <option key={d}>{d}</option>)}
          </select>
          <select className="input-field text-xs" value={filters.host} onChange={set("host")}>
            <option value="">All hosts</option>
            {hosts.map((h) => <option key={h.employee_id} value={h.employee_id}>{h.name}</option>)}
          </select>
          <select className="input-field text-xs" value={filters.status} onChange={set("status")}>
            <option value="">All statuses</option>
            <option value="in">IN</option>
            <option value="out">OUT</option>
            <option value="pending">Pending</option>
            <option value="cancelled">Cancelled</option>
          </select>
          <select className="input-field text-xs" value={filters.type} onChange={set("type")}>
            <option value="">All visitors</option>
            <option value="first">First-time visitors</option>
            <option value="repeat">Repeat visitors</option>
          </select>
        </div>
        {active && (
          <button type="button" className="text-xs text-brand-600" onClick={() => setFilters((f) => ({ ...f, status: "", type: "", host: "", department: "", q: "" }))}>
            Clear filters
          </button>
        )}
        {exportError && <p className="text-xs text-danger-500">{exportError}</p>}
      </div>

      {error && <p className="text-sm text-danger-500 p-4">{error}</p>}
      <div className="overflow-x-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th>Visitor</th>
              <th>Snapshot</th>
              <th>Purpose</th>
              <th>Person to meet</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>Waiting</th>
              <th>Duration</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows && rows.length === 0 && (
              <tr>
                <td colSpan={9} className="text-center text-slate-400 py-10">No visits match these filters.</td>
              </tr>
            )}
            {(rows || []).map((r) => (
              <tr key={r.id} className="cursor-pointer hover:bg-slate-50/70" onClick={() => onOpenVisit(r.id)}>
                <td className="min-w-44">
                  <button type="button" className="font-medium text-ink-900 hover:text-brand-600 text-left"
                    onClick={(e) => { e.stopPropagation(); onOpenVisitor(r.visitor_id); }}>
                    {r.visitor_name}
                  </button>
                  <div className="text-[11px] text-slate-400">
                    {r.visitor_code} · {r.first_visit ? "first visit" : `${r.visits_this_month} this month`}
                  </div>
                </td>
                <td>
                  {r.snapshot_id ? <MediaImage mediaId={r.snapshot_id} className="w-9 h-9 rounded-full" />
                    : <span className="text-[11px] text-slate-400">{r.snapshot_consent ? "None" : "Declined"}</span>}
                </td>
                <td className="max-w-40 truncate">{r.purpose}</td>
                <td>
                  {r.host_name}
                  {r.host_department && <div className="text-[11px] text-slate-400">{r.host_department}</div>}
                </td>
                <td className="whitespace-nowrap">
                  {clock(r.entry_at)}
                  <div className="text-[11px] text-slate-400">{niceDate(r.visit_date)}</div>
                </td>
                <td>{clock(r.exit_at)}</td>
                <td>{dur(r.waiting_seconds)}</td>
                <td className="whitespace-nowrap">
                  {r.status === "in" ? <span className="text-emerald-700">{dur(r.elapsed_seconds)} so far</span> : dur(r.duration_seconds)}
                </td>
                <td><StatusPill status={r.status} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// The page's filter state -> the API's query parameters.
export function apiFilters(f) {
  return { from: f.from, to: f.to, status: f.status, host: f.host, department: f.department, type: f.type, q: f.q };
}
