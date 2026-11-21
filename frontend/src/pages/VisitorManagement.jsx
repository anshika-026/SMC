import { useCallback, useEffect, useRef, useState } from "react";
import { Bell, Camera, ShieldCheck, UserPlus, X } from "lucide-react";
import PageHeader from "../components/PageHeader";
import SidePanel from "../components/SidePanel";
import VisitorAlerts, { fmtTime } from "../components/VisitorAlerts";
import VisitorEventDetail from "../components/VisitorEventDetail";
import VisitorHistory from "../components/VisitorHistory";
import PeopleInVisitorArea from "../components/PeopleInVisitorArea";
import VisitorEmployees from "../components/VisitorEmployees";
import Tabs from "../components/Tabs";
import RegisterVisitorModal from "../components/visitor/RegisterVisitorModal";
import VisitorKpiCards from "../components/visitor/VisitorKpiCards";
import VisitorRecordsTable, { apiFilters } from "../components/visitor/VisitorRecordsTable";
import VisitPanel from "../components/visitor/VisitPanel";
import VisitorProfile from "../components/visitor/VisitorProfile";
import VisitorAnalytics from "../components/visitor/VisitorAnalytics";
import CameraDetections from "../components/visitor/CameraDetections";
import VisitorDirectory from "../components/visitor/VisitorDirectory";
import PrivacySettings from "../components/visitor/PrivacySettings";
import FootfallView from "../components/visitor/FootfallView";
import ZonesView from "../components/zones/ZonesView";
import { shiftIso, todayIso } from "../components/workforce/shared";
import * as api from "../api/client";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";

// Visitor Management: three KPI cards (Visitor Activity and Visitor Trends from the
// visitor area cameras, Footfall Analytics; each opens its detailed view) over the sections:
//   Visitor records   every visit: who, why, whom they met, in / out, waiting,
//                     duration (visitor_registry.py). Camera arrivals the
//                     operator can link to a visit open from here.
//   Visitor profiles  one profile per visitor with their visit history
//   Analytics         visitor reports over any date range
//   Footfall          people entering the gates: reports, cameras, today
//                     (footfall.py; it no longer has its own sidebar entry)
//   Zones & areas     every area marked on a camera, by purpose (zones.py);
//                     the visitor seating-area alerts (visitors.py) live under
//                     their zone, and their pop-ups still show on every tab.

const EVENTS_MS = 4_000;
const RECORDS_MS = 20_000;
const TOAST_MS = 8_000;
const HIGHLIGHT_MS = 8_000;
// "People in visitor area": everyone who sat in a visitor area, visitors (with their clip) and
// employees (with face snapshots) in one list, filterable to either. It replaced the
// employees-only "Employees in visitor area" tab.
const PEOPLE_TAB = "People in visitor area";
const TABS = ["Visitor records", PEOPLE_TAB, "Visitor profiles", "Analytics", "Footfall", "Zones & areas"];
const TAB_PARAM = { records: "Visitor records", people: PEOPLE_TAB, employees: PEOPLE_TAB, profiles: "Visitor profiles", analytics: "Analytics", footfall: "Footfall", zones: "Zones & areas" };
const NO_FILTERS = { status: "", type: "", host: "", department: "", q: "" };

// KPI card presets -> the records table's date range.
function presetRange(preset) {
  const today = todayIso();
  if (preset === "today") return { from: today, to: today };
  if (preset === "month") return { from: `${today.slice(0, 7)}-01`, to: today };
  if (preset === "all") return { from: "", to: "" };
  return { from: shiftIso(today, -6), to: today };
}

function initialTab() {
  // ?tab=footfall etc. (old /footfall links land here), else the remembered tab.
  const q = new URLSearchParams(window.location.search).get("tab");
  if (q && TAB_PARAM[q]) return TAB_PARAM[q];
  try {
    const t = window.localStorage.getItem("visitorTab");
    if (t === "Employees in visitor area") return PEOPLE_TAB; // the tab's old name
    return TABS.includes(t) ? t : TABS[0];
  } catch {
    return TABS[0];
  }
}

export default function VisitorManagement() {
  const [vcams, setVcams] = useState([]);
  const [events, setEvents] = useState(null);
  const [evError, setEvError] = useState("");
  const [filter, setFilter] = useState(0);
  const [openEvent, setOpenEvent] = useState(null); // visitor-area event id
  const [toasts, setToasts] = useState([]);
  const [highlight, setHighlight] = useState(() => new Set());
  const [tab, setTab] = useState(initialTab);

  // Visitor records
  const [vrSettings, setVrSettings] = useState(null);
  const [filters, setFilters] = useState(() => ({ ...presetRange("week"), ...NO_FILTERS }));
  const [records, setRecords] = useState(null);
  const [recordsError, setRecordsError] = useState("");
  const [todayVisits, setTodayVisits] = useState([]);
  const [registering, setRegistering] = useState(false);
  const [privacyOpen, setPrivacyOpen] = useState(false);
  const [arrivalsOpen, setArrivalsOpen] = useState(false);
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
    if (window.location.search) window.history.replaceState(null, "", window.location.pathname);
  }

  const loadRecords = useCallback(() => {
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
    api.getVisitorCameras().then((d) => setVcams(d.cameras)).catch(() => {});
  }, []);

  const changed = useCallback(() => setRefreshKey((k) => k + 1), []);

  // A KPI card opens "People in visitor area", showing visitors or employees.
  const [peopleShow, setPeopleShow] = useState({ show: "all", n: 0 });
  function openPeople(show) {
    setPeopleShow((x) => ({ show, n: x.n + 1 }));
    pickTab(PEOPLE_TAB);
  }

  // Visitor seating-area alerts: polled on every tab so their pop-ups always show.
  const maxSeen = useRef(null);
  const timers = useRef(new Set());
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

  const initialEvent = events?.find((e) => e.id === openEvent) || null;
  const unlinkedArrivals = todayVisits.filter((v) => v.status === "pending").length;

  // A visitor seating zone's activity (Zones & areas -> Activity).
  const visitorActivity = (
    <div className="space-y-5">
      <VisitorAlerts
        events={events}
        cameras={vcams}
        filter={filter}
        onFilter={setFilter}
        loading={!events && !evError}
        error={evError}
        highlightIds={highlight}
        onOpen={setOpenEvent}
      />
      <VisitorHistory cameras={vcams} onOpenEvent={setOpenEvent} />
      <VisitorEmployees />
    </div>
  );

  return (
    <div>
      {/* Notification banner for new visitor-area alerts */}
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

      <VisitorKpiCards onOpen={openPeople} onFootfall={() => pickTab("Footfall")} refreshKey={refreshKey} />

      <div className="overflow-x-auto">
        <Tabs tabs={TABS} active={tab} onChange={pickTab} />
      </div>

      {tab === "Visitor records" && (
        <div id="visitor-records" className="scroll-mt-4 space-y-3">
          <div className="flex justify-end">
            <button type="button" className="btn-secondary text-xs inline-flex items-center gap-1.5" onClick={() => setArrivalsOpen(true)}>
              <Camera size={13} /> Camera arrivals{unlinkedArrivals ? ` · ${unlinkedArrivals} waiting for check-in` : ""}
            </button>
          </div>
          <VisitorRecordsTable
            filters={filters}
            setFilters={setFilters}
            rows={records}
            error={recordsError}
            onOpenVisit={setVisitId}
            onOpenVisitor={setVisitorId}
          />
        </div>
      )}
      {tab === PEOPLE_TAB && <PeopleInVisitorArea key={peopleShow.n} initialShow={peopleShow.show} cameras={vcams} onOpenEvent={setOpenEvent} />}
      {tab === "Visitor profiles" && <VisitorDirectory onOpenVisitor={setVisitorId} refreshKey={refreshKey} />}
      {tab === "Analytics" && <VisitorAnalytics onOpenVisitor={setVisitorId} />}
      {tab === "Footfall" && <FootfallView />}
      {tab === "Zones & areas" && <ZonesView visitorActivity={visitorActivity} />}

      <SidePanel open={arrivalsOpen} onClose={() => setArrivalsOpen(false)} width="max-w-4xl" title="Camera arrivals">
        {arrivalsOpen && <CameraDetections visits={todayVisits} onOpenVisit={(id) => { setArrivalsOpen(false); setVisitId(id); }} onChanged={changed} />}
      </SidePanel>

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
          }}
        />
      )}
    </div>
  );
}
