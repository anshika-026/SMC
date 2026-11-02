import { useEffect, useState } from "react";
import { NavLink } from "react-router-dom";
import {
  Gauge,
  MonitorPlay,
  Siren,
  ShieldAlert,
  Fingerprint,
  UserCheck,
  ScanFace,
  Contact,
  TrendingUp,
  Cctv,
  MapPinned,
  CalendarCheck,
} from "lucide-react";
import { useAuth } from "../context/AuthContext";
import * as api from "../api/client";
import Avatar from "../components/Avatar";
import BrandLogo from "../components/BrandLogo";
import { setVisibleInterval, clearVisibleInterval } from "../lib/visibleInterval";
import { featureOn, licenseOn } from "../lib/features";

// `requiresFeature` gates a nav item for a client login (user.role ===
// "client", see AuthContext.loginAsClient) to only what their license has
// enabled (backend/app/license_db.py's ALL_FEATURES) — an admin login
// always sees everything, feature keys are ignored for them. Live camera,
// Alerts and Dashboard have no feature of their own (base capability),
// so they stay visible to any logged-in client regardless of features.
const NAV_SECTIONS = [
  {
    items: [{ to: "/dashboard", label: "Dashboard", icon: Gauge }],
  },
  {
    title: "Monitoring",
    items: [
      { to: "/live-feed", label: "Live Feed", icon: MonitorPlay },
      { to: "/alerts", label: "Alerts & Events", icon: Siren, badgeKey: "alerts", analytics: "alerts" },
    ],
  },
  {
    title: "People",
    items: [
      { to: "/people", label: "Identity", icon: Fingerprint, requiresFeature: "face_recognition", analytics: "face_recognition" },
    ],
  },
  {
    title: "Analytics",
    items: [
      { to: "/workforce", label: "Workforce Insights", icon: TrendingUp, requiresFeature: "workforce_analytics", analytics: "desk_analytics" },
      { to: "/face-recognition", label: "Face Recognition", icon: ScanFace, requiresFeature: "attendance", analytics: "face_recognition" },
      // Attendance is built from face recognition sightings (backend/app/attendance.py).
      { to: "/attendance", label: "Attendance", icon: CalendarCheck, requiresFeature: "attendance", analytics: "face_recognition", license: "attendance" },
      { to: "/visitors", label: "Visitor Management", icon: Contact, requiresFeature: "footfall_analytics", analytics: "visitors" },
      // Intrusion Detection: alerts, their videos and historical insights on one page.
      { to: "/intrusion", label: "Intrusion", icon: ShieldAlert, analytics: "intrusion", license: "intrusion_detection" },
    ],
  },
  {
    title: "Management",
    adminOnly: true, // managing other companies' cameras/sites/licenses isn't part of a client's own portal
    items: [
      { to: "/cameras", label: "Camera", icon: Cctv },
      { to: "/sites", label: "Site", icon: MapPinned },
    ],
  },
];

export default function Sidebar() {
  const { user } = useAuth();
  const isClient = user?.role === "client";
  // Live badge counts (currently: active alerts), refreshed every 30 s.
  const [counts, setCounts] = useState({});
  useEffect(() => {
    if (isClient) return;
    const load = () => api.getAlertsSummary().then((s) => setCounts({ alerts: s.active })).catch(() => {});
    load();
    const t = setVisibleInterval(load, 30000);
    return () => clearVisibleInterval(t);
  }, [isClient]);

  const sections = NAV_SECTIONS.filter((s) => !(isClient && s.adminOnly))
    .map((s) => ({
      ...s,
      items: s.items.filter(
        (item) =>
          featureOn(item.analytics) &&
          licenseOn(item.license) &&
          !(isClient && item.adminOnly) &&
          (!isClient || !item.requiresFeature || (user.allowedFeatures || []).includes(item.requiresFeature))
      ),
    }))
    .filter((s) => s.items.length > 0);

  // Same visual language as the pages: a floating white card (rounded, soft
  // shadow) on the page background, icons in soft tiles, an indigo-to-violet
  // pill for the current page.
  return (
    <aside className="hidden md:flex md:w-64 shrink-0 h-screen sticky top-0 p-3 pr-0">
      <div className="relative isolate flex flex-col w-full rounded-2xl bg-white/90 backdrop-blur-xl shadow-[0_1px_3px_rgba(15,23,42,0.06),0_8px_30px_rgba(79,70,229,0.07)] ring-1 ring-white/70 overflow-hidden">
        <div className="flex items-center gap-2.5 px-5 h-16">
          <BrandLogo size={30} />
          <div className="leading-tight">
            <span className="block text-ink-900 font-semibold tracking-tight">Deco Vision</span>
            <span className="block text-[10px] uppercase tracking-[0.14em] text-slate-400">Smart monitoring</span>
          </div>
        </div>

        <nav className="flex-1 overflow-y-auto px-3 pb-3 space-y-3 [scrollbar-width:thin]">
          {sections.map((section, i) => (
            <div key={i}>
              {section.title && (
                <p className="px-3 mb-1 text-[10px] font-semibold uppercase tracking-[0.14em] text-slate-400">{section.title}</p>
              )}
              <div className="space-y-0.5">
                {section.items.map(({ to, label, icon: Icon, badgeKey }) => {
                  const badge = badgeKey ? counts[badgeKey] : null;
                  return (
                    <NavLink
                      key={to}
                      to={to}
                      className={({ isActive: hit }) =>
                        `group flex items-center gap-2.5 pl-1.5 pr-2.5 py-1 rounded-xl text-[13px] transition-all ${
                          hit
                            ? "bg-gradient-to-r from-indigo-500 to-violet-500 text-white shadow-[0_6px_16px_-6px_rgba(99,102,241,0.65)]"
                            : "text-slate-600 hover:bg-indigo-50/60 hover:text-ink-900"
                        }`
                      }
                    >
                      {({ isActive: hit }) => {
                        const isActive = hit;
                        return (
                          <>
                            <span
                              className={`w-7 h-7 rounded-lg grid place-items-center shrink-0 transition-colors ${
                                isActive ? "bg-white/20 text-white" : "bg-slate-50 text-slate-500 group-hover:bg-white group-hover:text-indigo-600"
                              }`}
                            >
                              <Icon size={15} strokeWidth={1.9} />
                            </span>
                            <span className="flex-1 leading-tight">{label}</span>
                            {badge ? (
                              <span
                                className={`text-[10px] font-semibold rounded-full px-1.5 py-0.5 leading-none ${
                                  isActive ? "bg-white text-indigo-600" : "bg-rose-500 text-white"
                                }`}
                              >
                                {badge}
                              </span>
                            ) : null}
                          </>
                        );
                      }}
                    </NavLink>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>

        <div className="p-3 pt-1">
          <NavLink
            to="/settings/profile"
            className={({ isActive }) =>
              `flex items-center gap-2.5 px-2 py-2 rounded-xl text-[13px] transition-all ${
                isActive
                  ? "bg-gradient-to-r from-indigo-500 to-violet-500 text-white shadow-[0_6px_16px_-6px_rgba(99,102,241,0.65)]"
                  : "text-slate-600 hover:bg-indigo-50/60 hover:text-ink-900"
              }`
            }
          >
            <Avatar name={user?.name || user?.username || user?.email} size={30} />
            <span className="flex-1 min-w-0">
              <span className="block truncate font-medium">{user?.name || user?.username || user?.email || "Account"}</span>
              <span className="block text-[11px] opacity-70">Settings</span>
            </span>
          </NavLink>
        </div>
      </div>
    </aside>
  );
}
