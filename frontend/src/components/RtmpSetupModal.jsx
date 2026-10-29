import { useEffect, useState } from "react";
import { Copy, Check, RefreshCw } from "lucide-react";
import Modal from "./Modal";
import * as api from "../api/client";

// What a camera's status means on this screen. "Online" is only ever shown when
// the backend reports frames actually arriving from the camera.
const STATUS = {
  online: { text: "Online — video is arriving", tone: "text-success-600" },
  receiving: { text: "Stream received, starting video…", tone: "text-warning-600" },
  offline: { text: "Waiting for the camera to start streaming", tone: "text-slate-500" },
  unknown: { text: "Waiting for the camera to start streaming", tone: "text-slate-500" },
};

function CopyField({ label, value, mono = true }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }
  return (
    <div>
      <p className="text-sm font-medium text-ink-900 mb-1.5">{label}</p>
      <div className="flex gap-2">
        <input readOnly value={value} className={`input-field flex-1 ${mono ? "font-mono text-xs" : ""}`} onFocus={(e) => e.target.select()} />
        <button type="button" onClick={copy} className="btn-secondary text-sm whitespace-nowrap flex items-center gap-1.5">
          {copied ? <Check size={14} /> : <Copy size={14} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  );
}

// RTMP push setup screen: shown right after an RTMP-push camera is created
// (with the details the backend returned) and from the camera's edit form.
// `initial` is the backend's setup payload or null; `initialError` explains why
// there is none (e.g. RTMP push not configured on this server).
export default function RtmpSetupModal({ open, cameraId, cameraName, initial, initialError, onClose }) {
  const [setup, setSetup] = useState(initial || null);
  const [error, setError] = useState(initialError || "");
  const [rotating, setRotating] = useState(false);

  useEffect(() => {
    if (!open || !cameraId) return undefined;
    let stop = false;
    async function load() {
      try {
        const s = await api.getRtmpSetup(cameraId);
        if (!stop) {
          setSetup(s);
          setError("");
        }
      } catch (err) {
        if (!stop) setError(err.message);
      }
    }
    load();
    const t = setInterval(load, 5000); // status refresh while the screen is open
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, [open, cameraId]);

  async function rotate() {
    if (!window.confirm("Create a new stream key? The current key stops working immediately and the camera must be given the new one.")) return;
    setRotating(true);
    try {
      setSetup(await api.rotateRtmpKey(cameraId));
      setError("");
    } catch (err) {
      setError(err.message);
    } finally {
      setRotating(false);
    }
  }

  const status = STATUS[setup?.status] || STATUS.unknown;
  return (
    <Modal open={open} onClose={onClose} title={`RTMP setup${cameraName ? ` — ${cameraName}` : ""}`} width="max-w-lg">
      <div className="space-y-4">
        {error && !setup && <p className="text-sm text-danger-500">{error}</p>}
        {setup && (
          <>
            <CopyField label="RTMP server" value={setup.server_url} />
            <CopyField label="Stream key" value={setup.stream_key} />
            <CopyField label="Full URL (one-field devices)" value={setup.full_url} />

            <div className="rounded-lg bg-[#f5f6fa] px-3 py-2.5 text-xs text-slate-600 space-y-1.5">
              <p>
                In the camera's (or NVR's) web interface, open the RTMP / live-streaming settings — often{" "}
                <span className="font-medium">Settings → Stream → Custom</span>, though menu names vary by manufacturer.
              </p>
              <p>
                Enter the <span className="font-medium">RTMP server</span> and <span className="font-medium">Stream key</span> in their
                fields, or paste the <span className="font-medium">Full URL</span> if the device has a single field. Enable the stream and save.
              </p>
              <p>
                The camera must support RTMP push. If it only offers RTSP, add it with the RTSP option instead, or use an NVR,
                encoder or gateway that can publish to RTMP.
              </p>
              <p>Keep the stream key private: anyone with it can send video as this camera.</p>
            </div>

            <div className="flex items-center justify-between">
              <p className={`text-sm font-medium ${status.tone}`}>{status.text}</p>
              <button type="button" onClick={rotate} disabled={rotating} className="btn-secondary text-xs flex items-center gap-1.5 disabled:opacity-50">
                <RefreshCw size={13} /> {rotating ? "Creating…" : "New stream key"}
              </button>
            </div>
            <p className="text-xs text-slate-400">
              The camera shows as Online only after the server receives a valid video stream from it.
            </p>
            {error && <p className="text-xs text-danger-500">{error}</p>}
          </>
        )}
        <div className="flex justify-end">
          <button type="button" onClick={onClose} className="btn-primary text-sm">
            Done
          </button>
        </div>
      </div>
    </Modal>
  );
}
