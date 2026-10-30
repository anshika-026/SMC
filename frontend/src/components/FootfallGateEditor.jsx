import { useEffect, useRef, useState } from "react";
import { ArrowLeftRight, RefreshCw, Undo2 } from "lucide-react";
import * as api from "../api/client";
import { officeArrow } from "./StaffEntranceEditor";

// One footfall gate: the ENTRY LINE across the gate (2 clicks where feet cross)
// with an arrow pointing inside, and optionally a box around the gate to limit
// who is considered. Only people who cross the line from outside to inside are
// counted; people walking past or standing outside never are. Points are
// fractions of the picture (backend/app/footfall.py).

const LINE = "#facc15";

function boxOf(zone) {
  if (!zone || zone.length < 3) return null;
  const xs = zone.map((p) => p[0]);
  const ys = zone.map((p) => p[1]);
  return { x0: Math.min(...xs), y0: Math.min(...ys), x1: Math.max(...xs), y1: Math.max(...ys) };
}

function toPolygon(b) {
  const [x0, x1] = [Math.min(b.x0, b.x1), Math.max(b.x0, b.x1)];
  const [y0, y1] = [Math.min(b.y0, b.y1), Math.max(b.y0, b.y1)];
  return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((p) => p.map((v) => +v.toFixed(4)));
}

export default function FootfallGateEditor({ gate, onSaved }) {
  const [frameUrl, setFrameUrl] = useState(null);
  const [frameError, setFrameError] = useState("");
  const [mode, setMode] = useState("line");
  const [line, setLine] = useState(gate.entry_line || []);
  const [insideSign, setInsideSign] = useState(gate.inside_sign ?? 1);
  const [box, setBox] = useState(() => boxOf(gate.zone));
  const [dragging, setDragging] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const areaRef = useRef(null);

  function loadFrame() {
    setFrameError("");
    api
      .fetchFootfallFrameObjectUrl(gate.camera_id)
      .then((u) =>
        setFrameUrl((old) => {
          if (old) URL.revokeObjectURL(old);
          return u;
        })
      )
      .catch((e) => setFrameError(e.message));
  }

  useEffect(() => {
    loadFrame();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gate.camera_id]);

  function point(e) {
    const r = areaRef.current.getBoundingClientRect();
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))].map(
      (v) => +v.toFixed(4)
    );
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
      await api.addFootfallCamera(gate.camera_id, box ? toPolygon(box) : null);
      await api.setFootfallEntry(gate.camera_id, line.length === 2 ? line : null, insideSign);
      setMessage(
        line.length === 2
          ? "Saved. Only people who walk in through the gate (crossing the line towards the arrow) are counted."
          : "Saved without an entry line: everyone seen in the area is counted. Draw the line to count only entries."
      );
      onSaved?.();
    } catch (e) {
      setMessage(e?.message || "Couldn't save. Check the backend is running.");
    } finally {
      setSaving(false);
    }
  }

  const arrow = line.length === 2 ? officeArrow(line, insideSign) : null;
  const b = box && {
    left: `${Math.min(box.x0, box.x1) * 100}%`,
    top: `${Math.min(box.y0, box.y1) * 100}%`,
    width: `${Math.abs(box.x1 - box.x0) * 100}%`,
    height: `${Math.abs(box.y1 - box.y0) * 100}%`,
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap gap-1">
        {[
          ["line", "1. Entry line across the gate"],
          ["area", "2. Area around the gate (optional)"],
        ].map(([key, label]) => (
          <button
            key={key}
            type="button"
            onClick={() => setMode(key)}
            className={`px-3 py-1.5 rounded-full text-sm font-medium ${mode === key ? "bg-brand-500 text-white" : "bg-white border border-border-200 text-slate-600"}`}
          >
            {label}
          </button>
        ))}
        <button type="button" onClick={loadFrame} className="ml-auto px-3 py-1.5 rounded-lg text-sm text-slate-500 flex items-center gap-1.5">
          <RefreshCw size={14} /> New picture
        </button>
      </div>

      <div
        ref={areaRef}
        className="relative w-full aspect-video bg-[#11151c] rounded-lg overflow-hidden select-none touch-none cursor-crosshair"
        onPointerDown={onDown}
        onPointerMove={onMove}
        onPointerUp={onUp}
      >
        {frameUrl ? (
          <img src={frameUrl} alt={`Live picture from ${gate.name}`} className="w-full h-full object-fill pointer-events-none" draggable={false} />
        ) : (
          <p className="absolute inset-0 flex items-center justify-center text-sm text-slate-300 px-4 text-center">
            {frameError || "Loading a picture from the camera…"}
          </p>
        )}
        {b && <div className="absolute border-2 border-brand-500 bg-brand-500/15 pointer-events-none" style={b} />}
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" className="absolute inset-0 w-full h-full pointer-events-none">
          {line.length === 2 && (
            <polyline points={line.map(([x, y]) => `${x * 100},${y * 100}`).join(" ")} stroke={LINE} strokeWidth="4" vectorEffect="non-scaling-stroke" />
          )}
          {arrow && (
            <line x1={arrow.mx * 100} y1={arrow.my * 100} x2={arrow.ex * 100} y2={arrow.ey * 100} stroke={LINE} strokeWidth="3" strokeDasharray="3 2" vectorEffect="non-scaling-stroke" />
          )}
        </svg>
        {line.map(([x, y], i) => (
          <span key={i} className="absolute w-2.5 h-2.5 -ml-[5px] -mt-[5px] rounded-full border-2 border-white pointer-events-none" style={{ left: `${x * 100}%`, top: `${y * 100}%`, background: LINE }} />
        ))}
        {arrow && (
          <span
            className="absolute -translate-x-1/2 -translate-y-1/2 text-[11px] font-semibold text-black px-1.5 py-0.5 rounded pointer-events-none"
            style={{ left: `${arrow.ex * 100}%`, top: `${arrow.ey * 100}%`, background: LINE }}
          >
            Inside
          </span>
        )}
      </div>

      <p className="text-xs text-slate-500">
        {mode === "line"
          ? "Click two points across the gate, on the floor where people's feet cross it. Then make sure the arrow points INTO the premises (use Flip if not). Only people who cross the line in the arrow's direction are counted."
          : "Optional: drag a box around the gate. Only people inside the box take part, which keeps a busy street or corridor out."}
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => setInsideSign((s) => -s)} disabled={line.length !== 2} className="btn-secondary text-sm flex items-center gap-1.5 disabled:opacity-50">
          <ArrowLeftRight size={14} /> Flip inside side
        </button>
        <button type="button" onClick={() => (mode === "line" ? setLine([]) : setBox(null))} className="btn-secondary text-sm flex items-center gap-1.5">
          <Undo2 size={14} /> {mode === "line" ? "Clear line" : "Clear area"}
        </button>
        <button type="button" onClick={save} disabled={saving || line.length === 1} className="btn-primary text-sm ml-auto disabled:opacity-50">
          {saving ? "Saving…" : "Save gate & count entries"}
        </button>
      </div>
      {line.length !== 2 && (
        <p className="text-xs text-warning-600">
          No entry line yet: without it, everyone seen in the area is counted, including people outside the gate.
        </p>
      )}
      {message && <p className="text-sm text-ink-900">{message}</p>}
    </div>
  );
}
