import { useEffect, useState } from "react";
import { ImageOff } from "lucide-react";
import * as api from "../../api/client";

// Small helpers shared by the visitor records screens.

export const clock = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "—");

export const dateTime = (ts) =>
  ts ? new Date(ts * 1000).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "—";

export const niceDate = (iso) =>
  iso ? new Date(`${iso}T12:00:00`).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" }) : "—";

// Seconds as "45s", "12m", "1h 05m"; null -> the given fallback (never a made-up value).
export function dur(seconds, fallback = "—") {
  if (seconds === null || seconds === undefined) return fallback;
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s`;
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m}m` : `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}

// "2026-10-30T14:05" for <input type="datetime-local"> and back.
export function toLocalInput(ts) {
  const d = ts ? new Date(ts * 1000) : new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
export const fromLocalInput = (s) => (s ? new Date(s).getTime() / 1000 : null);

const STATUS = {
  in: { label: "IN", cls: "bg-emerald-50 text-emerald-700 ring-emerald-200" },
  out: { label: "OUT", cls: "bg-slate-100 text-slate-600 ring-slate-200" },
  pending: { label: "Pending", cls: "bg-amber-50 text-amber-700 ring-amber-200" },
  cancelled: { label: "Cancelled", cls: "bg-rose-50 text-rose-600 ring-rose-200" },
};

export function StatusPill({ status }) {
  const s = STATUS[status] || STATUS.pending;
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-semibold ring-1 ring-inset ${s.cls}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${status === "in" ? "bg-emerald-500" : status === "out" ? "bg-slate-400" : status === "cancelled" ? "bg-rose-400" : "bg-amber-500"}`} />
      {s.label}
    </span>
  );
}

export const SOURCE_LABEL = { operator: "reception", camera: "camera, confirmed", correction: "corrected", system: "system" };

// An authenticated snapshot/clip (fetched as a blob, revoked on unmount).
export function MediaImage({ mediaId, className = "", alt = "Visitor snapshot" }) {
  const [url, setUrl] = useState(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (!mediaId) return undefined;
    let made = null;
    let live = true;
    api.vr
      .mediaUrl(mediaId)
      .then((u) => {
        made = u;
        if (live) setUrl(u);
        else URL.revokeObjectURL(u);
      })
      .catch(() => live && setFailed(true));
    return () => {
      live = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [mediaId]);
  if (!mediaId || failed)
    return (
      <div className={`grid place-items-center bg-slate-100 text-slate-400 ${className}`}>
        <ImageOff size={14} />
      </div>
    );
  if (!url) return <div className={`bg-slate-100 animate-pulse ${className}`} />;
  return <img src={url} alt={alt} className={`object-cover ${className}`} />;
}

export function MediaVideo({ mediaId, className = "" }) {
  const [url, setUrl] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let made = null;
    let live = true;
    api.vr
      .mediaUrl(mediaId)
      .then((u) => {
        made = u;
        if (live) setUrl(u);
        else URL.revokeObjectURL(u);
      })
      .catch((e) => live && setError(e.message));
    return () => {
      live = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [mediaId]);
  if (error) return <p className="text-xs text-slate-400">{error}</p>;
  if (!url) return <div className={`bg-slate-100 animate-pulse ${className}`} />;
  return <video src={url} controls className={`bg-black ${className}`} />;
}

export function Field({ label, children, hint }) {
  return (
    <label className="block">
      <span className="text-xs font-medium text-slate-600">{label}</span>
      <div className="mt-1">{children}</div>
      {hint && <span className="text-[11px] text-slate-400">{hint}</span>}
    </label>
  );
}
