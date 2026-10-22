import { useRef, useState } from "react";
import { Pencil, Square, Undo2, Save, Trash2, ShieldAlert } from "lucide-react";
import Modal from "./Modal";
import * as api from "../api/client";
import useLiveCameraFeed from "../hooks/useLiveCameraFeed";
import { CameraStatusBadges } from "./VisitorCameraGrid";

const clamp01 = (v) => Math.min(1, Math.max(0, v));

// Large live view of one camera with the detection-area editor drawn directly
// on the video. The overlay wrapper is aspect-video and the canvas is
// stretched to fill it (default object-fit, NOT cover), so a click at
// (x/width, y/height) of the wrapper is the same fraction of the camera frame.
// Points are stored and sent to the backend as those 0..1 fractions.
export default function VisitorZoneEditor({ vc, camera, onClose, onChanged, inline = false }) {
  const { canvasRef, status } = useLiveCameraFeed(camera, { overlay: false, width: 960 });
  const boxRef = useRef(null);
  const dragRef = useRef(null); // { type: "point", index } | { type: "rect", start }

  const [points, setPoints] = useState(vc.zone?.polygon || []);
  const [editing, setEditing] = useState(!vc.zone);
  const [rectMode, setRectMode] = useState(false);
  const [enabled, setEnabled] = useState(vc.zone ? vc.zone.enabled : true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const isLive = status === "live";
  const canSave = points.length >= 3 && !busy;

  function fraction(e) {
    const r = boxRef.current.getBoundingClientRect();
    return [clamp01((e.clientX - r.left) / r.width), clamp01((e.clientY - r.top) / r.height)];
  }

  function onBoxDown(e) {
    if (!editing || e.button > 0) return;
    const p = fraction(e);
    if (rectMode) {
      dragRef.current = { type: "rect", start: p };
      setPoints([p, p, p, p]);
      boxRef.current.setPointerCapture(e.pointerId);
    } else {
      setPoints((pts) => [...pts, p]);
    }
  }

  function onPointDown(e, index) {
    if (!editing) return;
    e.stopPropagation();
    dragRef.current = { type: "point", index };
    boxRef.current.setPointerCapture(e.pointerId);
  }

  function onMove(e) {
    const d = dragRef.current;
    if (!d) return;
    const p = fraction(e);
    if (d.type === "point") {
      setPoints((pts) => pts.map((q, i) => (i === d.index ? p : q)));
    } else {
      const [x0, y0] = d.start;
      setPoints([[x0, y0], [p[0], y0], p, [x0, p[1]]]);
    }
  }

  function onUp(e) {
    const d = dragRef.current;
    dragRef.current = null;
    if (boxRef.current?.hasPointerCapture(e.pointerId)) boxRef.current.releasePointerCapture(e.pointerId);
    if (d?.type === "rect") {
      setRectMode(false);
      setPoints((pts) => (Math.abs(pts[2][0] - pts[0][0]) < 0.01 || Math.abs(pts[2][1] - pts[0][1]) < 0.01 ? [] : pts));
    }
  }

  async function run(fn) {
    setBusy(true);
    setError("");
    try {
      await fn();
      onChanged();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const save = () =>
    run(async () => {
      const res = await api.saveVisitorZone(vc.camera_id, { polygon: points, enabled });
      setPoints(res.zone.polygon);
      setEnabled(res.zone.enabled);
      setEditing(false);
    });

  const toggleEnabled = () => {
    const next = !enabled;
    if (vc.zone && !editing) {
      run(async () => {
        const res = await api.saveVisitorZone(vc.camera_id, { polygon: vc.zone.polygon, enabled: next });
        setEnabled(res.zone.enabled);
      });
    } else {
      setEnabled(next);
    }
  };

  const clear = () => {
    if (!window.confirm(`Clear the detection area for "${vc.name}"? Detection will stop on this camera.`)) return;
    run(async () => {
      await api.clearVisitorZone(vc.camera_id);
      setPoints([]);
      setEditing(true);
      setEnabled(true);
    });
  };

  function startEdit() {
    setPoints(vc.zone?.polygon || points);
    setEditing(true);
  }

  function cancelEdit() {
    setPoints(vc.zone?.polygon || []);
    setEnabled(vc.zone ? vc.zone.enabled : true);
    setRectMode(false);
    setEditing(!vc.zone);
  }

  const polyStr = points.map(([x, y]) => `${x},${y}`).join(" ");
  const showFill = points.length >= 3;

  const body = (
    <>
      <div className="flex flex-wrap items-center justify-between gap-2 mb-3">
        <CameraStatusBadges vc={vc} />
        <span className="text-xs text-slate-400">{vc.site}</span>
      </div>

      <div
        ref={boxRef}
        onPointerDown={onBoxDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
        onPointerCancel={onUp}
        className={`aspect-video bg-ink-900 rounded-xl overflow-hidden relative select-none touch-none ${editing ? "cursor-crosshair" : ""}`}
      >
        <canvas ref={canvasRef} className="absolute inset-0 w-full h-full" style={{ objectFit: "fill" }} />

        {!isLive && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-1 text-white/50 text-sm pointer-events-none">
            <ShieldAlert size={28} />
            {!vc.online ? "Camera offline" : status === "connecting" ? "Connecting…" : status === "disabled" ? "Feed switched off" : "No picture"}
            <span className="text-xs text-white/30">Stream state: {vc.stream_state || status}</span>
          </div>
        )}

        <svg className="absolute inset-0 w-full h-full pointer-events-none" viewBox="0 0 1 1" preserveAspectRatio="none">
          {showFill ? (
            <polygon
              points={polyStr}
              fill={enabled ? "rgba(37,99,235,0.25)" : "rgba(148,163,184,0.25)"}
              stroke={enabled ? "#2563eb" : "#94a3b8"}
              strokeWidth="2"
              vectorEffect="non-scaling-stroke"
            />
          ) : (
            points.length > 1 && (
              <polyline points={polyStr} fill="none" stroke="#2563eb" strokeWidth="2" vectorEffect="non-scaling-stroke" />
            )
          )}
        </svg>

        {editing &&
          points.map(([x, y], i) => (
            <div
              key={i}
              onPointerDown={(e) => onPointDown(e, i)}
              className="absolute w-4 h-4 -ml-2 -mt-2 rounded-full bg-white border-2 border-brand-600 cursor-move shadow"
              style={{ left: `${x * 100}%`, top: `${y * 100}%` }}
            />
          ))}
      </div>

      <p className="mt-2 text-xs text-slate-500">
        {editing
          ? rectMode
            ? "Drag on the video from one corner to the opposite corner."
            : "Click on the video to add points (at least 3). Drag a point to adjust it."
          : vc.zone
            ? "Detection area is shown on the live video."
            : ""}
      </p>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        {editing ? (
          <>
            <button type="button" className="btn-secondary inline-flex items-center gap-1.5" onClick={() => setRectMode((m) => !m)} disabled={busy}>
              <Square size={14} /> {rectMode ? "Cancel rectangle" : "Draw rectangle"}
            </button>
            <button
              type="button"
              className="btn-secondary inline-flex items-center gap-1.5"
              onClick={() => setPoints((p) => p.slice(0, -1))}
              disabled={busy || points.length === 0}
            >
              <Undo2 size={14} /> Undo last point
            </button>
            <button type="button" className="btn-primary inline-flex items-center gap-1.5" onClick={save} disabled={!canSave} title={points.length < 3 ? "Add at least 3 points" : ""}>
              <Save size={14} /> {busy ? "Saving…" : "Save area"}
            </button>
            {vc.zone && (
              <button type="button" className="btn-secondary" onClick={cancelEdit} disabled={busy}>
                Cancel
              </button>
            )}
          </>
        ) : (
          <button type="button" className="btn-secondary inline-flex items-center gap-1.5" onClick={startEdit} disabled={busy}>
            <Pencil size={14} /> Edit
          </button>
        )}
        {vc.zone && (
          <button type="button" className="btn-secondary text-danger-600 inline-flex items-center gap-1.5" onClick={clear} disabled={busy}>
            <Trash2 size={14} /> Clear area
          </button>
        )}
        <label className="ml-auto flex items-center gap-2 text-sm text-ink-900 cursor-pointer">
          <input type="checkbox" checked={enabled} onChange={toggleEnabled} disabled={busy || (!vc.zone && points.length < 3)} />
          Detection {enabled ? "on" : "off"}
        </label>
      </div>
      {error && <p className="mt-2 text-xs text-danger-600">{error}</p>}
    </>
  );

  // inline: a normal card on the page (the Visitor Management camera picker);
  // otherwise the original popup.
  return inline ? (
    <div className="card p-4">{body}</div>
  ) : (
    <Modal open onClose={onClose} title={vc.name} width="max-w-5xl">
      {body}
    </Modal>
  );
}
