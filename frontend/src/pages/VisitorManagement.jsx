import { useCallback, useEffect, useRef, useState } from "react";
import { Bell, X, Info, ShieldCheck, UserPlus } from "lucide-react";
import PageHeader from "../components/PageHeader";
import { liveCameraFor } from "../components/VisitorCameraGrid";
import VisitorZoneEditor from "../components/VisitorZoneEditor";
import VisitorAlerts, { fmtTime } from "../components/VisitorAlerts";
import VisitorEventDetail from "../components/VisitorEventDetail";
import VisitorHistory from "../components/VisitorHistory";
import VisitorEmployees from "../components/VisitorEmployees";
import Tabs from "../components/Tabs";
import KpiPanel from "../components/KpiPanel";
import { fmtDuration } from "../components/VisitorHistory";
import RegisterVisitorModal from "../components/visitor/RegisterVisitorModal";
import VisitorKpiCards from "../components/visitor/VisitorKpiCards";
import VisitorRecordsTable, { apiFilters } from "../components/visitor/VisitorRecordsTable";
import VisitPanel from "../components/visitor/VisitPanel";
import VisitorProfile from "../components/visitor/VisitorProfile";
import VisitorAnalytics from "../components/visitor/VisitorAnalytics";
import CameraDetections from "../components/visitor/CameraDetections";
import VisitorDirectory from "../components/visitor/VisitorDirectory";
import PrivacySettings from "../components/visitor/PrivacySettings";
import { shiftIso, todayIso } from "../components/workforce/shared";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";

// Visitor management.
//   Visitor records (landing): who visited, why, whom they met, entry/exit,
//     waiting time and visit duration, from /api/visitor-records (visitor_registry.py).
//   Camera detections / Analytics / Visitor directory: the same records.
//   Area monitoring: the seating-area alerts with 10 s clips (/api/visitors,
//     visitors.py), unchanged; their pop-up alerts show on every tab.

const CAMERAS_MS = 10_000;
const EVENTS_MS = 4_000;
const RECORDS_MS = 20_000;
const TOAST_MS = 8_000;
const HIGHLIGHT_MS = 8_000;
const TABS = ["Visitor records", "Camera detections", "Analytics", "Visitor directory", "Area monitoring"];
const AREA_TABS = ["Monitoring", "Area history", "Employees"];
const NO_FILTERS = { status: "", type: "", host: "", department: "", q: "" };

// KPI card presets -> the records table's date range.
function presetRange(preset) {
  const today = todayIso();
  if (preset === "today") return { from: today, to: today };
  if (preset === "month") return { from: `${today.slice(0, 7)}-01`, to: today };
  if (preset === "all") return { from: "", to: "" };
  return { from: shiftIso(today, -6), to: today };
}

export default function VisitorManagement() {
  const [vcams, setVcams] = useState(null);
  const [liveCams, setLiveCams] = useState([]);
  const [camError, setCamError] = useState("");
  const [events, setEvents] = useState(null);
  const [evError, setEvError] = useState("");
  const [filter, setFilter] = useState(0);
  const [selectedCam, setSelectedCam] = useState(() => {
    try {
      const v = Number(window.localStorage.getItem("visitorCamera"));
      return Number.isFinite(v) && v > 0 ? v : null;
    } catch {
      return null;
    }
  });
  const [openEvent, setOpenEvent] = useState(null); // event id
  const [toasts, setToasts] = useState([]);
  const [highlight, setHighlight] = useState(() => new Set());
  const [settings, setSettings] = useState(null);
  const [kpis, setKpis] = useState(null);
  const [areaTab, setAreaTab] = useState(AREA_TABS[0]);
  const [tab, setTab] = useState(() => {
    try {
      const t = window.localStorage.getItem("visitorTab");
      return TABS.includes(t) ? t : TABS[0];
    } catch {
      return TABS[0];
    }
  });

  // Visitor records
  const [vrSettings, setVrSettings] = useState(null);
  const [vrKpis, setVrKpis] = useState(null);
  const [filters, setFilters] = useState(() => ({ ...presetRange("week"), ...NO_FILTERS }));
  const [records, setRecords] = useState(null);
  const [recordsError, setRecordsError] = useState("");
  const [todayVisits, setTodayVisits] = useState([]);
  const [registering, setRegistering] = useState(false);
  const [privacyOpen, setPrivacyOpen] = useState(false);
  const [visitId, setVisitId] = useState(null);
  const [visitorId, setVisitorId] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);

  function pickTab(t) {
    setTab(t);
    try {
      window.localStorage.setItem("visitorTab", t);
    } catch {
      /* remembering the tab is a convenience only */
    }
  }

  const loadRecords = useCallback(() => {
    api.vr.kpis().then(setVrKpis).catch(() => {});
    api.vr
      .visits({ ...apiFilters(filters), limit: 1000 })
      .then((d) => {
        setRecords(d.visits);
        setRecordsError("");
      })
      .catch((e) => setRecordsError(e.message));
    const today = todayIso();
    api.vr.visits({ from: today, to: today, limit: 1000 }).then((d) => setTodayVisits(d.visits)).catch(() => {});
  }, [filters]);

  useEffect(() => {
    loadRecords();
    const h = setVisibleInterval(loadRecords, RECORDS_MS);
    return () => clearVisibleInterval(h);
  }, [loadRecords, refreshKey]);

  useEffect(() => {
    api.vr.settings().then(setVrSettings).catch(() => {});
  }, []);

  const changed = useCallback(() => setRefreshKey((k) => k + 1), []);

  function applyKpiFilter({ preset, ...rest }) {
    setFilters({ ...presetRange(preset), ...NO_FILTERS, ...rest });
    document.getElementById("visitor-records")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  const maxSeen = useRef(null); // highest event id at first load / since
  const timers = useRef(new Set());

  const loadCameras = useCallback(() => {
    api.getVisitorKpis().then(setKpis).catch(() => {});
    return api
      .getVisitorCameras()
      .then((d) => {
        setVcams(d.cameras);
        setCamError("");
      })
      .catch((e) => setCamError(e.message));
  }, []);

  useEffect(() => {
    loadCameras();
    api.getCameras().then(setLiveCams).catch(() => {});
    api.getVisitorSettings().then(setSettings).catch(() => {});
    const h = setVisibleInterval(loadCameras, CAMERAS_MS);
    return () => clearVisibleInterval(h);
  }, [loadCameras]);

  const later = useCallback((fn, ms) => {
    const t = setTimeout(() => {
      timers.current.delete(t);
      fn();
    }, ms);
    timers.current.add(t);
  }, []);

  useEffect(() => {
    const pending = timers.current;
    return () => pending.forEach(clearTimeout);
  }, []);

  const loadEvents = useCallback(() => {
    return api
      .getVisitorEvents({ limit: 50 })
      .then((d) => {
        const list = d.events || [];
        setEvents(list);
        setEvError("");
        const top = list.reduce((m, e) => Math.max(m, e.id), 0);
        if (maxSeen.current === null) {
          maxSeen.current = top;
          return;
        }
        const fresh = list.filter((e) => e.id > maxSeen.current);
        if (fresh.length) {
          maxSeen.current = top;
          setToasts((t) => [
            ...t,
            ...fresh.map((e) => ({
              key: e.id,
              text:
                e.alert_seq > 1
                  ? `Visitor still seated on ${e.camera_name} at ${fmtTime(e.alert_at)} (since ${fmtTime(e.entered_at)}, event #${e.id})`
                  : `Visitor detected on ${e.camera_name} at ${fmtTime(e.alert_at || e.entered_at)} (event #${e.id})`,
              id: e.id,
            })),
          ]);
          setHighlight((s) => new Set([...s, ...fresh.map((e) => e.id)]));
          fresh.forEach((e) => {
            later(() => setToasts((t) => t.filter((x) => x.key !== e.id)), TOAST_MS);
            later(
              () =>
                setHighlight((s) => {
                  const n = new Set(s);
                  n.delete(e.id);
                  return n;
                }),
              HIGHLIGHT_MS
            );
          });
        }
      })
      .catch((e) => setEvError(e.message));
  }, [later]);

  useEffect(() => {
    loadEvents();
    const h = setVisibleInterval(loadEvents, EVENTS_MS);
    return () => clearVisibleInterval(h);
  }, [loadEvents]);

  // The camera picked in the dropdown; falls back to the first camera when nothing
  // (or a camera that no longer exists) was remembered.
  const activeVc = vcams ? vcams.find((c) => c.camera_id === selectedCam) || vcams[0] || null : null;

  function pickCamera(id) {
    setSelectedCam(id);
    try {
      window.localStorage.setItem("visitorCamera", String(id));
    } catch {
      /* remembering the choice is a convenience only */
    }
  }
  const initialEvent = events?.find((e) => e.id === openEvent) || null;

  return (
    <div>
      {/* Notification banner for new visitor-area events */}
      {toasts.length > 0 && (
        <div className="fixed top-4 left-1/2 -translate-x-1/2 z-[60] space-y-2 w-[min(32rem,calc(100vw-2rem))]">
          {toasts.map((t) => (
            <div key={t.key} className="flex items-start gap-2 bg-ink-900 text-white rounded-xl shadow-lg px-4 py-3 text-sm">
              <Bell size={16} className="mt-0.5 shrink-0 text-warning-500" />
              <button type="button" className="flex-1 text-left" onClick={() => setOpenEvent(t.id)}>
                {t.text}
              </button>
              <button type="button" className="text-white/60 hover:text-white" onClick={() => setToasts((l) => l.filter((x) => x.key !== t.key))}>
                <X size={14} />
              </button>
            </div>
          ))}
        </div>
      )}

      <PageHeader
        title="Visitor Management"
        action={
          <div className="flex gap-2">
            <button type="button" className="btn-secondary text-sm inline-flex items-center gap-1.5" onClick={() => setPrivacyOpen(true)}>
              <ShieldCheck size={15} /> <span className="hidden sm:inline">Privacy & retention</span>
            </button>
            <button type="button" className="btn-primary text-sm inline-flex items-center gap-1.5" onClick={() => setRegistering(true)}>
              <UserPlus size={15} /> Register visitor
            </button>
          </div>
        }
      />

      <div className="overflow-x-auto">
        <Tabs tabs={TABS} active={tab} onChange={pickTab} />
      </div>

      {tab === "Visitor records" && (
        <>
          <VisitorKpiCards k={vrKpis} onFilter={applyKpiFilter} onOpenVisitor={setVisitorId} />
          <div id="visitor-records" className="scroll-mt-4">
            <VisitorRecordsTable
              filters={filters}
              setFilters={setFilters}
              rows={records}
              error={recordsError}
              onOpenVisit={setVisitId}
              onOpenVisitor={setVisitorId}
            />
          </div>
        </>
      )}
      {tab === "Camera detections" && <CameraDetections visits={todayVisits} onOpenVisit={setVisitId} onChanged={changed} />}
      {tab === "Analytics" && <VisitorAnalytics onOpenVisitor={setVisitorId} />}
      {tab === "Visitor directory" && <VisitorDirectory onOpenVisitor={setVisitorId} refreshKey={refreshKey} />}

      {tab === "Area monitoring" && (
        <>
          <KpiPanel
            icon={Info}
            title="Visitor seating area (today)"
            items={[
              { label: "People alerted today", value: kpis ? String(kpis.visitors_today) : null, sub: kpis ? `${kpis.employees_seen_today} employees seen, not counted` : null },
              { label: "Average time in the area", value: kpis ? (kpis.visitors_today ? fmtDuration(kpis.average_stay_seconds) : "—") : null, sub: "unknown people in the seating area" },
              {
                label: `In the area over ${kpis ? kpis.long_stay_minutes : 30} min`,
                value: kpis ? String(kpis.long_stays) : null,
                sub: "stays today",
                tone: "warn",
              },
              { label: "In the area now", value: kpis ? String(kpis.waiting_now) : null, sub: "unknown people" },
            ]}
          />

          <Tabs tabs={AREA_TABS} active={areaTab} onChange={setAreaTab} />

          {areaTab === "Area history" && <VisitorHistory cameras={vcams || []} onOpenEvent={setOpenEvent} />}
          {areaTab === "Employees" && <VisitorEmployees />}

          {areaTab === "Monitoring" && settings?.rules && (
            <div className="card p-4 mb-4 flex gap-3 text-xs text-slate-500">
              <Info size={16} className="shrink-0 text-brand-600 mt-0.5" />
              <div>
                <p className="font-medium text-ink-900 mb-1">How it works</p>
                <ul className="space-y-0.5">
                  {["new_entry", "reentry", "retention"]
                    .filter((k) => settings.rules[k])
                    .map((k) => (
                      <li key={k}>{settings.rules[k]}</li>
                    ))}
                </ul>
              </div>
            </div>
          )}

          {areaTab === "Monitoring" && (
            <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 items-start">
              <div className="xl:col-span-2">
                {camError && <p className="text-sm text-danger-600 mb-3">Could not load cameras: {camError}</p>}
                {!vcams && !camError && <p className="text-sm text-slate-400 py-10 text-center">Loading cameras…</p>}
                {vcams && vcams.length === 0 && <p className="text-sm text-slate-400 py-10 text-center">No cameras are configured.</p>}
                {vcams && vcams.length > 0 && activeVc && (
                  <div className="space-y-4">
                    <div className="card p-4">
                      <label htmlFor="visitor-camera" className="text-sm font-medium text-ink-900 block mb-1.5">
                        Camera
                      </label>
                      <select
                        id="visitor-camera"
                        value={activeVc.camera_id}
                        onChange={(e) => pickCamera(Number(e.target.value))}
                        className="w-full md:w-96 px-3 py-2 rounded-lg border border-border-200 text-sm bg-white"
                      >
                        {vcams.map((c) => (
                          <option key={c.camera_id} value={c.camera_id}>
                            {c.name}
                            {c.site ? ` (${c.site})` : ""} - {c.online ? "online" : "offline"} - {c.zone ? (c.detection_active ? "area set, detection on" : "area set, detection off") : "no area yet"}
                          </option>
                        ))}
                      </select>
                      <p className="text-xs text-slate-500 mt-2">
                        Pick a camera, then click on its live picture to mark the area. Only people entering that area create a visitor event.
                      </p>
                    </div>
                    <VisitorZoneEditor
                      key={activeVc.camera_id}
                      inline
                      vc={activeVc}
                      camera={liveCameraFor(activeVc, liveCams)}
                      onChanged={loadCameras}
                    />
                  </div>
                )}
              </div>

              <VisitorAlerts
                events={events}
                cameras={vcams || []}
                filter={filter}
                onFilter={setFilter}
                loading={!events && !evError}
                error={evError}
                highlightIds={highlight}
                onOpen={setOpenEvent}
              />
            </div>
          )}
        </>
      )}

      <RegisterVisitorModal
        open={registering}
        settings={vrSettings}
        onClose={() => setRegistering(false)}
        onRegistered={(v) => {
          setRegistering(false);
          changed();
          setVisitId(v.id);
        }}
      />
      <PrivacySettings open={privacyOpen} settings={vrSettings} onClose={() => setPrivacyOpen(false)} onSaved={setVrSettings} />
      {visitId !== null && (
        <VisitPanel
          visitId={visitId}
          onClose={() => setVisitId(null)}
          onChanged={changed}
          onOpenProfile={(id) => {
            setVisitId(null);
            setVisitorId(id);
          }}
        />
      )}
      {visitorId !== null && (
        <VisitorProfile
          visitorId={visitorId}
          onClose={() => setVisitorId(null)}
          onChanged={changed}
          onOpenVisit={(id) => {
            setVisitorId(null);
            setVisitId(id);
          }}
        />
      )}

      {openEvent !== null && (
        <VisitorEventDetail
          key={openEvent}
          eventId={openEvent}
          initial={initialEvent}
          onClose={() => setOpenEvent(null)}
          onDeleted={() => {
            setOpenEvent(null);
            loadEvents();
            loadCameras();
          }}
        />
      )}
    </div>
  );
}
