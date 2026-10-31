import { useEffect, useRef, useState } from "react";
import { ShieldAlert, Trash2, Undo2 } from "lucide-react";
import Modal from "../Modal";
import * as api from "../../api/client";
import useLiveCameraFeed from "../../hooks/useLiveCameraFeed";

// Mark a restricted area on a camera's live picture (click to add corners,
// drag a corner to move it) and set its rule. Points are 0..1 fractions of
// the frame, as every other area editor in SMC stores them.

const clamp01 = (v) => Math.min(1, Math.max(0, v));
const RULES = [
  ["non_employees", "People who are not recognised employees"],
  ["everyone", "Anyone, employees included"],
];

export default function RestrictedZoneEditor({ open, zone, cameras, onClose, onSaved }) {
  const [cameraId, setCameraId] = useState(zone?.camera_id ?? "");
  const [form, setForm] = useState(() => ({
    name: zone?.name || "",
    rule: zone?.settings?.rule || "non_employees",
    severity: zone?.settings?.severity || "High",
    cooldown_minutes: zone?.settings?.cooldown_minutes ?? 10,
    enabled: zone ? zone.enabled : true,
  }));
  const [points, setPoints] = useState(zone?.points || []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!open) return;
    setCameraId(zone?.camera_id ?? "");
    setPoints(zone?.points || []);
    setError("");
  }, [open, zone]);

  const camera = cameras.find((c) => c.id === Number(cameraId)) || null;
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.type === "checkbox" ? e.target.checked : e.target.value }));

  async function save() {
    setBusy(true);
    setError("");
    try {
      const body = { ...form, cooldown_minutes: Number(form.cooldown_minutes), polygon: points };
      if (zone) await api.zonesApi.updateRestricted(zone.id, body);
      else await api.zonesApi.createRestricted({ ...body, camera_id: Number(cameraId) });
      onSaved();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open={open} onClose={onClose} width="max-w-5xl" title={zone ? `Edit restricted zone · ${zone.name}` : "New restricted zone"}>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-5 text-sm">
        <div className="lg:col-span-2 space-y-2">
          {!zone && (
            <select className="input-field" value={cameraId} onChange={(e) => { setCameraId(e.target.value); setPoints([]); }}>
              <option value="">Choose the camera…</option>
              {cameras.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
          )}
          {camera ? (
            <PolygonOnFeed key={camera.id} camera={camera} points={points} setPoints={setPoints} />
          ) : (
            <div className="aspect-video rounded-xl bg-slate-100 grid place-items-center text-slate-400 text-sm">Choose a camera to mark the area on its picture.</div>
          )}
          <div className="flex items-center gap-2 text-xs text-slate-500">
            <span>Click on the picture to add corners ({points.length} so far, at least 3). Drag a corner to move it.</span>
            <button type="button" className="ml-auto inline-flex items-center gap-1 hover:text-ink-900" disabled={!points.length} onClick={() => setPoints((p) => p.slice(0, -1))}>
              <Undo2 size={13} /> Undo
            </button>
            <button type="button" className="inline-flex items-center gap-1 hover:text-danger-500" disabled={!points.length} onClick={() => setPoints([])}>
              <Trash2 size={13} /> Clear
            </button>
          </div>
        </div>
        <div className="space-y-3">
          <label className="block">
            <span className="text-xs text-slate-500">Zone name</span>
            <input className="input-field mt-1" value={form.name} onChange={set("name")} placeholder="e.g. Server room" maxLength={60} />
          </label>
          <label className="block">
            <span className="text-xs text-slate-500">Alert when entered by</span>
            <select className="input-field mt-1" value={form.rule} onChange={set("rule")}>
              {RULES.map(([k, label]) => (
                <option key={k} value={k}>
                  {label}
                </option>
              ))}
            </select>
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="block">
              <span className="text-xs text-slate-500">Severity</span>
              <select className="input-field mt-1" value={form.severity} onChange={set("severity")}>
                {["Critical", "High", "Medium", "Low"].map((s) => (
                  <option key={s}>{s}</option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-slate-500">Repeat alert after (min)</span>
              <input type="number" min={1} max={1440} className="input-field mt-1" value={form.cooldown_minutes} onChange={set("cooldown_minutes")} />
            </label>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={form.enabled} onChange={set("enabled")} /> Monitoring on
          </label>
          <p className="text-[11px] text-slate-500 leading-relaxed">
            Alerts appear in Alerts &amp; Events with a picture. Recognised employees are named; with the first rule they are logged but never alerted. Visitor seating
            areas and attendance are not affected.
          </p>
          {error && <p className="text-xs text-danger-500">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" className="btn-secondary text-sm" onClick={onClose}>
              Cancel
            </button>
            <button type="button" className="btn-primary text-sm" disabled={busy || points.length < 3 || !form.name.trim() || (!zone && !cameraId)} onClick={save}>
              {busy ? "Saving…" : "Save zone"}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

function PolygonOnFeed({ camera, points, setPoints }) {
  const { canvasRef, live, status } = useLiveCameraFeed(camera, { overlay: false, width: 960 });
  const boxRef = useRef(null);
  const drag = useRef(null);
  const frac = (e) => {
    const r = boxRef.current.getBoundingClientRect();
    return [clamp01((e.clientX - r.left) / r.width), clamp01((e.clientY - r.top) / r.height)];
  };
  return (
    <div
      ref={boxRef}
      className="relative aspect-video rounded-xl overflow-hidden bg-ink-900 cursor-crosshair select-none touch-none"
      onPointerDown={(e) => e.button === 0 && !drag.current && setPoints((p) => [...p, frac(e)])}
      onPointerMove={(e) => drag.current !== null && setPoints((p) => p.map((q, i) => (i === drag.current ? frac(e) : q)))}
      onPointerUp={() => (drag.current = null)}
    >
      <canvas ref={canvasRef} className="w-full h-full" />
      {!live && (
        <div className="absolute inset-0 flex flex-col items-center justify-center text-white/50 text-sm gap-1">
          <ShieldAlert size={24} />
          {status === "connecting" ? "Connecting…" : "No picture from this camera right now"}
        </div>
      )}
      <svg viewBox="0 0 1 1" preserveAspectRatio="none" className="absolute inset-0 w-full h-full pointer-events-none">
        {points.length >= 2 && (
          <polygon points={points.map((p) => p.join(",")).join(" ")} fill="rgba(239,68,68,0.22)" stroke="#ef4444" strokeWidth="0.004" />
        )}
      </svg>
      {points.map((p, i) => (
        <span
          key={i}
          onPointerDown={(e) => {
            e.stopPropagation();
            drag.current = i;
            boxRef.current.setPointerCapture(e.pointerId);
          }}
          className="absolute w-3.5 h-3.5 -ml-[7px] -mt-[7px] rounded-full bg-white ring-2 ring-red-500 cursor-move"
          style={{ left: `${p[0] * 100}%`, top: `${p[1] * 100}%` }}
        />
      ))}
    </div>
  );
}
