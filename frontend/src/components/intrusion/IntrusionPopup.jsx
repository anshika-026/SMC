import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ShieldAlert, X } from "lucide-react";
import * as api from "../../api/client";
import { featureOn, licenseOn } from "../../lib/features";
import { setVisibleInterval, clearVisibleInterval } from "../../lib/visibleInterval";

const POLL_MS = 5_000;
const SHOW_MS = 8_000;
const NAME_WAIT_S = 12;   // a new intrusion pops up once someone is named, or after this long

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

// A short, soft two-tone beep (Web Audio, no sound file). Browsers only allow sound after the
// user has clicked somewhere on the page once; before that it stays silent, the pop-up still shows.
function beep() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = beep.ctx || (beep.ctx = new Ctx());
    if (ctx.state === "suspended") ctx.resume();
    [0, 0.22].forEach((at, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.type = "sine";
      osc.frequency.value = i ? 660 : 880;
      const t = ctx.currentTime + at;
      gain.gain.setValueAtTime(0.0001, t);
      gain.gain.exponentialRampToValueAtTime(0.08, t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
      osc.connect(gain).connect(ctx.destination);
      osc.start(t);
      osc.stop(t + 0.2);
    });
  } catch {
    /* sound is a convenience only */
  }
}

// New intrusion / loitering alerts, on every page: a pop-up (who, where, when) with a soft beep,
// gone by itself after a few seconds; a click opens the Intrusion page. Alerts that already
// existed when the page was opened never pop up.
export default function IntrusionPopup() {
  const enabled = featureOn("intrusion") && licenseOn("intrusion_detection");
  const navigate = useNavigate();
  const [toasts, setToasts] = useState([]);
  // alert id -> names already announced for it; null until the first look (whatever exists then
  // never pops up).
  const announced = useRef(null);

  const dismiss = useCallback((key) => setToasts((t) => t.filter((x) => x.key !== key)), []);

  const poll = useCallback(() => {
    const day = todayIso();
    api.intrusionApi.alerts({ start: day, end: day }).then((d) => {
      const rows = (d.alerts || []).slice().sort((a, b) => a.id - b.id);
      const names = (r) => (r.people || []).map((p) => p.name);
      if (announced.current === null) {
        announced.current = new Map(rows.map((r) => [r.id, new Set(names(r))]));
        return;
      }
      const now = Date.now() / 1000;
      const fresh = [];
      rows.filter((r) => r.status === "Active").forEach((r) => {
        const done = announced.current.get(r.id);
        if (!done) {
          // A new intrusion: wait a few seconds for face recognition to name the person.
          if (!r.people?.length && now - r.ts < NAME_WAIT_S) return;
          announced.current.set(r.id, new Set(names(r)));
          fresh.push({ ...r, key: `${r.id}`, title: r.who });
          return;
        }
        // Someone else came in while this intrusion is still going on: their own pop-up.
        const added = names(r).filter((n) => !done.has(n));
        if (!added.length) return;
        added.forEach((n) => done.add(n));
        fresh.push({ ...r, key: `${r.id}:${added.join(",")}`, title: added.join(", "), ts: now });
      });
      if (!fresh.length) return;
      beep();
      setToasts((t) => [...t, ...fresh].slice(-3));
      fresh.forEach((x) => setTimeout(() => dismiss(x.key), SHOW_MS));
    }).catch(() => {});
  }, [dismiss]);

  useEffect(() => {
    if (!enabled) return undefined;
    poll();
    const h = setVisibleInterval(poll, POLL_MS);
    return () => clearVisibleInterval(h);
  }, [enabled, poll]);

  if (!toasts.length) return null;
  return (
    <div className="fixed top-4 right-4 z-[60] flex flex-col gap-2 w-[min(360px,calc(100vw-32px))]" role="alert" aria-live="assertive">
      {toasts.map((a) => (
        <div key={a.key} className="bg-white rounded-2xl shadow-lg border-l-4 border-danger-500 p-3 flex gap-3 items-start">
          <ShieldAlert size={20} className="text-danger-500 shrink-0 mt-0.5" />
          <button type="button" className="text-left flex-1 min-w-0" onClick={() => { dismiss(a.key); navigate("/intrusion"); }}>
            <p className="text-sm font-semibold text-ink-900 truncate">{a.title}</p>
            <p className="text-xs text-slate-600 truncate">
              {a.event === "Loitering Detected" ? "Loitering" : "Came in"} · {a.zone_name} · {a.camera_name} · {new Date(a.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
            </p>
          </button>
          <button type="button" aria-label="Close" className="text-slate-400 hover:text-ink-900" onClick={() => dismiss(a.key)}>
            <X size={16} />
          </button>
        </div>
      ))}
    </div>
  );
}
