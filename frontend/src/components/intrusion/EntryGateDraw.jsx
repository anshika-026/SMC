import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowLeftRight, RefreshCw, Undo2 } from "lucide-react";
import * as api from "../../api/client";
import { officeArrow } from "../StaffEntranceEditor";

// Draw an intrusion ENTRY GATE on a camera's picture (backend/app/gate_crossing.py): a line across
// the gate (2 clicks where feet cross) with an arrow pointing INSIDE, and optionally a box around
// the gate. Only people who cross the line in the arrow's direction are marked; people going out
// are not. Same drawing as the footfall gate (FootfallGateEditor). Points are frame fractions.

const LINE = "#facc15";

// Without a drawn box, the gate's area is a generous band around the line.
export function bandAround(line, pad = 0.15) {
  const xs = line.map((p) => p[0]);
  const ys = line.map((p) => p[1]);
  const c = (v) => +Math.min(1, Math.max(0, v)).toFixed(4);
  const x0 = c(Math.min(...xs) - pad), x1 = c(Math.max(...xs) + pad), y0 = c(Math.min(...ys) - pad), y1 = c(Math.max(...ys) + pad);
  return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]];
}

function boxPolygon(b) {
  const [x0, x1] = [Math.min(b.x0, b.x1), Math.max(b.x0, b.x1)];
  const [y0, y1] = [Math.min(b.y0, b.y1), Math.max(b.y0, b.y1)];
  return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((p) => p.map((v) => +v.toFixed(4)));
}

export default function EntryGateDraw({ cameras, initial, canSave = true, extraFields, saveGate, saveLabel = "Save gate" }) {
  const [cameraId, setCameraId] = useState(initial?.camera_id ?? cameras[0]?.id ?? null);
  const [frameUrl, setFrameUrl] = useState(null);
  const [frameError, setFrameError] = useState("");
  const [mode, setMode] = useState("line");
  const [line, setLine] = useState(initial?.entry_line || []);
  const [insideSign, setInsideSign] = useState(initial?.inside_sign ?? 1);
  const [box, setBox] = useState(null);
  const [dragging, setDragging] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const areaRef = useRef(null);

  const loadFrame = useCallback(() => {
    if (!cameraId) return;
    setFrameError("");
    api
      .fetchCameraFrameObjectUrl(cameraId)
      .then((u) => setFrameUrl((old) => { if (old) URL.revokeObjectURL(old); return u; }))
      .catch((e) => setFrameError(e.message));
  }, [cameraId]);
  useEffect(() => {
    setFrameUrl(null);
    loadFrame();
  }, [loadFrame]);

  function point(e) {
    const r = areaRef.current.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))].map((v) => +v.toFixed(4));
  }
  function onDown(e) {
    if (!frameUrl) return;
    setMessage("");
    const p = point(e);
    if (mode === "line") {
      setLine((l) => (l.length >= 2 ? [p] : [...l, p]));
      return;
    }
    e.currentTarget.setPointerCapture(e.pointerId);
    setBox({ x0: p[0], y0: p[1], x1: p[0], y1: p[1] });
    setDragging(true);
  }
  function onMove(e) {
    if (!dragging) return;
    const [x, y] = point(e);
    setBox((b) => ({ ...b, x1: x, y1: y }));
  }
  function onUp() {
    if (!dragging) return;
    setDragging(false);
    setBox((b) => (b && Math.abs(b.x1 - b.x0) > 0.02 && Math.abs(b.y1 - b.y0) > 0.02 ? b : null));
  }

  async function save() {
    setSaving(true);
    try {
      const name = await saveGate(cameraId, { entry_line: line, inside_sign: insideSign, polygon: box ? boxPolygon(box) : bandAround(line) });
      setMessage(`${name} saved. Only people who walk in through it (towards the arrow) are marked.`);
    } catch (e) {
      setMessage(e?.message || "Couldn't save the gate.");
    } finally {
      setSaving(false);
    }
  }

  const arrow = line.length === 2 ? officeArrow(line, insideSign) : null;
  const b = box && {
    left: `${Math.min(box.x0, box.x1) * 100}%`, top: `${Math.min(box.y0, box.y1) * 100}%`,
    width: `${Math.abs(box.x1 - box.x0) * 100}%`, height: `${Math.abs(box.y1 - box.y0) * 100}%`,
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {!initial && (
          <>
            <label htmlFor="gate-camera" className="text-sm font-medium text-ink-900">Camera</label>
            <select id="gate-camera" value={cameraId ?? ""} onChange={(e) => { setCameraId(Number(e.target.value)); setLine([]); setBox(null); }} className="input-field w-auto">
              {cameras.map((c) => <option key={c.id} value={c.id}>{c.label}</option>)}
            </select>
          </>
        )}
        {[["line", "1. Gate line"], ["area", "2. Area around the gate (optional)"]].map(([k, label]) => (
          <button key={k} type="button" onClick={() => setMode(k)}
            className={`px-3 py-1.5 rounded-full text-sm font-medium ${mode === k ? "bg-brand-500 text-white" : "bg-white border border-border-200 text-slate-600"}`}>
            {label}
          </button>
        ))}
        <button type="button" onClick={loadFrame} className="ml-auto px-3 py-1.5 rounded-lg text-sm text-slate-500 flex items-center gap-1.5">
          <RefreshCw size={14} /> New picture
        </button>
      </div>

      <div ref={areaRef} className="relative w-full aspect-video bg-[#11151c] rounded-lg overflow-hidden select-none touch-none cursor-crosshair"
        onPointerDown={onDown} onPointerMove={onMove} onPointerUp={onUp}>
        {frameUrl ? (
          <img src={frameUrl} alt="Live picture from the camera" className="w-full h-full object-fill pointer-events-none" draggable={false} />
        ) : (
          <p className="absolute inset-0 flex items-center justify-center text-sm text-slate-300 px-4 text-center">{frameError || "Loading the camera picture…"}</p>
        )}
        {b && <div className="absolute border-2 border-brand-500 bg-brand-500/15 pointer-events-none" style={b} />}
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 w-full h-full pointer-events-none">
          {line.length === 2 && <polyline points={line.map(([x, y]) => `${x * 100},${y * 100}`).join(" ")} stroke={LINE} strokeWidth="4" vectorEffect="non-scaling-stroke" />}
          {arrow && <line x1={arrow.mx * 100} y1={arrow.my * 100} x2={arrow.ex * 100} y2={arrow.ey * 100} stroke={LINE} strokeWidth="3" strokeDasharray="3 2" vectorEffect="non-scaling-stroke" />}
        </svg>
        {line.map(([x, y], i) => (
          <span key={i} className="absolute w-2.5 h-2.5 -ml-[5px] -mt-[5px] rounded-full border-2 border-white pointer-events-none" style={{ left: `${x * 100}%`, top: `${y * 100}%`, background: LINE }} />
        ))}
        {arrow && (
          <span className="absolute -translate-x-1/2 -translate-y-1/2 text-[11px] font-semibold text-black px-1.5 py-0.5 rounded pointer-events-none" style={{ left: `${arrow.ex * 100}%`, top: `${arrow.ey * 100}%`, background: LINE }}>
            Inside
          </span>
        )}
      </div>

      <p className="text-xs text-slate-500">
        {mode === "line"
          ? "Click two points across the gate, on the floor where people's feet cross it. Make sure the arrow points INSIDE (use Flip if not). Only people crossing towards the arrow are marked; people going out are not."
          : "Optional: drag a box around the gate so people walking past further away never take part."}
      </p>

      {extraFields}

      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => setInsideSign((s) => -s)} disabled={line.length !== 2} className="btn-secondary text-sm flex items-center gap-1.5 disabled:opacity-50">
          <ArrowLeftRight size={14} /> Flip inside side
        </button>
        <button type="button" onClick={() => (mode === "line" ? setLine([]) : setBox(null))} className="btn-secondary text-sm flex items-center gap-1.5">
          <Undo2 size={14} /> {mode === "line" ? "Clear line" : "Clear area"}
        </button>
        <button type="button" onClick={save} disabled={saving || line.length !== 2 || !canSave} className="btn-primary text-sm ml-auto disabled:opacity-50">
          {saving ? "Saving…" : saveLabel}
        </button>
      </div>
      {message && <p className="text-sm text-ink-900">{message}</p>}
    </div>
  );
}
