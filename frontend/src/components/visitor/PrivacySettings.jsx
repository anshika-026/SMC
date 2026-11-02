import { useEffect, useState } from "react";
import Modal from "../Modal";
import * as api from "../../api/client";
import { Field } from "./shared";

// Privacy and retention for visitor records: the notice shown at
// registration, how long snapshots/clips are kept, the long-stay threshold
// and the default gate camera.

export default function PrivacySettings({ open, onClose, settings, onSaved }) {
  const [form, setForm] = useState(null);
  const [cameras, setCameras] = useState([]);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open || !settings) return;
    setForm({
      media_retention_days: settings.media_retention_days, long_stay_minutes: settings.long_stay_minutes,
      waiting_alert_minutes: settings.waiting_alert_minutes ?? 15,
      default_gate_camera_id: settings.default_gate_camera_id ?? "", notice_text: settings.notice_text,
    });
    setError("");
    api.vr.cameras().then((d) => setCameras(d.cameras)).catch(() => {});
  }, [open, settings]);

  async function save(e) {
    e.preventDefault();
    try {
      const s = await api.vr.saveSettings({
        ...form, media_retention_days: Number(form.media_retention_days), long_stay_minutes: Number(form.long_stay_minutes),
        waiting_alert_minutes: Number(form.waiting_alert_minutes),
        default_gate_camera_id: form.default_gate_camera_id === "" ? null : Number(form.default_gate_camera_id),
      });
      onSaved(s);
      onClose();
    } catch (err) {
      setError(err.message);
    }
  }

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }));
  return (
    <Modal open={open} onClose={onClose} title="Visitor settings, privacy & retention" width="max-w-lg">
      {form && (
        <form onSubmit={save} className="space-y-3 text-sm">
          <Field label="Visitor notice (shown at registration)" hint="{days} is replaced with the retention period.">
            <textarea className="input-field min-h-[90px]" value={form.notice_text} onChange={set("notice_text")} />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Keep snapshots & clips for (days)" hint="1-365. Visit records themselves are kept.">
              <input type="number" min={1} max={365} className="input-field" value={form.media_retention_days} onChange={set("media_retention_days")} />
            </Field>
            <Field label="Long stay after (minutes)">
              <input type="number" min={10} max={1440} className="input-field" value={form.long_stay_minutes} onChange={set("long_stay_minutes")} />
            </Field>
          </div>
          <Field label="Visitor waiting alert after (minutes)" hint="Checked-in visitors not yet met by their host for longer than this appear under Attention required on the dashboard.">
            <input type="number" min={1} max={240} className="input-field" value={form.waiting_alert_minutes} onChange={set("waiting_alert_minutes")} />
          </Field>
          <Field label="Default entry gate camera">
            <select className="input-field" value={form.default_gate_camera_id} onChange={set("default_gate_camera_id")}>
              <option value="">Not set</option>
              {cameras.map((c) => <option key={c.camera_id} value={c.camera_id}>{c.name}</option>)}
            </select>
          </Field>
          <ul className="text-[11px] text-slate-500 list-disc pl-4 space-y-0.5">
            <li>Snapshots are taken only for visitors who agreed, and only of a clear face; one per visit.</li>
            <li>No face recognition or face matching is used. Returning visitors are matched by mobile, e-mail or name, and confirmed by the operator.</li>
            <li>A visitor's data can be erased from their profile (profile, visits, snapshots and clips).</li>
          </ul>
          {error && <p className="text-xs text-danger-500">{error}</p>}
          <div className="flex justify-end gap-2">
            <button type="button" className="btn-secondary text-sm" onClick={onClose}>Cancel</button>
            <button type="submit" className="btn-primary text-sm">Save</button>
          </div>
        </form>
      )}
    </Modal>
  );
}
