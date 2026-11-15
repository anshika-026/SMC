import { useAuth } from "../context/AuthContext";

// Shown while SMC can't reach the Deco Vision portal to check its license
// (backend/app/master_link.py state "grace"): everything keeps working until
// the grace period ends, then SMC locks until the portal answers again.
export default function LicenseGraceBanner() {
  const { user } = useAuth();
  const lic = user?.license;
  if (lic?.mode !== "master" || lic.state !== "grace" || !lic.grace_until) return null;
  const when = new Date(lic.grace_until * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  const checked = lic.checked_at ? new Date(lic.checked_at * 1000).toLocaleString([], { timeStyle: "short", dateStyle: "medium" }) : "";
  return (
    <div role="status" className="bg-amber-50 border-b border-amber-200 text-amber-800 text-sm px-6 py-2">
      SMC can't reach Deco Vision to check its license{checked ? ` (last checked ${checked})` : ""}. Everything keeps
      working until {when}; after that SMC locks until the license can be checked again.
    </div>
  );
}
