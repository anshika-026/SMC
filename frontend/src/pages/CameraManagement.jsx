import { useEffect, useState } from "react";
import { Pencil, Trash2, Video } from "lucide-react";
import PageHeader from "../components/PageHeader";
import StatCard from "../components/StatCard";
import DataTable from "../components/DataTable";
import StatusBadge from "../components/StatusBadge";
import Modal from "../components/Modal";
import RtmpSetupModal from "../components/RtmpSetupModal";
import Tabs from "../components/Tabs";
import ToggleSwitch from "../components/ToggleSwitch";
import * as api from "../api/client";
import { featureOn } from "../lib/features";

// The backend has no per-camera health metric yet, so we derive a
// deterministic pseudo-random score (70-100%) per camera id — stable across
// renders/refreshes for the same camera, but clearly a local UI mock.
function healthForCamera(id) {
  const seed = String(id)
    .split("")
    .reduce((acc, ch) => (acc * 31 + ch.charCodeAt(0)) % 100000, 7);
  return 70 + (seed % 31);
}

function healthTone(pct) {
  if (pct >= 90) return "bg-success-500";
  if (pct >= 80) return "bg-warning-500";
  return "bg-danger-500";
}

const VIEWS = ["All", "Camera Health"];
const PURPOSES = ["General", "Theft", "Entry/Exit", "Surveillance", "Safety"];
const EMPTY_FORM = { protocol: "rtsp", streamUrl: "", driveName: "", site: "", purpose: "General", code: "", attendanceTracking: true };

// Per-protocol wording for the stream link field.
const SOURCE_TEXT = {
  rtsp: {
    label: "RTSP link",
    placeholder: "rtsp://user:password@192.168.1.10:554/stream",
    hint: "Paste the full link from the camera or NVR, including the login.",
  },
  rtmp: {
    label: "RTMP link",
    placeholder: "rtmp://server:1935/app/stream-key",
    hint: "Paste the full RTMP link, including the application name and stream key.",
  },
};

const PROTOCOL_LABELS = { rtsp: "RTSP", rtmp: "RTMP link", rtmp_push: "RTMP Push — camera streams to us" };

// Shown instead of a link field when the camera pushes to us.
function PushExplainer() {
  return (
    <div className="rounded-lg bg-[#f5f6fa] px-3 py-2.5 text-xs text-slate-600 space-y-1">
      <p>
        No link needed: when you save, this server creates a private stream key for the camera and shows the RTMP server,
        stream key and full URL to enter in the camera.
      </p>
      <p>
        The camera (or its NVR/encoder) must support RTMP push. Cameras that only offer RTSP should be added with the RTSP
        option.
      </p>
    </div>
  );
}

// RTSP / RTMP choice for the Add and Edit Camera forms.
function ProtocolSwitch({ value, onChange }) {
  return (
    <div className="flex flex-wrap gap-2" role="radiogroup" aria-label="Stream protocol">
      {api.CAMERA_PROTOCOLS.map((p) => (
        <button
          key={p}
          type="button"
          role="radio"
          aria-checked={value === p}
          onClick={() => onChange(p)}
          className={`px-3 py-1.5 rounded-lg text-sm font-medium border ${
            value === p ? "bg-brand-500 text-white border-brand-500" : "border-border-200 text-ink-900"
          }`}
        >
          {PROTOCOL_LABELS[p] || p.toUpperCase()}
        </button>
      ))}
    </div>
  );
}

// RTMP: early feedback only; the backend validates the link again.
function describeSource(url, protocol) {
  if (protocol === api.PUSH_PROTOCOL) return { push: true }; // nothing to type: the key is generated
  if (protocol !== "rtmp") return describeRtsp(url);
  if (!url.trim()) return null;
  const p = api.parseRtmpUrl(url);
  return p.error ? { error: p.error } : { ...p, rtmp: true };
}

// What the Add Camera form can tell from the link alone, before testing it.
function describeRtsp(url) {
  if (!url.trim()) return null;
  if (!/^rtsp:\/\//i.test(url.trim())) return { error: "The link must start with rtsp://" };
  const p = api.parseRtspUrl(url.trim());
  if (!p.host) return { error: "The link has no camera address after the @" };
  return p;
}

export default function CameraManagement() {
  const [cameras, setCameras] = useState([]);
  const [addOpen, setAddOpen] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [sites, setSites] = useState([]);
  const [test, setTest] = useState({ state: "idle", url: "", message: "" });
  const [saving, setSaving] = useState(false);
  const [addError, setAddError] = useState("");
  const [view, setView] = useState("All");
  const [editing, setEditing] = useState(null);
  const [toast, setToast] = useState("");
  // RTMP push setup screen: { id, name, initial, error } or null.
  const [setupFor, setSetupFor] = useState(null);

  useEffect(() => {
    refresh();
  }, []);

  function refresh() {
    api.getCameras().then(setCameras);
  }

  function showToast(msg) {
    setToast(msg);
    setTimeout(() => setToast(""), 2500);
  }

  const online = cameras.filter((c) => c.status === "Active").length;
  const rtspInfo = describeSource(form.streamUrl, form.protocol);
  const sourceText = SOURCE_TEXT[form.protocol] || SOURCE_TEXT.rtsp;
  const avgHealth = cameras.length
    ? Math.round(cameras.reduce((sum, c) => sum + healthForCamera(c.id), 0) / cameras.length)
    : null;

  function openAdd() {
    setForm(EMPTY_FORM);
    setTest({ state: "idle", url: "", message: "" });
    setAddError("");
    setAddOpen(true);
    api
      .getSites()
      .then((list) => {
        setSites(list);
        if (list.length === 1) setForm((f) => ({ ...f, site: f.site || list[0].name }));
      })
      .catch(() => setSites([]));
  }

  async function runTest() {
    setTest({ state: "testing", url: "", message: "" });
    try {
      const url = await api.testCameraStream(form.streamUrl.trim(), form.protocol);
      setTest({ state: "ok", url, message: "" });
    } catch (err) {
      setTest({ state: "failed", url: "", message: err.message });
    }
  }

  async function handleAdd(e) {
    e.preventDefault();
    const parsed = describeSource(form.streamUrl, form.protocol);
    if (!parsed || parsed.error) {
      setAddError(parsed?.error || `Paste the camera's ${form.protocol === "rtmp" ? "RTMP" : "RTSP"} link`);
      return;
    }
    setSaving(true);
    setAddError("");
    try {
      const created = await api.addCamera({ ...form, streamUrl: form.streamUrl.trim() });
      setAddOpen(false);
      refresh();
      if (form.protocol === api.PUSH_PROTOCOL) {
        // Only after the backend confirmed the camera and returned its real setup.
        setSetupFor({ id: created.id, name: created.name, initial: created.rtmp_setup, error: created.rtmp_setup_error || "" });
      } else {
        showToast(`${form.driveName} added. Open Live Feed to watch it.`);
      }
    } catch (err) {
      // The backend explains a rejected link (never echoing it back).
      setAddError(err?.message && !/^Request failed/i.test(err.message) ? err.message : "Couldn't save the camera. Check the backend is running.");
    } finally {
      setSaving(false);
    }
  }

  function openEdit(cam) {
    setEditing({
      id: cam.id,
      camCode: cam.code,
      label: cam.label,
      purpose: cam.purpose || "General",
      site: cam.site,
      protocol: cam.protocol || "rtsp",
      sourceUrlSet: cam.sourceUrlSet,
      rtmpKeySet: cam.rtmpKeySet,
      sourceDisplay: cam.sourceDisplay,
      // An RTMP link is never sent to the browser: the field starts blank and
      // blank means "keep the saved link".
      streamUrl:
        cam.protocol === "rtmp"
          ? ""
          : api.buildRtspUrl({
              user: cam.user,
              password: "",
              host: cam.host,
              port: cam.port,
              streamPath: cam.streamPath,
            }),
      attendanceTracking: cam.attendanceTracking,
      error: "",
    });
  }

  async function handleEditSubmit(e) {
    e.preventDefault();
    const common = {
      name: editing.label,
      cam_code: editing.camCode,
      purpose: editing.purpose,
      site: editing.site,
      attendance_tracking: editing.attendanceTracking,
    };
    let payload;
    if (editing.protocol === api.PUSH_PROTOCOL) {
      payload = { ...common, protocol: api.PUSH_PROTOCOL }; // the backend keeps (or creates) the key
    } else if (editing.protocol === "rtmp") {
      const typed = editing.streamUrl.trim();
      if (typed) {
        const check = api.parseRtmpUrl(typed);
        if (check.error) return setEditing((f) => ({ ...f, error: check.error }));
      } else if (!editing.sourceUrlSet) {
        return setEditing((f) => ({ ...f, error: "Enter the RTMP link, for example rtmp://host/app/stream-key" }));
      }
      // Blank = keep the saved link (it is never shown here).
      payload = { ...common, protocol: "rtmp", ...(typed && { source_url: typed }) };
    } else {
      const parsed = api.parseRtspUrl(editing.streamUrl);
      payload = {
        ...common,
        protocol: "rtsp",
        host: parsed.host,
        port: parsed.port,
        user: parsed.user,
        stream_path: parsed.streamPath,
      };
      // Password is never sent to the frontend, so the field only ever shows
      // it blank — only overwrite the stored password if the user actually
      // typed a new one into the URL.
      if (parsed.password) payload.password = parsed.password;
    }

    try {
      await api.updateCamera(editing.id, payload);
    } catch (err) {
      return setEditing((f) => ({ ...f, error: err?.message || "Couldn't save the camera." }));
    }
    setEditing(null);
    showToast("Camera updated");
    refresh();
  }

  // Feed off = the backend stops connecting to this camera entirely (no live
  // view, no analytics, no background streaming), freeing CPU and bandwidth.
  const [switching, setSwitching] = useState(null);
  async function toggleFeed(cam, on) {
    setSwitching(cam.id);
    try {
      await api.updateCamera(cam.id, { live_feed_enabled: on });
      showToast(on ? `${cam.label} feed switched on` : `${cam.label} feed switched off: it's no longer streamed or analysed`);
      refresh();
    } catch {
      showToast(`Couldn't change ${cam.label}. Check the backend is running.`);
    } finally {
      setSwitching(null);
    }
  }

  async function handleDelete(cam) {
    if (!window.confirm(`Remove camera "${cam.label}"? This cannot be undone.`)) return;
    await api.deleteCamera(cam.id);
    showToast(`${cam.label} removed`);
    refresh();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Camera Management"
        action={
          <button onClick={openAdd} className="btn-primary text-sm">
            + Add camera
          </button>
        }
      />

      {toast && (
        <div className="text-sm text-success-600 bg-success-50 border border-success-500/20 rounded-lg px-3.5 py-2">
          {toast}
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        <StatCard label="Total Cameras" value={cameras.length} subTone="neutral" />
        <StatCard label="Cameras Online" value={online} />
        <StatCard label="Cameras Offline" value={cameras.length - online} subTone="danger" />
        <StatCard label="Avg Camera Health" value={avgHealth === null ? "-" : `${avgHealth}%`} />
      </div>

      <Tabs tabs={VIEWS} active={view} onChange={setView} />

      {view === "All" ? (
        <DataTable
          columns={[
            { key: "code", label: "Cam Code" },
            { key: "label", label: "Label" },
            { key: "site", label: "Site" },
            { key: "purpose", label: "Purpose" },
            { key: "status", label: "Status", render: (r) => <StatusBadge value={r.status} /> },
            {
              key: "live",
              label: "Live feed",
              render: (r) => (
                <span className="flex items-center gap-2">
                  <ToggleSwitch checked={r.feedOn} onChange={(on) => toggleFeed(r, on)} disabled={!r.isConfigured || switching === r.id} />
                  <span className="text-xs text-slate-500">{r.feedOn ? "On" : "Off"}</span>
                </span>
              ),
            },
            {
              key: "action",
              label: "Actions",
              render: (r) => (
                <div className="flex items-center gap-3">
                  <button
                    onClick={() => openEdit(r)}
                    title="Edit"
                    className="text-slate-400 hover:text-brand-600"
                  >
                    <Pencil size={15} />
                  </button>
                  <button
                    onClick={() => handleDelete(r)}
                    title="Delete"
                    className="text-slate-400 hover:text-danger-500"
                  >
                    <Trash2 size={15} />
                  </button>
                </div>
              ),
            },
          ]}
          rows={cameras}
        />
      ) : (
        <div className="card divide-y divide-[#f1f2f7]">
          {cameras.length === 0 ? (
            <div className="text-center text-sm text-slate-400 py-10">
              No cameras yet — add one to see health data.
            </div>
          ) : (
            cameras.map((cam) => {
              const pct = healthForCamera(cam.id);
              return (
                <div key={cam.id} className="flex items-center gap-4 px-4 py-3.5">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-ink-900 truncate">{cam.label}</p>
                    <p className="text-xs text-slate-400 truncate">
                      {cam.site} · {cam.code}
                    </p>
                  </div>
                  <div className="w-40 h-2 rounded-full bg-[#eceef4] overflow-hidden hidden sm:block">
                    <div
                      className={`h-full rounded-full ${healthTone(pct)}`}
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                  <span className="text-sm font-semibold text-ink-900 w-10 text-right shrink-0">
                    {pct}%
                  </span>
                </div>
              );
            })
          )}
        </div>
      )}

      <Modal open={addOpen} onClose={() => setAddOpen(false)} title="Add Camera" width="max-w-lg">
        <form onSubmit={handleAdd} className="space-y-4">
          <div>
            <p className="text-sm font-medium text-ink-900 mb-1.5">Stream protocol</p>
            <ProtocolSwitch
              value={form.protocol}
              onChange={(p) => {
                setForm((f) => ({ ...f, protocol: p, streamUrl: "" }));
                setTest({ state: "idle", url: "", message: "" });
                setAddError("");
              }}
            />
          </div>
          {form.protocol === api.PUSH_PROTOCOL && <PushExplainer />}
          {form.protocol !== api.PUSH_PROTOCOL && (
          <div>
            <label htmlFor="add-rtsp" className="text-sm font-medium text-ink-900 block mb-1.5">
              {sourceText.label} <span className="text-danger-500">*</span>
            </label>
            <div className="flex gap-2">
              <input
                id="add-rtsp"
                required
                autoFocus
                value={form.streamUrl}
                onChange={(e) => {
                  setForm((f) => ({ ...f, streamUrl: e.target.value }));
                  setTest({ state: "idle", url: "", message: "" });
                }}
                placeholder={sourceText.placeholder}
                autoComplete="off"
                spellCheck={false}
                className="input-field font-mono text-xs flex-1"
              />
              <button
                type="button"
                onClick={runTest}
                disabled={!rtspInfo || !!rtspInfo.error || test.state === "testing"}
                className="btn-secondary text-sm whitespace-nowrap disabled:opacity-50"
              >
                {test.state === "testing" ? "Testing…" : "Test"}
              </button>
            </div>
            {!rtspInfo && <p className="text-xs text-slate-400 mt-1.5">{sourceText.hint}</p>}
            {rtspInfo?.error && <p className="text-xs text-danger-500 mt-1.5">{rtspInfo.error}</p>}
            {rtspInfo && !rtspInfo.error && rtspInfo.rtmp && (
              <p className="text-xs text-slate-500 mt-1.5">
                Server <span className="font-mono">{rtspInfo.host}:{rtspInfo.port}</span> · stream key kept private
              </p>
            )}
            {rtspInfo && !rtspInfo.error && !rtspInfo.rtmp && (
              <p className="text-xs text-slate-500 mt-1.5">
                Camera <span className="font-mono">{rtspInfo.host}:{rtspInfo.port}</span>
                {rtspInfo.user && (
                  <>
                    {" "}· user <span className="font-mono">{rtspInfo.user}</span>
                  </>
                )}
                {rtspInfo.password ? " · password included" : " · no password"} · path{" "}
                <span className="font-mono">{rtspInfo.streamPath}</span>
              </p>
            )}
            {test.state === "ok" && (
              <div className="mt-2 rounded-lg overflow-hidden border border-border-200">
                <img src={test.url} alt="Still from the camera" className="w-full aspect-video object-cover" />
                <p className="text-xs text-success-600 px-3 py-1.5">Connected. This is what the camera sees right now.</p>
              </div>
            )}
            {test.state === "failed" && <p className="text-xs text-danger-500 mt-1.5">{test.message}</p>}
          </div>
          )}

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="add-name" className="text-sm font-medium text-ink-900 block mb-1.5">
                Camera name <span className="text-danger-500">*</span>
              </label>
              <input
                id="add-name"
                required
                value={form.driveName}
                onChange={(e) => setForm((f) => ({ ...f, driveName: e.target.value }))}
                placeholder="e.g. Gate 2"
                className="input-field"
              />
            </div>
            <div>
              <label htmlFor="add-site" className="text-sm font-medium text-ink-900 block mb-1.5">
                Site <span className="text-danger-500">*</span>
              </label>
              <input
                id="add-site"
                required
                list="add-site-options"
                value={form.site}
                onChange={(e) => setForm((f) => ({ ...f, site: e.target.value }))}
                placeholder="Noida Site"
                className="input-field"
              />
              <datalist id="add-site-options">
                {sites.map((site) => (
                  <option key={site.id} value={site.name} />
                ))}
              </datalist>
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label htmlFor="add-purpose" className="text-sm font-medium text-ink-900 block mb-1.5">
                Purpose
              </label>
              <select
                id="add-purpose"
                value={form.purpose}
                onChange={(e) => setForm((f) => ({ ...f, purpose: e.target.value }))}
                className="input-field"
              >
                {PURPOSES.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="add-code" className="text-sm font-medium text-ink-900 block mb-1.5">
                Cam code (optional)
              </label>
              <input
                id="add-code"
                value={form.code}
                onChange={(e) => setForm((f) => ({ ...f, code: e.target.value }))}
                placeholder="CAM-XXXX"
                className="input-field"
              />
            </div>
          </div>
          {form.purpose === "Entry/Exit" && (
            <p className="text-xs text-brand-600 -mt-2">
              Entry/Exit cameras count unique footfall. Draw the doorway zone on the Footfall UAT page after adding.
            </p>
          )}

          {featureOn("face_recognition") && (
          <label className="flex items-start gap-2.5 cursor-pointer">
            <input
              type="checkbox"
              checked={form.attendanceTracking}
              onChange={(e) => setForm((f) => ({ ...f, attendanceTracking: e.target.checked }))}
              className="mt-0.5 accent-brand-500"
            />
            <span>
              <span className="block text-sm font-medium text-ink-900">Run face recognition on this camera</span>
              <span className="block text-xs text-slate-500">
                Leave off for live view only. Face recognition uses a lot of CPU per camera.
              </span>
            </span>
          </label>
          )}

          {addError && <p className="text-sm text-danger-500">{addError}</p>}
          <button
            type="submit"
            disabled={saving}
            className="btn-primary w-full flex items-center justify-center gap-2 disabled:opacity-60"
          >
            <Video size={15} /> {saving ? "Adding…" : "Add camera"}
          </button>
        </form>
      </Modal>

      {/* --- Edit Camera ------------------------------------------------ */}
      <Modal open={!!editing} onClose={() => setEditing(null)} title="Edit Camera" width="max-w-lg">
        {editing && (
          <form onSubmit={handleEditSubmit} className="space-y-5">
            <p className="text-sm text-slate-500 -mt-3">Edit a camera and assign its site access.</p>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-sm font-medium text-ink-900 block mb-1.5">
                  Camera Code <span className="text-danger-500">*</span>
                </label>
                <input
                  required
                  value={editing.camCode}
                  onChange={(e) => setEditing((f) => ({ ...f, camCode: e.target.value }))}
                  className="input-field"
                />
              </div>
              <div>
                <label className="text-sm font-medium text-ink-900 block mb-1.5">
                  Camera Label <span className="text-danger-500">*</span>
                </label>
                <input
                  required
                  value={editing.label}
                  onChange={(e) => setEditing((f) => ({ ...f, label: e.target.value }))}
                  className="input-field"
                />
              </div>
            </div>

            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="text-sm font-medium text-ink-900 block mb-1.5">
                  Purpose <span className="text-danger-500">*</span>
                </label>
                <select
                  required
                  value={editing.purpose}
                  onChange={(e) => setEditing((f) => ({ ...f, purpose: e.target.value }))}
                  className="input-field"
                >
                  {[...new Set([editing.purpose, ...PURPOSES])].map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="text-sm font-medium text-ink-900 block mb-1.5">Site</label>
                <input
                  required
                  value={editing.site}
                  onChange={(e) => setEditing((f) => ({ ...f, site: e.target.value }))}
                  className="input-field"
                />
              </div>
            </div>

            <div className="pt-1">
              <p className="text-sm font-semibold text-ink-900 mb-3">Stream Configuration</p>
              <div className="mb-3">
                <ProtocolSwitch
                  value={editing.protocol}
                  onChange={(p) =>
                    setEditing((f) => {
                      // Back to RTSP on an RTSP camera restores its saved address
                      // (password blank as always); otherwise the field starts empty.
                      const saved = cameras.find((c) => c.id === f.id);
                      const rtspUrl =
                        saved && saved.protocol === "rtsp"
                          ? api.buildRtspUrl({ user: saved.user, password: "", host: saved.host, port: saved.port, streamPath: saved.streamPath })
                          : "";
                      return { ...f, protocol: p, error: "", streamUrl: p === "rtsp" ? rtspUrl : "" };
                    })
                  }
                />
              </div>
              {editing.protocol === api.PUSH_PROTOCOL &&
                (editing.rtmpKeySet && cameras.find((c) => c.id === editing.id)?.protocol === api.PUSH_PROTOCOL ? (
                  <button
                    type="button"
                    className="btn-secondary text-sm"
                    onClick={() => setSetupFor({ id: editing.id, name: editing.label, initial: null, error: "" })}
                  >
                    Show RTMP setup (server, stream key, full URL)
                  </button>
                ) : (
                  <PushExplainer />
                ))}
              {editing.protocol !== api.PUSH_PROTOCOL && (
              <>
              <label className="text-sm font-medium text-ink-900 block mb-1.5">
                {editing.protocol === "rtmp" ? "RTMP link" : "Stream URL"}{" "}
                {!(editing.protocol === "rtmp" && editing.sourceUrlSet) && <span className="text-danger-500">*</span>}
              </label>
              <input
                required={!(editing.protocol === "rtmp" && editing.sourceUrlSet)}
                value={editing.streamUrl}
                onChange={(e) => setEditing((f) => ({ ...f, streamUrl: e.target.value, error: "" }))}
                placeholder={
                  editing.protocol === "rtmp"
                    ? editing.sourceDisplay || SOURCE_TEXT.rtmp.placeholder
                    : "rtsp://user:password@host:port/path"
                }
                autoComplete="off"
                spellCheck={false}
                className="input-field font-mono text-xs"
              />
              <p className="text-xs text-slate-400 mt-1.5">
                {editing.protocol === "rtmp"
                  ? editing.sourceUrlSet
                    ? "The saved RTMP link (with its stream key) is hidden for security. Leave this blank to keep it, or paste a new link to replace it."
                    : SOURCE_TEXT.rtmp.hint
                  : "The saved password is hidden for security and shown blank here. Leave it blank to keep the current password, or type the full URL with a new password to change it."}
              </p>
              </>
              )}
              {editing.error && <p className="text-xs text-danger-500 mt-1.5">{editing.error}</p>}
            </div>

            {featureOn("face_recognition") && (
            <label className="flex items-start gap-2.5 cursor-pointer">
              <input
                type="checkbox"
                checked={editing.attendanceTracking}
                onChange={(e) => setEditing((f) => ({ ...f, attendanceTracking: e.target.checked }))}
                className="mt-0.5 accent-brand-500"
              />
              <span>
                <span className="block text-sm font-medium text-ink-900">Enable attendance tracking</span>
                <span className="block text-xs text-slate-500">
                  Attendance events and employee check-ins will be processed using this camera.
                </span>
              </span>
            </label>
            )}

            <div className="flex items-center gap-3 pt-1">
              <button type="button" onClick={() => setEditing(null)} className="btn-secondary flex-1">
                Cancel
              </button>
              <button type="submit" className="btn-primary flex-1">
                Submit
              </button>
            </div>
          </form>
        )}
      </Modal>

      {setupFor && (
        <RtmpSetupModal
          open
          cameraId={setupFor.id}
          cameraName={setupFor.name}
          initial={setupFor.initial}
          initialError={setupFor.error}
          onClose={() => setSetupFor(null)}
        />
      )}
    </div>
  );
}
