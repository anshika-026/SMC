import { useCallback, useEffect, useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import * as api from "../../api/client";
import Modal from "../Modal";

// Company holiday calendar (backend/app/workforce.py): organisation-wide or
// for one site. Holidays are not working days: nobody is absent on them and
// leave over them isn't counted.

export default function HolidaysPanel() {
  const [year, setYear] = useState(new Date().getFullYear());
  const [rows, setRows] = useState(null);
  const [sites, setSites] = useState([]);
  const [form, setForm] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(() => {
    api.hr
      .holidays(year)
      .then((d) => {
        setRows(d.holidays);
        setError("");
      })
      .catch((e) => setError(e.message));
  }, [year]);

  useEffect(() => {
    load();
    api.getSites?.().then((s) => setSites((s || []).map((x) => x.name || x.label || x).filter(Boolean))).catch(() => {});
  }, [load]);

  async function save(e) {
    e.preventDefault();
    try {
      const body = { day: form.day, name: form.name, site: form.site || null };
      if (form.id) await api.hr.editHoliday(form.id, body);
      else await api.hr.addHoliday(body);
      setForm(null);
      load();
    } catch (err) {
      setForm((f) => ({ ...f, error: err.message }));
    }
  }

  async function remove(h) {
    if (!window.confirm(`Delete ${h.name} (${h.day})?`)) return;
    try {
      await api.hr.deleteHoliday(h.id);
      load();
    } catch (err) {
      setError(err.message);
    }
  }

  const today = new Date().toISOString().slice(0, 10);

  return (
    <div className="card p-6">
      <div className="flex flex-wrap items-center justify-between gap-2 mb-5">
        <h2 className="text-base font-semibold text-ink-900">Holiday Calendar</h2>
        <div className="flex items-center gap-2">
          <select className="input-field !w-auto" value={year} onChange={(e) => setYear(Number(e.target.value))}>
            {[-1, 0, 1].map((o) => new Date().getFullYear() + o).map((y) => (
              <option key={y}>{y}</option>
            ))}
          </select>
          <button type="button" onClick={() => setForm({ day: `${year}-01-01`, name: "", site: "" })} className="btn-primary text-sm inline-flex items-center gap-1.5">
            <Plus size={14} /> Add holiday
          </button>
        </div>
      </div>
      {error && <p className="text-sm text-danger-500 mb-3">{error}</p>}
      {rows && rows.length === 0 && <p className="text-sm text-slate-400 py-6 text-center">No holidays for {year} yet.</p>}
      <ul className="divide-y divide-[#f1f2f7]">
        {(rows || []).map((h) => (
          <li key={h.id} className="flex items-center justify-between gap-3 py-3 text-sm">
            <span>
              <span className={`font-medium ${h.day < today ? "text-slate-400" : "text-ink-900"}`}>{h.name}</span>
              <span className="block text-xs text-slate-400">{h.site ? `Only ${h.site}` : "Whole organisation"}</span>
            </span>
            <span className="flex items-center gap-3">
              <span className="text-slate-500">
                {new Date(`${h.day}T12:00:00`).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" })}
              </span>
              <button type="button" title="Edit" className="text-slate-400 hover:text-ink-900" onClick={() => setForm({ ...h, site: h.site || "" })}>
                <Pencil size={14} />
              </button>
              <button type="button" title="Delete" className="text-slate-400 hover:text-danger-500" onClick={() => remove(h)}>
                <Trash2 size={14} />
              </button>
            </span>
          </li>
        ))}
      </ul>

      <Modal open={!!form} onClose={() => setForm(null)} title={form?.id ? "Edit holiday" : "Add holiday"}>
        {form && (
          <form onSubmit={save} className="space-y-3 text-sm">
            <label className="block">
              <span className="text-xs text-slate-500">Name</span>
              <input className="input-field mt-1" value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} required />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">Date</span>
              <input type="date" className="input-field mt-1" value={form.day} onChange={(e) => setForm((f) => ({ ...f, day: e.target.value }))} required />
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">Applies to</span>
              <input className="input-field mt-1" list="holiday-sites" placeholder="Whole organisation (leave empty) or a site name" value={form.site} onChange={(e) => setForm((f) => ({ ...f, site: e.target.value }))} />
              <datalist id="holiday-sites">
                {sites.map((s) => (
                  <option key={s} value={s} />
                ))}
              </datalist>
            </label>
            {form.error && <p className="text-xs text-danger-500">{form.error}</p>}
            <div className="flex justify-end gap-2">
              <button type="button" className="btn-secondary text-sm" onClick={() => setForm(null)}>
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
