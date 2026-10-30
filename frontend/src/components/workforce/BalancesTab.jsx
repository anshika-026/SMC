import { useCallback, useEffect, useState } from "react";
import * as api from "../../api/client";
import Modal from "../Modal";

// Employee-wise leave balances for a year (allotted + carried forward +
// adjustment, used = approved, pending, remaining), editable allocations,
// default allocation for new years, and the leave types themselves.

export default function BalancesTab() {
  const [year, setYear] = useState(new Date().getFullYear());
  const [data, setData] = useState(null);
  const [types, setTypes] = useState([]);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [edit, setEdit] = useState(null);     // {employee, balance}
  const [typeForm, setTypeForm] = useState(null);

  const load = useCallback(() => {
    api.hr
      .balances(year)
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(e.message));
    api.hr.leaveTypes().then((d) => setTypes(d.leave_types)).catch(() => {});
  }, [year]);

  useEffect(() => {
    load();
  }, [load]);

  async function allocate() {
    try {
      const r = await api.hr.allocateDefaults(year);
      setNotice(r.created ? `Gave ${r.created} default allocation(s) for ${year} (existing ones unchanged).` : `Everyone already has ${year} allocations.`);
      load();
    } catch (e) {
      setError(e.message);
    }
  }

  async function saveEdit(e) {
    e.preventDefault();
    try {
      await api.hr.setAllocation({
        employee_id: edit.employee.employee_id,
        leave_type: edit.balance.leave_type,
        year,
        allotted: Number(edit.allotted),
        carried_forward: Number(edit.carried_forward),
        adjustment: Number(edit.adjustment),
        note: edit.note,
      });
      setEdit(null);
      load();
    } catch (err) {
      setEdit((x) => ({ ...x, error: err.message }));
    }
  }

  async function saveType(e) {
    e.preventDefault();
    try {
      await api.hr.saveLeaveType({ ...typeForm, annual_days: Number(typeForm.annual_days), carry_forward_max: Number(typeForm.carry_forward_max), expires_months: Number(typeForm.expires_months) });
      setTypeForm(null);
      load();
    } catch (err) {
      setTypeForm((x) => ({ ...x, error: err.message }));
    }
  }

  const codes = types.filter((t) => t.active).map((t) => t.code);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <select className="input-field !w-auto" value={year} onChange={(e) => setYear(Number(e.target.value))}>
          {[0, 1, -1].map((o) => new Date().getFullYear() + o).sort().map((y) => (
            <option key={y}>{y}</option>
          ))}
        </select>
        <button type="button" className="btn-secondary text-sm" onClick={allocate}>
          Give default allocation for {year}
        </button>
        <button type="button" className="btn-secondary text-sm ml-auto" onClick={() => setTypeForm({ code: "", name: "", annual_days: 0, carry_forward_max: 0, expires_months: 0, active: true })}>
          Add leave type
        </button>
      </div>
      {notice && <p className="text-xs text-success-600">{notice}</p>}
      {error && <p className="text-sm text-danger-500">{error}</p>}

      <div className="card p-4">
        <h3 className="text-sm font-semibold text-ink-900 mb-2">Leave types</h3>
        <div className="flex flex-wrap gap-2">
          {types.map((t) => (
            <button key={t.code} type="button" onClick={() => setTypeForm({ ...t, active: !!t.active })} className={`badge ${t.active ? "badge-success" : "badge-neutral"}`}>
              {t.code} · {t.name} · {t.annual_days}/yr{t.carry_forward_max ? ` · carry ${t.carry_forward_max}` : ""}
              {t.expires_months ? ` · expires after ${t.expires_months} mo` : ""}
            </button>
          ))}
        </div>
      </div>

      <div className="card overflow-x-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th>Employee</th>
              {codes.map((c) => (
                <th key={c}>{c} left / total</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {(data?.employees || []).map((emp) => (
              <tr key={emp.employee_id}>
                <td>
                  <span className="font-medium text-ink-900">{emp.name}</span>
                  <span className="block text-[11px] text-slate-400">{emp.department || "-"}</span>
                </td>
                {codes.map((c) => {
                  const b = emp.balances.find((x) => x.leave_type === c);
                  if (!b) return <td key={c}>-</td>;
                  return (
                    <td key={c}>
                      <button type="button" className="text-left hover:underline" onClick={() => setEdit({ employee: emp, balance: b, allotted: b.allotted, carried_forward: b.carried_forward, adjustment: b.adjustment, note: b.note || "" })}>
                        <span className={`font-medium ${b.remaining < 0 ? "text-danger-500" : "text-ink-900"}`}>{b.remaining}</span>
                        <span className="text-slate-400"> / {b.total}</span>
                        <span className="block text-[11px] text-slate-400">
                          used {b.used}
                          {b.pending ? ` · pending ${b.pending}` : ""}
                        </span>
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-slate-400">Click a balance to change the allocation. Every change is audited.</p>

      <Modal open={!!edit} onClose={() => setEdit(null)} title={edit ? `${edit.employee.name} · ${edit.balance.name} · ${year}` : ""}>
        {edit && (
          <form onSubmit={saveEdit} className="space-y-3 text-sm">
            {[
              ["allotted", "Allotted days"],
              ["carried_forward", "Carried forward"],
              ["adjustment", "Adjustment (+/-)"],
            ].map(([k, label]) => (
              <label key={k} className="block">
                <span className="text-xs text-slate-500">{label}</span>
                <input type="number" step="0.5" className="input-field mt-1" value={edit[k]} onChange={(e) => setEdit((x) => ({ ...x, [k]: e.target.value }))} />
              </label>
            ))}
            <label className="block">
              <span className="text-xs text-slate-500">Note (why)</span>
              <input className="input-field mt-1" value={edit.note} onChange={(e) => setEdit((x) => ({ ...x, note: e.target.value }))} />
            </label>
            <p className="text-xs text-slate-500">
              Used {edit.balance.used} · pending {edit.balance.pending}
            </p>
            {edit.error && <p className="text-xs text-danger-500">{edit.error}</p>}
            <div className="flex justify-end gap-2">
              <button type="button" className="btn-secondary text-sm" onClick={() => setEdit(null)}>
                Cancel
              </button>
              <button type="submit" className="btn-primary text-sm">
                Save
              </button>
            </div>
          </form>
        )}
      </Modal>

      <Modal open={!!typeForm} onClose={() => setTypeForm(null)} title="Leave type">
        {typeForm && (
          <form onSubmit={saveType} className="space-y-3 text-sm">
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="text-xs text-slate-500">Code</span>
                <input className="input-field mt-1" value={typeForm.code} onChange={(e) => setTypeForm((x) => ({ ...x, code: e.target.value }))} required />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500">Name</span>
                <input className="input-field mt-1" value={typeForm.name} onChange={(e) => setTypeForm((x) => ({ ...x, name: e.target.value }))} required />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500">Days a year</span>
                <input type="number" step="0.5" className="input-field mt-1" value={typeForm.annual_days} onChange={(e) => setTypeForm((x) => ({ ...x, annual_days: e.target.value }))} />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500">Carry forward up to</span>
                <input type="number" step="0.5" className="input-field mt-1" value={typeForm.carry_forward_max} onChange={(e) => setTypeForm((x) => ({ ...x, carry_forward_max: e.target.value }))} />
              </label>
              <label className="block">
                <span className="text-xs text-slate-500">Carried days expire after (months, 0 = never)</span>
                <input type="number" className="input-field mt-1" value={typeForm.expires_months} onChange={(e) => setTypeForm((x) => ({ ...x, expires_months: e.target.value }))} />
              </label>
              <label className="flex items-center gap-2 mt-6">
                <input type="checkbox" checked={typeForm.active} onChange={(e) => setTypeForm((x) => ({ ...x, active: e.target.checked }))} /> Active
              </label>
            </div>
            {typeForm.error && <p className="text-xs text-danger-500">{typeForm.error}</p>}
            <div className="flex justify-end gap-2">
              <button type="button" className="btn-secondary text-sm" onClick={() => setTypeForm(null)}>
                Cancel
              </button>
              <button type="submit" className="btn-primary text-sm">
                Save
              </button>
            </div>
          </form>
        )}
      </Modal>
    </div>
  );
}
