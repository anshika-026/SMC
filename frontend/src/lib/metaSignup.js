// Meta WhatsApp Embedded Signup in the browser (developers.facebook.com/docs/whatsapp/embedded-signup).
// Opens Meta's own popup; Meta returns a code (valid 30 seconds) through FB.login and the chosen
// WhatsApp Business Account / phone number through a window message. Both go straight to the SMC
// server, which exchanges the code for the token. No token ever reaches this page.

const SDK_URL = "https://connect.facebook.net/en_US/sdk.js";
let sdk = null;

function loadSdk(appId, version) {
  if (sdk) return sdk;
  sdk = new Promise((resolve, reject) => {
    window.fbAsyncInit = () => {
      window.FB.init({ appId, autoLogAppEvents: true, xfbml: false, version });
      resolve(window.FB);
    };
    const s = document.createElement("script");
    s.src = SDK_URL;
    s.async = true;
    s.defer = true;
    s.crossOrigin = "anonymous";
    s.onerror = () => {
      sdk = null;
      reject(new Error("Couldn't load Meta's sign-in (check the network or an ad blocker)."));
    };
    document.body.appendChild(s);
  });
  return sdk;
}

const fromMeta = (origin) => {
  try {
    const host = new URL(origin).hostname;
    return host === "facebook.com" || host.endsWith(".facebook.com");
  } catch {
    return false;
  }
};

// Resolves {code, waba_id, phone_number_id} or rejects with a message for the user.
export async function runEmbeddedSignup({ appId, configId, version }) {
  const FB = await loadSdk(appId, version);
  return new Promise((resolve, reject) => {
    let code = null;
    let session = null;
    let timer = null;
    const done = (fn, v) => {
      window.removeEventListener("message", onMessage);
      clearTimeout(timer);
      fn(v);
    };
    const settle = () => {
      if (code && session) done(resolve, { code, waba_id: session.waba_id, phone_number_id: session.phone_number_id });
    };
    function onMessage(event) {
      if (!fromMeta(event.origin)) return;
      let msg;
      try {
        msg = typeof event.data === "string" ? JSON.parse(event.data) : event.data;
      } catch {
        return;
      }
      if (msg?.type !== "WA_EMBEDDED_SIGNUP") return;
      if (msg.event === "CANCEL") return done(reject, new Error(`Sign-up was closed${msg.data?.current_step ? ` at "${msg.data.current_step}"` : ""}.`));
      if (msg.event === "ERROR") return done(reject, new Error(msg.data?.error_message || "Meta reported an error during sign-up."));
      if (String(msg.event || "").startsWith("FINISH")) {
        if (!msg.data?.phone_number_id || !msg.data?.waba_id) {
          return done(reject, new Error("No phone number was added in Meta's sign-up. Add a number, then connect again."));
        }
        session = msg.data;
        settle();
      }
    }
    window.addEventListener("message", onMessage);
    FB.login(
      (response) => {
        code = response?.authResponse?.code || null;
        if (!code) return done(reject, new Error("Meta sign-in was not completed."));
        settle();
        // The window message normally arrives first; don't wait long, the code expires in 30 s.
        timer = setTimeout(() => done(reject, new Error("Meta didn't report which WhatsApp number was chosen. Try again.")), 10000);
      },
      { config_id: configId, response_type: "code", override_default_response_type: true, extras: { setup: {} } },
    );
  });
}
