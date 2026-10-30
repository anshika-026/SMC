import { useEffect, useState } from "react";
import { Search } from "lucide-react";
import * as api from "../../api/client";
import { niceDate } from "./shared";

// Every visitor profile: search by name, Visitor ID, company, mobile or a
// date they visited. Click to open the profile.

export default function VisitorDirectory({ onOpenVisitor, refreshKey }) {
  const [q, setQ] = useState("");
  const [day, setDay] = useState("");
  const [rows, setRows] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const t = setTimeout(() => {
      api.vr.visitors({ q, day, limit: 500 }).then((d) => { setRows(d.visitors); setError(""); }).catch((e) => setError(e.message));
    }, 250);
    return () => clearTimeout(t);
  }, [q, day, refreshKey]);

  return (
    <div className="bg-white rounded-2xl shadow-[0_1px_3px_rgba(15,23,42,0.05)]">
      <div className="p-4 border-b border-slate-100 flex flex-wrap gap-2 items-center">
        <h3 className="text-sm font-semibold text-ink-900 mr-auto">Visitor directory</h3>
        <label className="relative">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
          <input className="input-field text-xs w-72" style={{ paddingLeft: "2rem" }} placeholder="Name, Visitor ID, company, mobile" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        <input type="date" className="input-field text-xs w-auto" title="Visited on" value={day} onChange={(e) => setDay(e.target.value)} />
        {day && <button type="button" className="text-xs text-brand-600" onClick={() => setDay("")}>Any date</button>}
      </div>
      {error && <p className="text-sm text-danger-500 p-4">{error}</p>}
      <div className="overflow-x-auto">
        <table className="data-table">
          <thead>
            <tr><th>Visitor</th><th>Company</th><th>Contact</th><th>Total visits</th><th>This month</th><th>First visit</th><th>Most recent</th></tr>
          </thead>
          <tbody>
            {rows && rows.length === 0 && <tr><td colSpan={7} className="text-center text-slate-400 py-8">No visitors found.</td></tr>}
            {(rows || []).map((r) => (
              <tr key={r.id} className="cursor-pointer hover:bg-slate-50/70" onClick={() => onOpenVisitor(r.id)}>
                <td><span className="font-medium text-ink-900">{r.name}</span><div className="text-[11px] text-slate-400">{r.visitor_code}</div></td>
                <td>{r.company || "—"}</td>
                <td className="text-xs">{r.mobile || r.email || "—"}</td>
                <td className="tabular-nums">{r.total_visits}</td>
                <td className="tabular-nums">{r.visits_this_month}</td>
                <td>{niceDate(r.first_visit)}</td>
                <td>{niceDate(r.last_visit)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
