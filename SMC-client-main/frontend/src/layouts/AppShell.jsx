import { NavLink, Outlet } from "react-router-dom";
import { Footprints, FlaskConical } from "lucide-react";
import BrandLogo from "../components/BrandLogo";

const NAV = [
  { to: "/footfall", label: "Unique Footfall", icon: Footprints },
  { to: "/footfall-uat", label: "Footfall UAT", icon: FlaskConical },
];

export default function AppShell() {
  return (
    <div className="flex flex-col md:flex-row min-h-screen bg-[#f5f6fa]">
      <aside className="md:w-60 shrink-0 bg-white border-b md:border-b-0 md:border-r border-border-200 p-4">
        <div className="flex items-center gap-2 mb-4 md:mb-6">
          <BrandLogo size={28} />
          <span className="font-semibold text-ink-900">SMC Footfall</span>
        </div>
        <nav className="flex md:flex-col gap-1">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              className={({ isActive }) =>
                `flex items-center gap-2 px-3 py-2 rounded-lg text-sm font-medium ${
                  isActive ? "bg-brand-50 text-brand-600" : "text-slate-600 hover:bg-[#f5f6fa]"
                }`
              }
            >
              <Icon size={16} />
              {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <main className="flex-1 min-w-0 p-4 md:p-6 max-w-[1400px] w-full mx-auto">
        <Outlet />
      </main>
    </div>
  );
}
