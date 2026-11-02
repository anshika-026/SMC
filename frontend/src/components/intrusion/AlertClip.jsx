import { useEffect, useState } from "react";
import * as api from "../../api/client";

// The evidence video of an intrusion / loitering alert (backend/app/evidence.py). Ported from
// MAINDV's Alerts page: loaded once it is ready; while it is still being finalised the caller's
// onPending is called every few seconds (re-read the alert) so the player appears by itself.
// Shared by the Intrusion page, Alerts & Events and the dashboard's Security & Safety panel.

const CLIP_MESSAGES = {
  pending: "Preparing the video. It includes about 5 seconds after the event, so it appears shortly.",
  ready: "Loading video…",
  unavailable: "No video was available from the camera around this event.",
  failed: "The video for this alert could not be saved.",
  expired: "This video was removed after its retention period.",
};

export default function AlertClip({ alertId, clipStatus, label, onPending }) {
  const [clipUrl, setClipUrl] = useState(null);
  const [clipError, setClipError] = useState("");

  useEffect(() => {
    let url = null;
    let cancelled = false;
    setClipUrl(null);
    setClipError("");
    if (clipStatus === "ready") {
      api
        .fetchAlertClipObjectUrl(alertId)
        .then((u) => {
          if (cancelled) return URL.revokeObjectURL(u);
          url = u;
          setClipUrl(u);
          return undefined;
        })
        .catch((e) => !cancelled && setClipError(e.message || "The video could not be loaded."));
    }
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [alertId, clipStatus]);

  useEffect(() => {
    if (clipStatus !== "pending" || !onPending) return undefined;
    const t = setInterval(onPending, 3000);
    return () => clearInterval(t);
  }, [clipStatus, onPending]);

  async function download() {
    setClipError("");
    const name = (label || "alert").toLowerCase().replace(/[^a-z0-9]+/g, "-");
    try {
      await api.downloadAlertClip(alertId, `${name}-alert-${alertId}.mp4`);
    } catch (e) {
      setClipError(e.message || "The video could not be downloaded.");
    }
  }

  if (!clipStatus) return null;
  return (
    <div className="space-y-2">
      <div className="w-full rounded-xl bg-[#0c0c14] border border-[#23243a] overflow-hidden flex items-center justify-center min-h-[120px]">
        {clipUrl ? (
          <video src={clipUrl} controls playsInline preload="metadata" className="w-full max-h-80" aria-label={`Video for ${label || "the alert"}`} />
        ) : (
          <span role="status" className="text-xs text-white/60 px-4 py-6 text-center">
            {clipError || CLIP_MESSAGES[clipStatus] || "Loading video…"}
          </span>
        )}
      </div>
      {clipUrl && (
        <button type="button" onClick={download} className="btn-secondary w-full text-sm">
          Download Video
        </button>
      )}
      {clipError && clipUrl && <p className="text-xs text-danger-500">{clipError}</p>}
    </div>
  );
}
