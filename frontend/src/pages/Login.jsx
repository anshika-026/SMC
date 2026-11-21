import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import AuthLayout from "../layouts/AuthLayout";
import PasswordField from "../components/PasswordField";
import { useAuth } from "../context/AuthContext";
import * as api from "../api/client";
import { clearPortalTicket, readPortalTicket } from "../lib/portalTicket";

export default function Login() {
  const { login } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [masterLoginUrl, setMasterLoginUrl] = useState("");
  // Connected to the Deco Vision portal: the form needs the ticket from a portal
  // pass (pages/Sso.jsx) as well as the SMC email and password.
  const [portalMode, setPortalMode] = useState(false);
  const [portalTicket, setPortalTicket] = useState(() => readPortalTicket());

  useEffect(() => {
    api
      .getSsoConfig()
      .then((c) => {
        setPortalMode(c.mode === "master");
        setMasterLoginUrl(c.mode === "master" ? c.login_url || "" : "");
      })
      .catch(() => {});
  }, []);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await login(email, password, portalMode ? portalTicket?.ticket : null);
      clearPortalTicket();
      navigate("/dashboard");
    } catch (err) {
      if (portalMode && !readPortalTicket()) setPortalTicket(null);   // the ticket ran out meanwhile
      setError(err?.message || "Couldn't log in. Check your email and password and try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <AuthLayout>
      <h1 className="text-lg font-semibold text-ink-900">Log in</h1>
      <p className="text-sm text-slate-500 mt-1 mb-6">
        {portalMode && portalTicket
          ? `License confirmed by Deco Vision${portalTicket.company ? ` for ${portalTicket.company}` : ""}. Sign in with your SMC account.`
          : "Welcome back to Deco Vision"}
      </p>

      {portalMode && !portalTicket ? (
        <div className="space-y-4">
          <p className="text-sm text-slate-600">SMC is opened from the Deco Vision portal. Sign in there and choose SMC.</p>
          {error && <p className="text-sm text-danger-500">{error}</p>}
          {masterLoginUrl && (
            <a href={masterLoginUrl} className="btn-primary w-full inline-flex justify-center">
              Go to Deco Vision
            </a>
          )}
        </div>
      ) : (
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="text-sm font-medium text-ink-900 block mb-1.5">Email</label>
          <input
            type="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="Email address"
            className="input-field"
          />
        </div>
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="text-sm font-medium text-ink-900">Password</label>
            <span className="text-xs text-slate-500">Forgot it? Ask your administrator.</span>
          </div>
          <PasswordField
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
          />
        </div>

        {error && <p className="text-sm text-danger-500">{error}</p>}

        <button type="submit" disabled={loading} className="btn-primary w-full">
          {loading ? "Logging in..." : "Log in"}
        </button>
      </form>
      )}

      {portalMode ? null : masterLoginUrl ? (
        // Connected to the Deco Vision Master Portal: clients sign in there.
        <a href={masterLoginUrl} className="btn-secondary w-full mt-4 inline-flex justify-center">
          Sign in with Deco Vision
        </a>
      ) : (
        <p className="text-sm text-slate-500 text-center mt-6">
          Client portal login?{" "}
          <Link to="/client-login" className="text-brand-600 font-medium">
            Log in here
          </Link>
        </p>
      )}
    </AuthLayout>
  );
}
