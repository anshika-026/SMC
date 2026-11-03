import { useCallback, useEffect, useState } from "react";
import Modal from "../Modal";
import PolygonZoneEditor from "../PolygonZoneEditor";
import EntryGateDraw from "./EntryGateDraw";
import * as api from "../../api/client";
import { ZONE_TYPES, dwellProblem, minutesToSeconds, secondsToMinutes } from "../../lib/zoneSettings";

// Draw or edit an intrusion zone on a camera's picture (backend/app/intrusion.py). The form is
// MAINDV's "Draw restricted zone" dialog (zone type, loitering duration, active hours, severity,
// alerts on/off) plus SMC's area name and entry-gate mark. Used by the Intrusion page and by
// Visitor Management > Zones & areas, so both create the same zones.
//
// New zone: after saving one camera's outline the details stay filled in, so the same zone can be
// drawn on another camera too (one zone watched by several cameras; each keeps its own outline).
// Existing zone (`zone`): change its settings, and optionally redraw its outline.

const ZONE_COLOR = "#ef4444";
const SEVERITIES = ["Critical", "High", "Medium", "Low"];
const EMPTY_FORM = { name: "", area: "", gate: false, from: "", to: "", type: "restricted", minutes: "15", severity: "", alertsEnabled: true, enabled: true };

function formFor(zone, preset) {
  if (!zone) return { ...EMPTY_FORM, ...(preset || {}) };
  return {
    name: zone.name,
    area: zone.area || "",
    gate: !!zone.is_entry_gate,
    from: zone.active_from || "",
    to: zone.active_to || "",
    type: zone.zone_type || "restricted",
    minutes: secondsToMinutes(zone.dwell_seconds) || "15",
    severity: zone.severity || "",
    alertsEnabled: zone.alerts_enabled !== false,
    enabled: zone.enabled !== false,
  };
}

export default function IntrusionZoneEditor({ open, zone = null, preset = null, cameras, onClose, onChanged }) {
  const [form, setForm] = useState(() => formFor(zone, preset));
  const [saved, setSaved] = useState([]);      // cameras this new zone was drawn on in this dialog
  const [error, setError] = useState("");
  const [redraw, setRedraw] = useState(false);

  useEffect(() => {
    if (!open) return;
    setForm(formFor(zone, preset));
    setSaved([]);
    setError("");
    setRedraw(false);
  }, [open, zone, preset]);

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));
  const windowValid = (!form.from && !form.to) || (form.from && form.to);
  const dwellSeconds = minutesToSeconds(form.minutes);
  const dwellError = form.type === "loitering" ? dwellProblem(dwellSeconds) : "";
  const canSave = !!form.name.trim() && windowValid && !dwellError;

  const editorCameras = (cameras || []).map((c) => ({ id: c.id, label: c.label || c.name }));
  const zoneCameras = zone ? editorCameras.filter((c) => c.id === zone.camera_id) : editorCameras;

  const loadZonesFor = useCallback(
    (cameraId) => api.intrusionApi.zones(cameraId).then((zs) => zs.filter((z) => !zone || z.id !== zone.id).map((z) => ({ id: z.id, label: z.name, polygon: z.polygon }))),
    [zone]
  );

  function fields() {
    return {
      name: form.name.trim(),
      area: form.area.trim() || null,
      is_entry_gate: form.gate,
      active_from: form.from || null,
      active_to: form.to || null,
      zone_type: form.gate ? "restricted" : form.type,   // an entry gate marks people coming in
      dwell_seconds: form.type === "loitering" ? dwellSeconds : null,
      severity: form.severity || null,
      alerts_enabled: form.alertsEnabled,
      ...(zone ? { enabled: form.enabled } : {}),
    };
  }

  async function saveSettings() {
    setError("");
    try {
      await api.intrusionApi.updateZone(zone.id, fields());
      onChanged?.();
      onClose();
    } catch (e) {
      setError(e.message || "Could not save the changes.");
    }
  }

  const details = (
    <div className="grid sm:grid-cols-3 gap-3 text-sm">
      <label className="block sm:col-span-1">
        <span className="font-medium text-ink-900 block mb-1.5">
          Zone name <span className="text-danger-500">*</span>
        </span>
        <input value={form.name} onChange={set("name")} placeholder={form.gate ? "e.g. Reception Entry Gate" : "e.g. Server room door"} className="input-field" maxLength={80} />
      </label>
      <label className="block">
        <span className="font-medium text-ink-900 block mb-1.5">Area</span>
        <input value={form.area} onChange={set("area")} placeholder="e.g. Reception" className="input-field" maxLength={80} />
      </label>
      <label className="flex items-center gap-2 self-end pb-2">
        <input type="checkbox" checked={form.gate} onChange={set("gate")} /> This is an entry gate
      </label>
      <label className="block">
        <span className="font-medium text-ink-900 block mb-1.5">Zone type</span>
        <select value={form.gate ? "restricted" : form.type} onChange={set("type")} disabled={form.gate} className="input-field disabled:opacity-60">
          {ZONE_TYPES.map((t) => (
            <option key={t.value} value={t.value}>
              {t.label}
            </option>
          ))}
        </select>
      </label>
      {form.type === "loitering" && (
        <label className="block">
          <span className="font-medium text-ink-900 block mb-1.5">Alert after (minutes)</span>
          <input type="number" min="0.1" step="any" value={form.minutes} onChange={set("minutes")} className="input-field" />
        </label>
      )}
      <label className="block">
        <span className="font-medium text-ink-900 block mb-1.5">Alert severity</span>
        <select value={form.severity} onChange={set("severity")} className="input-field">
          <option value="">{form.type === "loitering" ? "Default (Medium)" : "Default (High)"}</option>
          {SEVERITIES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </label>
      <label className="block">
        <span className="font-medium text-ink-900 block mb-1.5">Active from</span>
        <input type="time" value={form.from} onChange={set("from")} className="input-field" />
      </label>
      <label className="block">
        <span className="font-medium text-ink-900 block mb-1.5">Active until</span>
        <input type="time" value={form.to} onChange={set("to")} className="input-field" />
      </label>
      <label className="flex items-center gap-2 self-end pb-2">
        <input type="checkbox" checked={form.alertsEnabled} onChange={set("alertsEnabled")} /> Raise alerts for this zone
      </label>
      {zone && (
        <label className="flex items-center gap-2 self-end pb-2">
          <input type="checkbox" checked={form.enabled} onChange={set("enabled")} /> Monitoring on
        </label>
      )}
      <p className="text-xs text-slate-500 sm:col-span-3 -mt-1">
        Leave both times empty to guard around the clock. 19:00 to 08:00 guards overnight only.
        {!windowValid && <span className="text-danger-500"> Set both times, or neither.</span>}
      </p>
      {dwellError && <p className="text-xs text-danger-500 sm:col-span-3">{dwellError}</p>}
    </div>
  );

  const hint =
    form.type === "loitering"
      ? "Click each corner of the floor area where people should not stay long. The timer runs while their feet are inside it."
      : "Click each corner of the floor area to guard. Someone counts as inside when their feet are in it.";

  return (
    <Modal open={open} onClose={onClose} width="max-w-3xl" title={zone ? `Edit zone · ${zone.name} (${zone.camera_name || "camera"})` : "New intrusion zone"}>
      {open && (
        <div className="space-y-4">
          {editorCameras.length === 0 ? (
            <p className="text-sm text-slate-500">No cameras yet. Add one under Management &gt; Camera.</p>
          ) : zone && !redraw ? (
            <>
              {details}
              {form.gate && !zone.entry_line && (
                <p className="text-xs text-warning-600">
                  This entry gate has no gate line yet, so it alerts on anyone inside it. Draw the line to mark only people who come in.
                </p>
              )}
              {error && <p className="text-sm text-danger-500">{error}</p>}
              <div className="flex flex-wrap justify-end gap-2">
                <button type="button" className="btn-secondary text-sm mr-auto" onClick={() => setRedraw(true)}>
                  {form.gate ? (zone.entry_line ? "Redraw the gate line" : "Draw the gate line") : "Redraw the outline"}
                </button>
                <button type="button" className="btn-secondary text-sm" onClick={onClose}>
                  Cancel
                </button>
                <button type="button" className="btn-primary text-sm" disabled={!canSave} onClick={saveSettings}>
                  Save changes
                </button>
              </div>
            </>
          ) : form.gate ? (
            <>
              <EntryGateDraw
                cameras={zoneCameras}
                initial={zone}
                canSave={canSave}
                extraFields={details}
                saveLabel={zone ? "Save gate" : "Save entry gate"}
                saveGate={async (cameraId, gate) => {
                  if (zone) {
                    const z = await api.intrusionApi.updateZone(zone.id, { ...fields(), ...gate });
                    onChanged?.();
                    onClose();
                    return z.name;
                  }
                  const z = await api.intrusionApi.createZone({
                    cameraId, polygon: gate.polygon, entryLine: gate.entry_line, insideSign: gate.inside_sign, name: form.name.trim(),
                    area: form.area.trim(), isEntryGate: true, from: form.from, to: form.to, zoneType: "restricted",
                    severity: form.severity || null, alertsEnabled: form.alertsEnabled,
                  });
                  setSaved((s) => [...new Set([...s, cameraId])]);
                  onChanged?.();
                  return `${z.name} on ${editorCameras.find((c) => c.id === cameraId)?.label || "the camera"}`;
                }}
              />
              {!zone && saved.length > 0 && (
                <p className="text-xs text-slate-500">
                  Saved on {saved.length} camera{saved.length === 1 ? "" : "s"}. To watch the same gate from another camera, pick it above and draw its line there.
                </p>
              )}
            </>
          ) : (
            <>
              <PolygonZoneEditor
                cameras={zoneCameras}
                loadZones={loadZonesFor}
                color={ZONE_COLOR}
                hint={hint}
                canSave={canSave}
                saveZone={async (cameraId, polygon) => {
                  if (zone) {
                    const z = await api.intrusionApi.updateZone(zone.id, { ...fields(), polygon });
                    onChanged?.();
                    onClose();
                    return z.name;
                  }
                  const z = await api.intrusionApi.createZone({
                    cameraId, polygon, name: form.name.trim(), area: form.area.trim(), isEntryGate: form.gate, from: form.from, to: form.to,
                    zoneType: form.type, dwellSeconds: form.type === "loitering" ? dwellSeconds : null, severity: form.severity || null,
                    alertsEnabled: form.alertsEnabled,
                  });
                  setSaved((s) => [...new Set([...s, cameraId])]);
                  return `${z.name} on ${editorCameras.find((c) => c.id === cameraId)?.label || "the camera"}`;
                }}
                deleteZone={(z) => api.intrusionApi.deleteZone(z.id)}
                onChanged={onChanged}
                extraFields={details}
              />
              {!zone && saved.length > 0 && (
                <p className="text-xs text-slate-500">
                  Saved on {saved.length} camera{saved.length === 1 ? "" : "s"}. To watch the same zone from another camera, pick it above and draw its outline there.
                </p>
              )}
            </>
          )}
        </div>
      )}
    </Modal>
  );
}
