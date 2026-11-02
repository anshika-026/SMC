import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import AuthLayout from "../layouts/AuthLayout";
import { useAuth } from "../context/AuthContext";
import * as api from "../api/client";

// Landing page for a client who signed in on the Deco Vision Master Portal.
// The portal sends the browser to /sso#token=<one-minute launch token>; the
// fragment never reaches any server log. The token is taken out of the
// address bar straight away, exchanged once for an SMC session
// (backend/app/sso_routes.py), and the existing SMC dashboard opens.
export default function Sso() {
  const { loginWithSso } = useAuth();
  const navigate = useNavigate();
  const [error, setError] = useState("");
  const [loginUrl, setLoginUrl] = useState("");
  const started = useRef(false); // a launch token works once; never send it twice

  useEffect(() => {
    if (started.current) return;
    started.current = true;
    const token = new URLSearchParams(window.location.hash.slice(1)).get("token");
    window.history.replaceState(null, "", window.location.pathname);
    api.getSsoConfig().then((c) => setLoginUrl(c.login_url || "")).catch(() => {});
    if (!token) {
      setError("This sign-in link is incomplete. Please sign in again.");
      return;
    }
    loginWithSso(token)
      .then(() => navigate("/dashboard", { replace: true }))
      .catch((err) => setError(err.message || "Couldn't sign you in. Please try again."));
  }, [loginWithSso, navigate]);

  return (
    <AuthLayout>
      {error ? (
        <>
          <h1 className="text-lg font-semibold text-ink-900">Sign-in didn't work</h1>
          <p className="text-sm text-danger-500 mt-2">{error}</p>
          {loginUrl && (
            <a href={loginUrl} className="btn-primary w-full mt-6 inline-flex justify-center">
              Sign in again
            </a>
          )}
        </>
      ) : (
        <>
          <h1 className="text-lg font-semibold text-ink-900">Signing you in…</h1>
          <p className="text-sm text-slate-500 mt-1">Checking your license with Deco Vision.</p>
        </>
      )}
    </AuthLayout>
  );
}
