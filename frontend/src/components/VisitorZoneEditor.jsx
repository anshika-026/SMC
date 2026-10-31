import { useRef, useState } from "react";
import { Pencil, Square, Undo2, Save, Trash2, ShieldAlert, Box } from "lucide-react";
import Modal from "./Modal";
import * as api from "../api/client";
import useLiveCameraFeed from "../hooks/useLiveCameraFeed";
import { CameraStatusBadges } from "./VisitorCameraGrid";

const clamp01 = (v) => Math.min(1, Math.max(0, v));

// Height a NEW area starts with (fraction of the frame height at its nearest
// corner), so it shows as a box straight away. Areas saved before 3D areas
// existed load with height 0 and look exactly as before (flat).
const DEFAULT_HEIGHT = 0.3;
const MAX_HEIGHT = 0.8;

// The 3D area: the polygon is the FLOOR, `height` lifts a copy of it as the
// top. Without camera calibration the box is drawn with an approximate
// perspective: vertical edges stay vertical in the picture and get shorter
// the further up the picture (= further from the camera) their corner is,
// in proportion to the corner's distance from the top edge. `height` is the
// edge length at the nearest (lowest) corner. Drawing only: the backend's
// zone rule uses the floor polygon (feet inside it).
function prismTop(points, height) {
  const nearY = Math.max(...points.map((p) => p[1]));
  return points.map(([x, y]) => [x, y - (nearY > 0 ? (height * y) / nearY : 0)]);
}

const nearestIndex = (points) => points.reduce((best, p, i) => (p[1] > points[best][1] ? i : best), 0);

// Large live view of one camera with the detection-area editor drawn directly
// on the video. The overlay wrapper is aspect-video and the canvas is
// stretched to fill it (default object-fit, NOT cover), so a click at
// (x/width, y/height) of the wrapper is the same fraction of the camera frame.
// Points are stored and sent to the backend as those 0..1 fractions.
export default function VisitorZoneEditor({ vc, camera, onClose, onChanged, inline = false }) {
  const { canvasRef, status } = useLiveCameraFeed(camera, { overlay: false, width: 960 });
  const boxRef = useRef(null);
  const dragRef = useRef(null); // { type: "point", index } | { type: "rect", start } | { type: "height" }

  const [points, setPoints] = useState(vc.zone?.polygon || []);
  const [editing, setEditing] = useState(!vc.zone);
  const [rectMode, setRectMode] = useState(false);
  const [enabled, setEnabled] = useState(vc.zone ? vc.zone.enabled : true);
  const [height, setHeight] = useState(vc.zone ? vc.zone.height || 0 : DEFAULT_HEIGHT);
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

  function onHeightDown(e) {
    if (!editing) return;
    e.stopPropagation();
    dragRef.current = { type: "height" };
    boxRef.current.setPointerCapture(e.pointerId);
  }

  function onMove(e) {
    const d = dragRef.current;
    if (!d) return;
    const p = fraction(e);
    if (d.type === "height") {
      const nearY = points[nearestIndex(points)][1];
      setHeight(Math.round(Math.min(MAX_HEIGHT, nearY, Math.max(0, nearY - p[1])) * 1000) / 1000);
    } else if (d.type === "point") {
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
      const res = await api.saveVisitorZone(vc.camera_id, { polygon: points, height, enabled });
      setPoints(res.zone.polygon);
      setHeight(res.zone.height || 0);
      setEnabled(res.zone.enabled);
      setEditing(false);
    });

  const toggleEnabled = () => {
    const next = !enabled;
    if (vc.zone && !editing) {
      run(async () => {
        const res = await api.saveVisitorZone(vc.camera_id, { polygon: vc.zone.polygon, height: vc.zone.height || 0, enabled: next });
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
      setHeight(DEFAULT_HEIGHT);
      setEditing(true);
      setEnabled(true);
    });
  };

  function startEdit() {
    setPoints(vc.zone?.polygon || points);
    setHeight(vc.zone ? vc.zone.height || 0 : height);
    setEditing(true);
  }

  function cancelEdit() {
    setPoints(vc.zone?.polygon || []);
    setHeight(vc.zone ? vc.zone.height || 0 : DEFAULT_HEIGHT);
    setEnabled(vc.zone ? vc.zone.enabled : true);
    setRectMode(false);
    setEditing(!vc.zone);
  }

  const toStr = (pts) => pts.map(([x, y]) => `${x},${y}`).join(" ");
  const polyStr = toStr(points);
  const showFill = points.length >= 3;
  const is3d = showFill && height > 0;
  const top = is3d ? prismTop(points, height) : null;
  const near = showFill ? nearestIndex(points) : -1;
  const stroke = enabled ? "#2563eb" : "#94a3b8";
  const rgb = enabled ? "37,99,235" : "148,163,184";
  // Side faces, furthest (highest in the picture) first so nearer faces paint over them.
  const sides = is3d
    ? points
        .map((p, i) => {
          const j = (i + 1) % points.length;
          return { key: i, pts: [p, points[j], top[j], top[i]], depth: p[1] + points[j][1] };
        })
        .sort((a, b) => a.depth - b.depth)
    : [];

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
            <>
              <polygon points={polyStr} fill={`rgba(${rgb},0.25)`} stroke={stroke} strokeWidth="2" vectorEffect="non-scaling-stroke" />
              {is3d && (
                <>
                  {sides.map((f) => (
                    <polygon key={f.key} points={toStr(f.pts)} fill={`rgba(${rgb},0.12)`} stroke="none" />
                  ))}
                  {points.map(([x, y], i) => (
                    <line key={i} x1={x} y1={y} x2={top[i][0]} y2={top[i][1]} stroke={stroke} strokeWidth="2" vectorEffect="non-scaling-stroke" />
                  ))}
                  <polygon points={toStr(top)} fill={`rgba(${rgb},0.1)`} stroke={stroke} strokeWidth="2" vectorEffect="non-scaling-stroke" />
                </>
              )}
            </>
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

        {editing && showFill && (
          <div
            onPointerDown={onHeightDown}
            title="Drag up or down to set the box height"
            className="absolute w-4 h-4 -ml-2 -mt-2 rounded-sm bg-brand-600 border-2 border-white cursor-ns-resize shadow"
            style={{ left: `${points[near][0] * 100}%`, top: `${(is3d ? top[near][1] : points[near][1]) * 100}%` }}
          />
        )}
      </div>

      <p className="mt-2 text-xs text-slate-500">
        {editing
          ? rectMode
            ? "Drag on the video from one corner to the opposite corner."
            : "Click on the video to mark the FLOOR of the area (at least 3 points). Drag a point to adjust it; drag the square handle (or use Box height) to raise the 3D box."
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
            <label className="flex items-center gap-2 text-sm text-ink-900" title="0 = flat area (2D)">
              <Box size={14} /> Box height
              <input
                type="range"
                min="0"
                max={MAX_HEIGHT}
                step="0.01"
                value={height}
                onChange={(e) => setHeight(Number(e.target.value))}
                disabled={busy}
                className="w-28"
              />
              <span className="w-10 text-xs text-slate-500">{height > 0 ? `${Math.round(height * 100)}%` : "flat"}</span>
            </label>
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
