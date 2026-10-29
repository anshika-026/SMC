import { useEffect, useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import Modal from "./Modal";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import { fmtDateTime } from "./VisitorAlerts";

const MAX_TRIES = 10;
const RETRY_MS = 3000;

export default function VisitorEventDetail({ eventId, initial, onClose, onDeleted }) {
  const [ev, setEv] = useState(initial || null);
  const [gone, setGone] = useState("");
  const [clipUrl, setClipUrl] = useState("");
  const [clipMsg, setClipMsg] = useState("");
  const [clipLoading, setClipLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const urlRef = useRef("");

  // Keep the event fresh (status, clip state) while the dialog is open.
  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .getVisitorEvent(eventId)
        .then((e) => !cancelled && setEv(e))
        .catch((e) => {
          if (cancelled) return;
          if (e.status === 404) setGone("This event has expired and was deleted.");
        });
    load();
    const h = setVisibleInterval(load, 4000);
    return () => {
      cancelled = true;
      clearVisibleInterval(h);
    };
  }, [eventId]);

  const clipStatus = ev?.clip_status;
  const clipError = ev?.clip_error;
  useEffect(() => {
    if (!clipStatus) return;
    let cancelled = false;
    let timer = null;
    const drop = () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      urlRef.current = "";
      setClipUrl("");
    };
    drop();
    if (clipStatus === "failed") {
      setClipLoading(false);
      setClipMsg(clipError ? `No clip: ${clipError}` : "No clip was recorded for this event.");
      return;
    }
    setClipLoading(true);
    setClipMsg("");
    const attempt = (n) => {
      api
        .fetchVisitorClipObjectUrl(eventId)
        .then((url) => {
          if (cancelled) return URL.revokeObjectURL(url);
          urlRef.current = url;
          setClipUrl(url);
          setClipLoading(false);
        })
        .catch((e) => {
          if (cancelled) return;
          if (e.status === 409 && n < MAX_TRIES) {
            setClipMsg("Clip is still being recorded — it will appear in a few seconds…");
            timer = setTimeout(() => attempt(n + 1), RETRY_MS);
            return;
          }
          setClipLoading(false);
          if (e.status === 409) setClipMsg("The clip is still being recorded. Close and reopen this event in a moment.");
          else if (e.status === 410) setClipMsg("This clip was deleted (it expired).");
          else if (e.status === 404) setClipMsg(clipError ? `No clip: ${clipError}` : "No clip is available for this event.");
          else if (e.status) setClipMsg(e.message);
          else setClipMsg("Could not reach the server to load the clip. Check your connection.");
        });
    };
    attempt(1);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      drop();
    };
  }, [eventId, clipStatus, clipError]);

  async function remove() {
    if (!window.confirm(`Delete visitor event #${eventId} and its clip? This cannot be undone.`)) return;
    setBusy(true);
    setError("");
    try {
      await api.deleteVisitorEvent(eventId);
      onDeleted(eventId);
    } catch (e) {
      setError(e.message);
      setBusy(false);
    }
  }

  const rows = ev
    ? [
        ["Event ID", `#${ev.id}`],
        ["Camera", ev.camera_name],
        ["Track ID", ev.track_id],
        ["Entered", fmtDateTime(ev.entered_at)],
        ...(ev.alert_seq > 1 ? [["Alert", `Repeat alert ${ev.alert_seq} at ${fmtDateTime(ev.alert_at)} (still seated)`]] : []),
        ["Departure", ev.left_at ? fmtDateTime(ev.left_at) : "Still inside"],
        ...(ev.left_at ? [["How departure was determined", ev.departure === "tracked" ? "Detected" : ev.departure === "assumed" ? "Assumed" : "-"]] : []),
      ]
    : [];

  return (
    <Modal open onClose={onClose} title={`Visitor event #${eventId}`} width="max-w-3xl">
      {gone && <p className="text-sm text-danger-600 mb-3">{gone}</p>}
      <div className="aspect-video bg-ink-900 rounded-xl overflow-hidden flex items-center justify-center">
        {clipUrl ? (
          <video src={clipUrl} controls autoPlay muted playsInline className="w-full h-full" />
        ) : (
          <p className="text-white/50 text-sm text-center px-6">
            {clipLoading ? clipMsg || "Loading clip…" : clipMsg || "No clip available."}
          </p>
        )}
      </div>

      {ev && (
        <dl className="mt-4 grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-2 text-sm">
          {rows.map(([k, v]) => (
            <div key={k}>
              <dt className="text-xs text-slate-400">{k}</dt>
              <dd className="text-ink-900">{v}</dd>
            </div>
          ))}
        </dl>
      )}
      {ev?.expires_at && (
        <p className="mt-3 text-xs text-slate-500">Clip is deleted automatically on {fmtDateTime(ev.expires_at)}</p>
      )}
      {error && <p className="mt-2 text-xs text-danger-600">{error}</p>}
      <div className="mt-4 flex justify-end">
        <button type="button" className="btn-secondary text-danger-600 inline-flex items-center gap-1.5" disabled={busy} onClick={remove}>
          <Trash2 size={14} /> Delete event
        </button>
      </div>
    </Modal>
  );
}
