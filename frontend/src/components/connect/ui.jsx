import { X } from "lucide-react";

// Dark-theme building blocks for SMC Connect (the rest of SMC is light; this page is a dark
// "control room" panel inside the normal layout, using the same brand tokens).

export const STATUS = {
  connected: { label: "Connected", cls: "bg-emerald-500/15 text-emerald-300 ring-emerald-400/30", dot: "bg-emerald-400" },
  pending: { label: "Waiting for verification", cls: "bg-amber-500/15 text-amber-300 ring-amber-400/30", dot: "bg-amber-400" },
  failed: { label: "Verification failed", cls: "bg-rose-500/15 text-rose-300 ring-rose-400/30", dot: "bg-rose-400" },
  disconnected: { label: "Disconnected", cls: "bg-slate-500/15 text-slate-300 ring-slate-400/30", dot: "bg-slate-400" },
  not_connected: { label: "Not connected", cls: "bg-slate-500/15 text-slate-300 ring-slate-400/30", dot: "bg-slate-400" },
  not_configured: { label: "Demo mode", cls: "bg-indigo-500/15 text-indigo-200 ring-indigo-400/30", dot: "bg-indigo-400" },
};

export const MSG_STATUS = {
  sent: { label: "Sent", cls: "bg-sky-500/15 text-sky-300", hint: "Accepted by Meta" },
  delivered: { label: "Delivered", cls: "bg-emerald-500/15 text-emerald-300", hint: "Delivered to the phone" },
  read: { label: "Read", cls: "bg-emerald-500/25 text-emerald-200", hint: "Opened by the recipient" },
  failed: { label: "Failed", cls: "bg-rose-500/15 text-rose-300", hint: "Meta could not send it" },
  not_sent: { label: "Not sent", cls: "bg-slate-500/20 text-slate-300", hint: "WhatsApp was not connected" },
};

export function StatusPill({ status }) {
  const s = STATUS[status] || STATUS.not_connected;
  return (
    <span className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 ${s.cls}`}>
      <span className={`w-1.5 h-1.5 rounded-full ${s.dot}`} />
      {s.label}
    </span>
  );
}

export function MsgBadge({ status }) {
  const s = MSG_STATUS[status] || { label: status, cls: "bg-slate-500/20 text-slate-300" };
  return <span title={s.hint} className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${s.cls}`}>{s.label}</span>;
}

export function Panel({ className = "", children }) {
  return <div className={`rounded-2xl bg-white/[0.04] ring-1 ring-white/10 ${className}`}>{children}</div>;
}

export function Switch({ checked, onChange, disabled, label }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-block w-9 h-5 rounded-full transition-colors shrink-0 disabled:opacity-40 ${checked ? "bg-[#25D366]" : "bg-white/15"}`}
    >
      <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${checked ? "left-[18px]" : "left-0.5"}`} />
    </button>
  );
}

export const field = "w-full rounded-xl bg-white/[0.06] ring-1 ring-white/10 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:outline-none focus:ring-2 focus:ring-[#25D366]/60";
export const btn = "inline-flex items-center justify-center gap-1.5 rounded-xl px-3.5 py-2 text-sm font-medium transition disabled:opacity-40";
export const btnPrimary = `${btn} bg-[#25D366] text-[#062b16] hover:bg-[#3ee27a]`;
export const btnGhost = `${btn} bg-white/[0.06] text-slate-200 ring-1 ring-white/10 hover:bg-white/10`;
export const btnDanger = `${btn} bg-rose-500/15 text-rose-200 ring-1 ring-rose-400/30 hover:bg-rose-500/25`;

export function Label({ children }) {
  return <span className="block text-[11px] uppercase tracking-wide text-slate-400 mb-1">{children}</span>;
}

export function DarkModal({ open, onClose, title, children, width = "max-w-lg" }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div role="dialog" aria-label={title} className={`w-full ${width} rounded-2xl bg-[#0f1528] ring-1 ring-white/10 text-slate-200 shadow-2xl`} onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-between px-5 py-4 border-b border-white/10">
          <h3 className="font-semibold text-white">{title}</h3>
          <button type="button" aria-label="Close" onClick={onClose} className="text-slate-400 hover:text-white">
            <X size={18} />
          </button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

// A WhatsApp-style chat bubble for previews (WhatsApp's *bold* shown as bold).
export function Bubble({ text, note }) {
  const html = (text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/\*([^*\n]+)\*/g, "<b>$1</b>")
    .replace(/_([^_\n]+)_/g, "<i>$1</i>");
  return (
    <div className="rounded-2xl bg-[#0b141a] p-4 ring-1 ring-white/5">
      <div className="max-w-[92%] rounded-xl rounded-tl-none bg-[#005c4b] px-3 py-2 text-[13px] leading-relaxed text-[#e9edef] whitespace-pre-wrap shadow"
        // eslint-disable-next-line react/no-danger -- escaped above; only *bold* / _italic_ become tags
        dangerouslySetInnerHTML={{ __html: html }} />
      {note && <p className="text-[10px] text-slate-500 mt-2">{note}</p>}
    </div>
  );
}

export const COUNTRY_CODES = [
  ["+91", "India"], ["+971", "UAE"], ["+1", "USA / Canada"], ["+44", "UK"], ["+65", "Singapore"], ["+61", "Australia"], ["+966", "Saudi Arabia"],
];
