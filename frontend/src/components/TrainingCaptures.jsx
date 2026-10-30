import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, Check, CheckSquare, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, HelpCircle, Trash2, UserCheck, X,
} from "lucide-react";
import Modal from "./Modal";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";

const PAGE_SIZE = 60;
const STATUS_REFRESH_MS = 15_000;

const STATUS_LABEL = {
  pending_review: "Pending",
  approved: "Approved",
  corrected: "Corrected",
  unknown: "Unknown",
  rejected: "Rejected",
};
const STATUS_BADGE = {
  pending_review: "badge-warning",
  approved: "badge-success",
  corrected: "badge-success",
  unknown: "badge-neutral",
  rejected: "badge-danger",
};

function fmtTime(sec) {
  if (!sec) return "—";
  const d = new Date(sec * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtDateTime(sec) {
  if (!sec) return "—";
  return new Date(sec * 1000).toLocaleString();
}
function fmtBytes(n) {
  if (!n) return "0 MB";
  return n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : `${(n / 1e6).toFixed(1)} MB`;
}
function pct(c) {
  return typeof c === "number" ? `${Math.round(c * 100)}%` : "—";
}
function predictedLabel(c) {
  if (!c.predicted_name) return "Person";
  return `${c.predicted_name} ${pct(c.confidence)}`;
}
function personLabel(p) {
  return `ID ${p.employee_id} — ${p.name}`;
}

// ---- Lazy, auth-fetched thumbnails ---------------------------------------
// At most 6 image requests at once; a tile only queues once it is near the
// viewport, and its object URL is revoked when the tile unmounts.
const queue = [];
let running = 0;
function pump() {
  while (running < 6 && queue.length) {
    const t = queue.shift();
    running++;
    t().finally(() => {
      running--;
      pump();
    });
  }
}
function enqueue(task) {
  queue.push(task);
  pump();
}

function CaptureImage({ id, lazy = true, className = "" }) {
  const holder = useRef(null);
  const [src, setSrc] = useState(null);
  const [failed, setFailed] = useState(false);
  const [near, setNear] = useState(!lazy);

  useEffect(() => {
    if (!lazy) return;
    const el = holder.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      setNear(true);
      return;
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setNear(true);
          io.disconnect();
        }
      },
      { rootMargin: "400px" }
    );
    io.observe(el);
    return () => io.disconnect();
  }, [lazy]);

  useEffect(() => {
    if (!near) return;
    let cancelled = false;
    let url = null;
    setSrc(null);
    setFailed(false);
    const run = () =>
      api
        .fetchCaptureImageUrl(id)
        .then((u) => {
          if (cancelled) URL.revokeObjectURL(u);
          else {
            url = u;
            setSrc(u);
          }
        })
        .catch(() => !cancelled && setFailed(true));
    if (lazy) enqueue(() => (cancelled ? Promise.resolve() : run()));
    else run();
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [near, id, lazy]);

  return (
    <div ref={holder} className={`bg-slate-100 flex items-center justify-center overflow-hidden ${className}`}>
      {src ? (
        <img src={src} alt="Face capture" className="w-full h-full object-contain" />
      ) : (
        <span className="text-[11px] text-slate-400">{failed ? "Image unavailable" : near ? "Loading…" : ""}</span>
      )}
    </div>
  );
}

// ---- Status strip -----------------------------------------------------------
function StatusStrip({ status, error, onChanged }) {
  const [busy, setBusy] = useState(false);
  const [workerError, setWorkerError] = useState("");
  async function toggleWorker(running) {
    if (running && !window.confirm("Stop capturing new face images? Images already saved are kept.")) return;
    setBusy(true);
    setWorkerError("");
    try {
      await (running ? api.stopCaptureWorker() : api.startCaptureWorker());
      onChanged?.();
    } catch (e) {
      setWorkerError(e.message);
    } finally {
      setBusy(false);
    }
  }
  if (error && !status) {
    return <div className="card p-3 text-sm text-danger-600">Could not load capture status: {error}</div>;
  }
  if (!status) return <div className="card p-3 text-sm text-slate-400">Loading capture status…</div>;
  const w = status.worker || {};
  const rc = status.review_counts || {};
  return (
    <div className="card p-4 space-y-3">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-sm">
        <div className="flex items-center gap-2">
          <span className={`badge ${w.running ? "badge-success" : "badge-danger"}`}>
            {w.running ? "Capture worker running" : "Capture worker is NOT running"}
          </span>
          {w.running && w.planned_end_at && (
            <span className="text-xs text-slate-500">planned end {new Date(w.planned_end_at * 1000).toLocaleDateString()}</span>
          )}
          {!w.running && <span className="text-xs text-slate-500">No new face crops are being captured.</span>}
          <button type="button" disabled={busy} onClick={() => toggleWorker(!!w.running)} className="btn-secondary !py-1 !px-3 text-xs">
            {busy ? "Working…" : w.running ? "Stop capture" : "Start capture"}
          </button>
          {workerError && <span className="text-xs text-danger-600">{workerError}</span>}
        </div>
        <span className="text-xs text-slate-500">Pending {rc.pending_review ?? 0}</span>
        <span className="text-xs text-slate-500">Approved {rc.approved ?? 0}</span>
        <span className="text-xs text-slate-500">Corrected {rc.corrected ?? 0}</span>
        <span className="text-xs text-slate-500">Unknown {rc.unknown ?? 0}</span>
        <span className="text-xs text-slate-500">Storage {fmtBytes(status.disk_usage_bytes)}</span>
        {typeof w.duplicates_rejected === "number" && (
          <span className="text-xs text-slate-500">Duplicates skipped {w.duplicates_rejected}</span>
        )}
      </div>
      <div className="flex flex-wrap gap-2">
        {(status.cameras || []).length === 0 && <span className="text-xs text-slate-400">No cameras reported.</span>}
        {(status.cameras || []).map((c) => (
          <span key={c.camera_id} className="badge badge-neutral">
            {c.name || c.camera_id}
            <span className={c.collecting ? "text-success-600" : "text-slate-400"}>
              {c.collecting ? "collecting" : "not collecting"}
            </span>
            <span className={c.recognition_running ? "text-success-600" : "text-danger-600"}>
              {c.recognition_running ? "recognition running" : "recognition stopped"}
            </span>
          </span>
        ))}
      </div>
    </div>
  );
}

// ---- Tile ---------------------------------------------------------------------
function Tile({ cap, selected, onToggle, onOpen }) {
  return (
    <div className={`card overflow-hidden relative ${selected ? "ring-2 ring-brand-500" : ""}`}>
      <label className="absolute top-1.5 left-1.5 z-10 bg-white/90 rounded p-1 cursor-pointer">
        <input type="checkbox" checked={selected} onChange={() => onToggle(cap.id)} aria-label="Select capture" />
      </label>
      <button type="button" onClick={() => onOpen(cap.id)} className="block w-full text-left">
        <CaptureImage id={cap.id} className="aspect-square w-full" />
        <div className="px-2.5 py-2 space-y-0.5">
          <div className="flex items-center justify-between gap-1">
            <span className="text-xs font-mono text-slate-500">{fmtTime(cap.captured_at)}</span>
            <span className={`badge ${STATUS_BADGE[cap.label_status] || "badge-neutral"}`}>
              {STATUS_LABEL[cap.label_status] || cap.label_status}
            </span>
          </div>
          <p className="text-xs font-medium text-ink-900 truncate">{predictedLabel(cap)}</p>
          {cap.verified_name && cap.label_status !== "approved" && (
            <p className="text-[11px] text-success-600 truncate">Verified: {cap.verified_name}</p>
          )}
          <p className="text-[11px] text-slate-400 truncate">{cap.camera_name || cap.camera_id}</p>
        </div>
      </button>
    </div>
  );
}

// ---- Detail view ---------------------------------------------------------------
function Row({ k, v }) {
  return (
    <div className="flex justify-between gap-3 text-sm py-1 border-b border-border-200/60">
      <span className="text-slate-500">{k}</span>
      <span className="text-ink-900 text-right">{v ?? "—"}</span>
    </div>
  );
}

function num(v, digits) {
  return v != null && !Number.isNaN(Number(v)) ? Number(v).toFixed(digits) : "—";
}

function CaptureDetail({ cap, index, count, people, busy, error, onPrev, onNext, onClose, onApprove, onCorrect, onUnknown, onDelete }) {
  const [choosing, setChoosing] = useState(false);
  const [target, setTarget] = useState(null);

  useEffect(() => {
    setChoosing(false);
    setTarget(null);
  }, [cap.id]);

  const q = cap.quality || {};
  const canApprove = !!cap.predicted_employee_id;
  return (
    <Modal open onClose={onClose} width="max-w-3xl" title={`Capture #${cap.id}`}>
      <div className="grid md:grid-cols-2 gap-5">
        <div className="space-y-2">
          <CaptureImage id={cap.id} lazy={false} className="w-full aspect-square rounded-xl" />
          <p className="text-[11px] text-slate-400">Only the face crop is stored (no full-frame image).</p>
          <div className="flex items-center justify-between">
            <button className="btn-secondary text-xs flex items-center gap-1" onClick={onPrev} disabled={index <= 0}>
              <ChevronLeft size={14} /> Previous
            </button>
            <span className="text-xs text-slate-400">
              {index + 1} of {count} loaded
            </span>
            <button className="btn-secondary text-xs flex items-center gap-1" onClick={onNext} disabled={index >= count - 1}>
              Next <ChevronRight size={14} />
            </button>
          </div>
        </div>

        <div>
          <Row k="Predicted" v={cap.predicted_name ? `${cap.predicted_name} (ID ${cap.predicted_employee_id})` : "Person"} />
          <Row k="Confidence" v={pct(cap.confidence)} />
          <Row k="Prediction tier" v={cap.prediction_tier} />
          <Row k="Review status" v={STATUS_LABEL[cap.label_status] || cap.label_status} />
          <Row k="Verified identity" v={cap.verified_name ? `${cap.verified_name} (ID ${cap.verified_employee_id})` : null} />
          <Row k="Reviewed" v={cap.reviewed_at ? `${fmtDateTime(cap.reviewed_at)}${cap.reviewed_by ? ` by ${cap.reviewed_by}` : ""}` : "Not yet"} />
          <Row k="Eligible for training" v={cap.training_eligible ? "Yes" : "No"} />
          <Row k="Captured" v={fmtDateTime(cap.captured_at)} />
          <Row k="Camera" v={cap.camera_name || cap.camera_id} />
          <Row k="Track" v={cap.track_id} />
          <Row k="Quality" v={`blur ${num(q.blur, 0)} · brightness ${num(q.brightness, 0)} · detection ${num(q.detection, 2)}`} />
        </div>
      </div>

      {error && <div className="mt-4 text-sm text-danger-600 bg-danger-50 rounded-lg px-3.5 py-2">{error}</div>}

      <div className="mt-5 flex flex-wrap gap-2">
        <button
          className="btn-primary text-sm flex items-center gap-1.5 disabled:opacity-50"
          disabled={!canApprove || busy}
          title={canApprove ? `Confirm this is ${cap.predicted_name}` : "There is no predicted name to confirm"}
          onClick={onApprove}
        >
          <Check size={15} /> Yes — Correct Name
        </button>
        <button className="btn-secondary text-sm flex items-center gap-1.5" disabled={busy} onClick={() => setChoosing((c) => !c)}>
          <UserCheck size={15} /> Correct Identity
        </button>
        <button className="btn-secondary text-sm flex items-center gap-1.5" disabled={busy} onClick={onUnknown}>
          <HelpCircle size={15} /> Unknown / Not suitable for training
        </button>
        <button
          className="text-sm flex items-center gap-1.5 px-3 py-2 rounded-lg border border-danger-500/40 text-danger-600 hover:bg-danger-50"
          disabled={busy}
          onClick={onDelete}
        >
          <Trash2 size={15} /> Delete Capture
        </button>
      </div>
      {!canApprove && <p className="mt-1 text-xs text-slate-400">“Yes — Correct Name” is disabled because no name was predicted.</p>}

      {choosing && (
        <div className="mt-3 rounded-xl border border-border-200 p-3 space-y-2">
          <p className="text-xs text-slate-500">Who is this?</p>
          {people.length === 0 && <p className="text-xs text-slate-400">No enrolled people loaded.</p>}
          <div className="flex flex-wrap gap-2">
            {people.map((p) => (
              <button
                key={p.employee_id}
                onClick={() => setTarget(p)}
                className={`px-3 py-1.5 rounded-full text-sm border ${
                  target?.employee_id === p.employee_id ? "bg-brand-500 text-white border-brand-500" : "border-border-200 text-ink-900"
                }`}
              >
                {personLabel(p)}
              </button>
            ))}
          </div>
          {target && (
            <div className="flex flex-wrap items-center gap-2 pt-1">
              <span className="text-sm text-ink-900">Label this capture as {personLabel(target)}?</span>
              <button className="btn-primary text-sm" disabled={busy} onClick={() => onCorrect(target)}>
                Confirm
              </button>
              <button className="btn-secondary text-sm" onClick={() => setTarget(null)}>
                Cancel
              </button>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

// ---- Training dataset / gallery section ---------------------------------------
const VERDICT_TEXT = {
  ok: "Safe to apply",
  no_candidates: "No new verified captures to add",
  untestable: "Cannot be tested yet",
  would_degrade: "Would make recognition worse — blocked",
};

function Counts({ r }) {
  if (!r) return <span>—</span>;
  return (
    <span>
      {r.correct} correct · {r.wrong_id} wrong ID · {r.unknown} unknown (of {r.n})
    </span>
  );
}

function PerPerson({ map, people }) {
  const entries = Object.entries(map || {});
  if (!entries.length) return <span className="text-slate-400">none</span>;
  const nameOf = (id) => people.find((p) => String(p.employee_id) === String(id))?.name;
  return <span>{entries.map(([id, n]) => `${nameOf(id) ? `${nameOf(id)} (ID ${id})` : id}: ${n}`).join(", ")}</span>;
}

function DatasetSection({ people, onConfirm, refreshKey }) {
  const [open, setOpen] = useState(false);
  const [preview, setPreview] = useState(null);
  const [plan, setPlan] = useState(null);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);
  const [buildRes, setBuildRes] = useState(null);
  const [updRes, setUpdRes] = useState(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setErr("");
    Promise.all([api.getDatasetPreview(), api.getGalleryPlan()])
      .then(([p, g]) => {
        setPreview(p);
        setPlan(g);
      })
      .catch((e) => setErr(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load, refreshKey]);

  const build = () =>
    onConfirm({
      title: "Build curated dataset?",
      body: "This copies only manually verified images (approved or corrected) into the dataset folder. Nothing is retrained.",
      confirmLabel: "Build dataset",
      run: async () => {
        setBusy(true);
        try {
          const r = await api.buildDataset();
          setBuildRes(r);
          setPreview(r);
        } finally {
          setBusy(false);
        }
      },
    });

  const update = () =>
    onConfirm({
      title: "Update embedding gallery?",
      body: "This changes who is recognised. A backup is written first.",
      confirmLabel: "Update gallery",
      run: async () => {
        setBusy(true);
        try {
          const r = await api.updateGallery();
          setUpdRes(r);
          load();
        } finally {
          setBusy(false);
        }
      },
    });

  return (
    <div className="card">
      <button className="w-full flex items-center justify-between px-5 py-3 text-left" onClick={() => setOpen((o) => !o)}>
        <span className="text-sm font-semibold text-ink-900">Training dataset</span>
        {open ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
      </button>
      {open && (
        <div className="px-5 pb-5 space-y-5 text-sm">
          <p className="text-xs text-slate-500 bg-slate-50 rounded-lg px-3 py-2">
            Nothing retrains automatically. Only captures you approved or corrected are ever used; pending, unknown and deleted
            captures are never included.
          </p>
          {loading && <p className="text-slate-400">Loading…</p>}
          {err && <p className="text-danger-600">{err}</p>}

          {preview && (
            <div className="space-y-1">
              <h4 className="font-medium text-ink-900">Curated dataset (verified only)</h4>
              <p>Verified input: {preview.verified_input} · Kept: {preview.kept} · Excluded: {preview.excluded}</p>
              <p className="text-slate-500">Per person: <PerPerson map={preview.per_person} people={people} /></p>
              {preview.excluded_reasons && Object.keys(preview.excluded_reasons).length > 0 && (
                <p className="text-slate-500">
                  Excluded because: {Object.entries(preview.excluded_reasons).map(([k, v]) => `${k} ${v}`).join(", ")}
                </p>
              )}
              {preview.dataset_dir && <p className="text-xs text-slate-400 break-all">Folder: {preview.dataset_dir}</p>}
              <button className="btn-secondary text-sm mt-1" disabled={busy} onClick={build}>
                Build curated dataset
              </button>
              {buildRes && <p className="text-success-600">Dataset built: {buildRes.kept} images written.</p>}
            </div>
          )}

          {plan && (
            <div className="space-y-1">
              <h4 className="font-medium text-ink-900">Embedding gallery update plan</h4>
              <p>
                Verdict:{" "}
                <span className={`badge ${plan.verdict === "ok" ? "badge-success" : plan.verdict === "would_degrade" ? "badge-danger" : "badge-warning"}`}>
                  {VERDICT_TEXT[plan.verdict] || plan.verdict}
                </span>
              </p>
              {plan.detail && <p className="text-slate-600">{plan.detail}</p>}
              <p>Would add: {plan.would_add ?? 0} · <span className="text-slate-500"><PerPerson map={plan.per_person} people={people} /></span></p>
              <p className="text-slate-500">Held-out images tested: {plan.held_out_tested ?? 0}</p>
              <p className="text-slate-500">Before: <Counts r={plan.before} /></p>
              <p className="text-slate-500">After: <Counts r={plan.after} /></p>
              <button
                className="btn-primary text-sm mt-1 disabled:opacity-50"
                disabled={plan.verdict !== "ok" || busy}
                title={plan.verdict === "ok" ? "" : "Only available when the verdict is OK"}
                onClick={update}
              >
                Update embedding gallery
              </button>
              {updRes && (
                <div className={`text-sm rounded-lg px-3 py-2 ${updRes.applied ? "bg-success-50 text-success-600" : "bg-slate-50 text-slate-600"}`}>
                  {updRes.applied ? "Applied." : "Not applied."}
                  {updRes.reason ? ` ${updRes.reason}` : ""}
                  {updRes.backup ? <span className="block break-all text-xs">Backup: {updRes.backup}</span> : null}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ---- Main -----------------------------------------------------------------------
export default function TrainingCaptures() {
  const [people, setPeople] = useState([]);
  const [status, setStatus] = useState(null);
  const [statusErr, setStatusErr] = useState("");
  const [filters, setFilters] = useState({ camera_id: "", predicted: "", status: "" });
  const [dates, setDates] = useState(null);
  const [datesErr, setDatesErr] = useState("");
  const [day, setDay] = useState("");
  const [items, setItems] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [listErr, setListErr] = useState("");
  const [selected, setSelected] = useState(() => new Set());
  const [detailId, setDetailId] = useState(null);
  const [busy, setBusy] = useState(false);
  const [actErr, setActErr] = useState("");
  const [confirm, setConfirm] = useState(null);
  const [confirmErr, setConfirmErr] = useState("");
  const [bulkTarget, setBulkTarget] = useState("");
  const [bulkResult, setBulkResult] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const reqId = useRef(0);

  useEffect(() => {
    api.getTrainingEmployees().then((r) => setPeople(Array.isArray(r) ? r : r?.employees || [])).catch(() => {});
  }, []);

  const loadStatus = useCallback(() => {
    api.getCaptureStatus().then((s) => { setStatus(s); setStatusErr(""); }).catch((e) => setStatusErr(e.message));
  }, []);
  useEffect(() => {
    loadStatus();
    const h = setVisibleInterval(loadStatus, STATUS_REFRESH_MS);
    return () => clearVisibleInterval(h);
  }, [loadStatus]);

  const loadDates = useCallback(() => {
    api
      .getCaptureDates(filters)
      .then((r) => {
        const list = r.dates || [];
        setDates(list);
        setDatesErr("");
        setDay((cur) => (cur && list.some((d) => d.day === cur) ? cur : list[0]?.day || ""));
      })
      .catch((e) => setDatesErr(e.message));
  }, [filters]);
  useEffect(() => {
    loadDates();
  }, [loadDates]);

  const loadPage = useCallback(
    (pg, append) => {
      if (!day) {
        setItems([]);
        setTotal(0);
        return;
      }
      const id = ++reqId.current;
      setLoading(true);
      setListErr("");
      api
        .getCaptures({ ...filters, date: day, page: pg, page_size: PAGE_SIZE })
        .then((r) => {
          if (id !== reqId.current) return;
          setItems((cur) => (append ? [...cur, ...r.items.filter((n) => !cur.some((c) => c.id === n.id))] : r.items));
          setTotal(r.total);
          setPage(pg);
        })
        .catch((e) => id === reqId.current && setListErr(e.message))
        .finally(() => id === reqId.current && setLoading(false));
    },
    [day, filters]
  );
  useEffect(() => {
    setSelected(new Set());
    setBulkResult(null);
    loadPage(1, false);
  }, [loadPage]);

  const refreshCounters = () => {
    loadStatus();
    loadDates();
    setRefreshKey((k) => k + 1);
  };

  const setFilter = (k, v) => setFilters((f) => ({ ...f, [k]: v }));
  const toggle = (id) =>
    setSelected((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  const detailIndex = items.findIndex((c) => c.id === detailId);
  const detail = detailIndex >= 0 ? items[detailIndex] : null;

  const removeFromGrid = (id) => {
    const next = items[detailIndex + 1] || items[detailIndex - 1];
    setItems((cur) => cur.filter((c) => c.id !== id));
    setTotal((t) => Math.max(0, t - 1));
    setSelected((s) => {
      const n = new Set(s);
      n.delete(id);
      return n;
    });
    setDetailId(next ? next.id : null);
  };

  const runSingle = async (fn) => {
    if (!detail) return;
    const id = detail.id;
    setBusy(true);
    setActErr("");
    try {
      await fn(id);
      const fresh = await api.getCapture(id);
      setItems((cur) => cur.map((c) => (c.id === id ? fresh : c)));
      refreshCounters();
    } catch (e) {
      setActErr(e.message);
    } finally {
      setBusy(false);
    }
  };

  const deleteDetail = async () => {
    if (!detail) return;
    await api.deleteCapture(detail.id);
    removeFromGrid(detail.id);
    refreshCounters();
  };

  const openDetail = (id) => {
    setActErr("");
    setDetailId(id);
  };

  const ask = (cfg) => {
    setConfirmErr("");
    setConfirm(cfg);
  };
  const runConfirm = async () => {
    try {
      await confirm.run();
      setConfirm(null);
    } catch (e) {
      setConfirmErr(e.message);
    }
  };

  const bulk = async (action, extra = {}) => {
    const ids = [...selected];
    const r = await api.bulkCaptureAction({ action, ids, ...extra });
    setBulkResult(r);
    const done = new Set(r.done || []);
    if (action === "delete") {
      setItems((cur) => cur.filter((c) => !done.has(c.id)));
      setTotal((t) => Math.max(0, t - done.size));
    } else {
      const fresh = await Promise.all([...done].map((id) => api.getCapture(id).catch(() => null)));
      const map = new Map(fresh.filter(Boolean).map((c) => [c.id, c]));
      setItems((cur) => cur.map((c) => map.get(c.id) || c));
    }
    setSelected(new Set(ids.filter((id) => !done.has(id))));
    refreshCounters();
  };
  const bulkSimple = async (action) => {
    try {
      await bulk(action);
    } catch (e) {
      setBulkResult({ done: [], failed: [{ id: "—", error: e.message }] });
    }
  };
  const bulkCorrect = () => {
    const p = people.find((x) => String(x.employee_id) === String(bulkTarget));
    if (!p) return;
    ask({
      title: "Correct selected captures?",
      body: `Label ${selected.size} selected capture(s) as ${personLabel(p)}? They become approved training data for this person.`,
      confirmLabel: "Correct labels",
      run: () => bulk("correct", { employeeId: p.employee_id, confirm: true }),
    });
  };
  const bulkDelete = () =>
    ask({
      title: "Delete selected captures?",
      body: `Delete ${selected.size} capture(s)? The image files are removed and can never be used for training. This cannot be undone.`,
      confirmLabel: "Delete",
      danger: true,
      run: () => bulk("delete", { confirm: true }),
    });

  const cameras = useMemo(() => status?.cameras || [], [status]);
  const hasMore = items.length < total;
  const selectAllLoaded = () => setSelected(new Set(items.map((c) => c.id)));

  return (
    <div className="space-y-4">
      <StatusStrip status={status} error={statusErr} onChanged={loadStatus} />

      <div className="flex flex-wrap items-center gap-3">
        <select className="input-field w-auto" value={filters.camera_id} onChange={(e) => setFilter("camera_id", e.target.value)}>
          <option value="">All cameras</option>
          {cameras.map((c) => (
            <option key={c.camera_id} value={c.camera_id}>{c.name || c.camera_id}</option>
          ))}
        </select>
        <select className="input-field w-auto" value={filters.predicted} onChange={(e) => setFilter("predicted", e.target.value)}>
          <option value="">Any predicted identity</option>
          {people.map((p) => (
            <option key={p.employee_id} value={p.employee_id}>{personLabel(p)}</option>
          ))}
          <option value="unknown">Unknown</option>
        </select>
        <select className="input-field w-auto" value={filters.status} onChange={(e) => setFilter("status", e.target.value)}>
          <option value="">All review statuses</option>
          {Object.entries(STATUS_LABEL).map(([k, v]) => (
            <option key={k} value={k}>{v}</option>
          ))}
        </select>
      </div>

      <div className="flex flex-col md:flex-row gap-4">
        <div className="md:w-52 shrink-0">
          <div className="card p-2 flex md:flex-col gap-1 overflow-x-auto md:overflow-y-auto md:max-h-[560px]">
            {dates === null && !datesErr && <p className="text-xs text-slate-400 p-2">Loading dates…</p>}
            {datesErr && <p className="text-xs text-danger-600 p-2">{datesErr}</p>}
            {dates && dates.length === 0 && <p className="text-xs text-slate-400 p-2">No captures match.</p>}
            {(dates || []).map((d) => (
              <button
                key={d.day}
                onClick={() => setDay(d.day)}
                className={`shrink-0 text-left rounded-lg px-3 py-2 text-sm ${
                  day === d.day ? "bg-brand-500 text-white" : "hover:bg-slate-50 text-ink-900"
                }`}
              >
                <span className="block font-medium">{d.day}</span>
                <span className={`block text-[11px] ${day === d.day ? "text-white/80" : "text-slate-500"}`}>
                  {d.total} captures{d.pending ? ` · ${d.pending} pending` : ""}
                </span>
              </button>
            ))}
          </div>
        </div>

        <div className="flex-1 min-w-0 space-y-3">
          {selected.size > 0 && (
            <div className="sticky top-2 z-20 card p-3 flex flex-wrap items-center gap-2 shadow-md">
              <span className="text-sm font-medium text-ink-900">{selected.size} selected</span>
              <button className="btn-secondary text-xs" onClick={() => bulkSimple("approve")}>Approve predicted names</button>
              <select className="input-field w-auto text-xs" value={bulkTarget} onChange={(e) => setBulkTarget(e.target.value)}>
                <option value="">Correct to…</option>
                {people.map((p) => (
                  <option key={p.employee_id} value={p.employee_id}>{personLabel(p)}</option>
                ))}
              </select>
              <button className="btn-secondary text-xs disabled:opacity-50" disabled={!bulkTarget} onClick={bulkCorrect}>Correct</button>
              <button className="btn-secondary text-xs" onClick={() => bulkSimple("unknown")}>Mark unknown</button>
              <button className="text-xs px-3 py-2 rounded-lg border border-danger-500/40 text-danger-600 hover:bg-danger-50" onClick={bulkDelete}>
                Delete
              </button>
              <span className="flex-1" />
              <button className="text-xs text-slate-500 flex items-center gap-1" onClick={selectAllLoaded}>
                <CheckSquare size={13} /> Select all on page
              </button>
              <button className="text-xs text-slate-500 flex items-center gap-1" onClick={() => setSelected(new Set())}>
                <X size={13} /> Clear
              </button>
            </div>
          )}

          {bulkResult && (
            <div className="card p-3 text-sm">
              <div className="flex justify-between">
                <span>{(bulkResult.done || []).length} done, {(bulkResult.failed || []).length} failed</span>
                <button onClick={() => setBulkResult(null)} aria-label="Dismiss"><X size={14} /></button>
              </div>
              {(bulkResult.failed || []).length > 0 && (
                <ul className="mt-1 text-xs text-danger-600 space-y-0.5">
                  {bulkResult.failed.map((f, i) => (
                    <li key={i}>#{f.id}: {f.error}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {listErr && (
            <div className="text-sm text-danger-600 bg-danger-50 rounded-lg px-3.5 py-2 flex items-center gap-2">
              <AlertTriangle size={15} /> {listErr}
            </div>
          )}

          {items.length > 0 && (
            <div className="flex items-center justify-between text-xs text-slate-500">
              <span>{day}: showing {items.length} of {total}</span>
              {selected.size === 0 && <button className="underline" onClick={selectAllLoaded}>Select all on page</button>}
            </div>
          )}

          {!loading && !listErr && items.length === 0 && (
            <div className="card p-10 text-center text-sm text-slate-400">
              {day ? "No captures for this date and filters." : "No captures yet."}
            </div>
          )}

          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3">
            {items.map((c) => (
              <Tile key={c.id} cap={c} selected={selected.has(c.id)} onToggle={toggle} onOpen={openDetail} />
            ))}
          </div>

          {loading && <p className="text-sm text-slate-400">Loading captures…</p>}
          {hasMore && !loading && (
            <div className="text-center">
              <button className="btn-secondary text-sm" onClick={() => loadPage(page + 1, true)}>
                Load more ({total - items.length} remaining)
              </button>
            </div>
          )}
        </div>
      </div>

      <DatasetSection people={people} onConfirm={ask} refreshKey={refreshKey} />

      {detail && (
        <CaptureDetail
          cap={detail}
          index={detailIndex}
          count={items.length}
          people={people}
          busy={busy}
          error={actErr}
          onClose={() => setDetailId(null)}
          onPrev={() => items[detailIndex - 1] && openDetail(items[detailIndex - 1].id)}
          onNext={() => items[detailIndex + 1] && openDetail(items[detailIndex + 1].id)}
          onApprove={() => runSingle((id) => api.approveCapture(id))}
          onCorrect={(p) => runSingle((id) => api.correctCapture(id, p.employee_id))}
          onUnknown={() => runSingle((id) => api.markCaptureUnknown(id))}
          onDelete={() =>
            ask({
              title: "Delete this capture?",
              body: "The image file is removed and this capture can never be used for training. This cannot be undone.",
              confirmLabel: "Delete capture",
              danger: true,
              run: deleteDetail,
            })
          }
        />
      )}

      <Modal open={!!confirm} onClose={() => setConfirm(null)} title={confirm?.title}>
        <p className="text-sm text-slate-600">{confirm?.body}</p>
        {confirmErr && <p className="mt-3 text-sm text-danger-600 bg-danger-50 rounded-lg px-3 py-2">{confirmErr}</p>}
        <div className="mt-5 flex justify-end gap-2">
          <button className="btn-secondary text-sm" onClick={() => setConfirm(null)}>Cancel</button>
          <button className={`text-sm px-4 py-2 rounded-lg text-white ${confirm?.danger ? "bg-danger-600" : "bg-brand-500"}`} onClick={runConfirm}>
            {confirm?.confirmLabel || "Confirm"}
          </button>
        </div>
      </Modal>
    </div>
  );
}
