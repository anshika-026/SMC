import { useEffect, useState } from "react";
import * as api from "../../api/client";

// Attendance policy (backend/app/workforce.py): every rule the attendance
// classification uses, editable by admins; each save is audited. Payroll
// figures are a PREVIEW only - SMC never deducts anything itself.

const GROUPS = [
  {
    title: "Shift",
    fields: [
      ["shift_start", "Shift starts", "time"],
      ["shift_end", "Shift ends", "time"],
      ["grace_minutes", "Grace period (minutes)", "number"],
      ["half_day_after", "Half day if first in after", "time"],
      ["early_departure_grace_minutes", "Early departure grace (minutes)", "number"],
    ],
  },
  {
    title: "Monthly allowances",
    fields: [
      ["late_allowance_per_month", "Late arrivals allowed per month", "number"],
      ["half_day_allowance_per_month", "Half days allowed per month", "number"],
      ["long_outside_minutes", "Flag an outside period longer than (minutes)", "number"],
    ],
  },
  {
    title: "Overtime",
    fields: [
      ["overtime_after_minutes", "Overtime starts this long after shift end (minutes)", "number"],
      ["overtime_rate", "Overtime rate (× hourly rate)", "number"],
    ],
  },
  {
    title: "Payroll preview (never applied automatically)",
    fields: [
      ["daily_rate", "Daily rate (0 = show days, not money)", "number"],
      ["deduct_days_per_absent", "Days deducted per absence", "number"],
      ["deduct_days_per_half_day", "Days deducted per half day", "number"],
      ["deduct_days_per_late_beyond_allowance", "Days deducted per late beyond the allowance", "number"],
    ],
  },
];

export default function PolicyPanel() {
  const [policy, setPolicy] = useState(null);
  const [weekdays, setWeekdays] = useState([]);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");

  useEffect(() => {
    api.hr
      .policy()
      .then((d) => {
        setPolicy(d.policy);
        setWeekdays(d.weekdays);
      })
      .catch((e) => setMessage(e.message));
  }, []);

  async function save(e) {
    e.preventDefault();
    setSaving(true);
    setMessage("");
    try {
      const d = await api.hr.savePolicy(policy);
      setPolicy(d.policy);
      setMessage("Saved. Attendance is recalculated with the new rules.");
    } catch (err) {
      setMessage(err.message);
    } finally {
      setSaving(false);
    }
  }

  if (!policy) return <div className="card p-6 text-sm text-slate-400">{message || "Loading policy…"}</div>;

  return (
    <form onSubmit={save} className="card p-6 space-y-6">
      <div>
        <h2 className="text-base font-semibold text-ink-900">Attendance policy</h2>
        <p className="text-xs text-slate-500 mt-1">
          These rules decide who is late, on a half day, absent, leaving early or working overtime. Changes apply to every report
          straight away and are recorded in the audit log.
        </p>
      </div>
      {GROUPS.map((g) => (
        <fieldset key={g.title}>
          <legend className="text-sm font-semibold text-ink-900 mb-3">{g.title}</legend>
          <div className="grid sm:grid-cols-2 lg:grid-cols-3 gap-3">
            {g.fields.map(([key, label, type]) => (
              <label key={key} className="block text-sm">
                <span className="text-xs text-slate-500">{label}</span>
                <input
                  type={type}
                  step={type === "number" ? "any" : undefined}
                  min={type === "number" ? 0 : undefined}
                  className="input-field mt-1"
                  value={policy[key]}
                  onChange={(e) => setPolicy((p) => ({ ...p, [key]: e.target.value }))}
                />
              </label>
            ))}
          </div>
        </fieldset>
      ))}
      <fieldset>
        <legend className="text-sm font-semibold text-ink-900 mb-3">Weekly offs</legend>
        <div className="flex flex-wrap gap-3">
          {weekdays.map((d) => (
            <label key={d} className="flex items-center gap-1.5 text-sm">
              <input
                type="checkbox"
                checked={policy.weekly_offs.includes(d)}
                onChange={(e) => setPolicy((p) => ({ ...p, weekly_offs: e.target.checked ? [...p.weekly_offs, d] : p.weekly_offs.filter((x) => x !== d) }))}
              />
              {d}
            </label>
          ))}
        </div>
      </fieldset>
      <div className="flex items-center gap-3">
        <button type="submit" className="btn-primary text-sm" disabled={saving}>
          {saving ? "Saving…" : "Save policy"}
        </button>
        {message && <span className="text-sm text-slate-600">{message}</span>}
      </div>
    </form>
  );
}
