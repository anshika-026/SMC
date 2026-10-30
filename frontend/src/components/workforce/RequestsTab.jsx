import { useCallback, useEffect, useState } from "react";
import { Plus } from "lucide-react";
import * as api from "../../api/client";
import Modal from "../Modal";
import { todayIso } from "./shared";

// Leave and work-from-home requests, entered and decided by HR/admin on the
// employee's behalf (employees have no login yet). Every request and decision
// is kept as history and audited (backend/app/workforce.py).

const EMPTY = { employee_id: "", kind: "leave", leave_type: "CL", day_from: todayIso(), day_to: todayIso(), half: "full", reason: "", approve: false };
const STATUS_BADGE = { pending: "badge-warning", approved: "badge-success", rejected: "badge-danger", cancelled: "badge-neutral" };

export default function RequestsTab() {
  const [rows, setRows] = useState(null);
  const [status, setStatus] = useState("pending");
  const [employees, setEmployees] = useState([]);
  const [types, setTypes] = useState([]);
  const [form, setForm] = useState(null);
  const [error, setError] = useState("");
  const [formError, setFormError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api.hr
      .requests({ status })
      .then((d) => {
        setRows(d.requests);
        setError("");
      })
      .catch((e) => setError(e.message));
  }, [status]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    api.hr.balances().then((d) => setEmployees(d.employees)).catch(() => {});
    api.hr.leaveTypes().then((d) => setTypes(d.leave_types.filter((t) => t.active))).catch(() => {});
  }, []);

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setFormError("");
    try {
      const body = { ...form, day_to: form.half === "full" ? form.day_to : form.day_from };
      if (body.kind === "wfh") delete body.leave_type;
      await api.hr.addRequest(body);
      setForm(null);
      load();
    } catch (err) {
      setFormError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function decide(r, decision) {
    const note = decision === "approved" ? "" : window.prompt(`Reason for ${decision === "rejected" ? "rejecting" : "cancelling"} (optional)`) ?? null;
    if (note === null) return;
    try {
      await api.hr.decide(r.id, decision, note);
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {["pending", "approved", "rejected", "cancelled", ""].map((s) => (
          <button
            key={s || "all"}
            type="button"
            onClick={() => setStatus(s)}
            className={`px-3 py-1.5 rounded-full text-sm font-medium ${status === s ? "bg-brand-500 text-white" : "bg-white border border-border-200 text-slate-600"}`}
          >
            {s ? s[0].toUpperCase() + s.slice(1) : "All"}
          </button>
        ))}
        <button type="button" onClick={() => { setForm({ ...EMPTY, employee_id: employees[0]?.employee_id || "" }); setFormError(""); }} className="btn-primary text-sm ml-auto inline-flex items-center gap-1.5">
          <Plus size={14} /> New request
        </button>
      </div>
      {error && <p className="text-sm text-danger-500">{error}</p>}

      <div className="card overflow-x-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Employee</th>
              <th>Type</th>
              <th>Dates</th>
              <th>Days</th>
              <th>Reason</th>
              <th>Status</th>
              <th>Decided</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {rows && rows.length === 0 && (
              <tr>
                <td colSpan={9} className="text-center text-slate-400 py-8">
                  No {status || ""} requests.
                </td>
              </tr>
            )}
            {(rows || []).map((r) => (
              <tr key={r.id}>
                <td className="text-slate-400">{r.id}</td>
                <td className="font-medium text-ink-900">{r.employee_name}</td>
                <td>{r.kind === "wfh" ? "Work from home" : `Leave · ${r.leave_type}`}</td>
                <td>
                  {r.day_from}
                  {r.day_to !== r.day_from ? ` → ${r.day_to}` : ""}
                  {r.half !== "full" ? ` (${r.half} half)` : ""}
                </td>
                <td>{r.days}</td>
                <td className="text-xs text-slate-500 max-w-48 truncate">{r.reason || "-"}</td>
                <td>
                  <span className={`badge ${STATUS_BADGE[r.status]}`}>{r.status}</span>
                </td>
                <td className="text-xs text-slate-500">
                  {r.decided_by ? `${r.decided_by}${r.decision_note ? ` · ${r.decision_note}` : ""}` : "-"}
                </td>
                <td className="whitespace-nowrap">
                  {r.status === "pending" && (
                    <>
                      <button type="button" className="text-xs text-success-600 font-medium mr-3" onClick={() => decide(r, "approved")}>
                        Approve
                      </button>
                      <button type="button" className="text-xs text-danger-500 font-medium mr-3" onClick={() => decide(r, "rejected")}>
                        Reject
                      </button>
                    </>
                  )}
                  {(r.status === "pending" || r.status === "approved") && (
                    <button type="button" className="text-xs text-slate-500" onClick={() => decide(r, "cancelled")}>
                      Cancel
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Modal open={!!form} onClose={() => setForm(null)} title="New leave / WFH request">
        {form && (
          <form onSubmit={submit} className="space-y-3 text-sm">
            <label className="block">
              <span className="text-xs text-slate-500">Employee</span>
              <select className="input-field mt-1" value={form.employee_id} onChange={set("employee_id")} required>
                {employees.map((e) => (
                  <option key={e.employee_id} value={e.employee_id}>
                    {e.name} ({e.employee_id})
                  </option>
                ))}
              </select>
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="text-xs text-slate-500">Request</span>
                <select className="input-field mt-1" value={form.kind} onChange={set("kind")}>
                  <option value="leave">Leave</option>
                  <option value="wfh">Work from home</option>
                </select>
              </label>
              {form.kind === "leave" && (
                <label className="block">
                  <span className="text-xs text-slate-500">Leave type</span>
                  <select className="input-field mt-1" value={form.leave_type} onChange={set("leave_type")}>
                    {types.map((t) => (
                      <option key={t.code} value={t.code}>
                        {t.name}
                      </option>
                    ))}
                  </select>
                </label>
              )}
            </div>
            <label className="block">
              <span className="text-xs text-slate-500">Duration</span>
              <select className="input-field mt-1" value={form.half} onChange={set("half")}>
                <option value="full">Full day(s)</option>
                <option value="first">First half (morning)</option>
                <option value="second">Second half (afternoon)</option>
              </select>
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="text-xs text-slate-500">{form.half === "full" ? "From" : "Day"}</span>
                <input type="date" className="input-field mt-1" value={form.day_from} onChange={set("day_from")} required />
              </label>
              {form.half === "full" && (
                <label className="block">
                  <span className="text-xs text-slate-500">To</span>
                  <input type="date" className="input-field mt-1" value={form.day_to} min={form.day_from} onChange={set("day_to")} required />
                </label>
              )}
            </div>
            <label className="block">
              <span className="text-xs text-slate-500">Reason</span>
              <input className="input-field mt-1" value={form.reason} onChange={set("reason")} />
            </label>
            <label className="flex items-center gap-2">
              <input type="checkbox" checked={form.approve} onChange={set("approve")} /> Approve now
            </label>
            {formError && <p className="text-xs text-danger-500">{formError}</p>}
            <div className="flex justify-end gap-2">
              <button type="button" className="btn-secondary text-sm" onClick={() => setForm(null)}>
                Cancel
              </button>
              <button type="submit" className="btn-primary text-sm" disabled={busy || !form.employee_id}>
                {busy ? "Saving…" : "Save request"}
              </button>
            </div>
          </form>
        )}
      </Modal>
    </div>
  );
}
