import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import AuthLayout from "../layouts/AuthLayout";
import * as api from "../api/client";
import { savePortalTicket } from "../lib/portalTicket";

// Landing page for a visit from the Deco Vision Master Portal. The portal sends
// the browser to /sso#token=<one-minute pass>; the fragment never reaches any
// server log. The pass is taken out of the address bar straight away and
// exchanged once (backend/app/sso_routes.py) for a short login ticket. It does
// not log anyone in: the normal SMC login form opens, and needs the ticket plus
// the SMC email and password.
export default function Sso() {
  const navigate = useNavigate();
  const [error, setError] = useState("");
  const [loginUrl, setLoginUrl] = useState("");
  const started = useRef(false); // a pass works once; never send it twice

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
    window.history.replaceState(null, "", window.location.pathname);
    api.getSsoConfig().then((c) => setLoginUrl(c.login_url || "")).catch(() => {});
    if (!token) {
      setError("This sign-in link is incomplete. Please open SMC from Deco Vision again.");
      return;
    }
    api
      .ssoExchange(token)
      .then((res) => {
        savePortalTicket(res.ticket, res.expires_in, res.company);
        navigate("/login", { replace: true });
      })
      .catch((err) => setError(err.message || "Couldn't check this sign-in link. Please try again."));
  }, [navigate]);

  return (
    <AuthLayout>
      {error ? (
        <>
          <h1 className="text-lg font-semibold text-ink-900">Sign-in didn't work</h1>
          <p className="text-sm text-danger-500 mt-2">{error}</p>
          {loginUrl && (
            <a href={loginUrl} className="btn-primary w-full mt-6 inline-flex justify-center">
              Back to Deco Vision
            </a>
          )}
        </>
      ) : (
        <>
          <h1 className="text-lg font-semibold text-ink-900">Checking your license…</h1>
          <p className="text-sm text-slate-500 mt-1">Deco Vision is confirming your access to SMC.</p>
        </>
      )}
    </AuthLayout>
  );
}
