# SMC Connect: WhatsApp Business setup

SMC Connect sends SMC events (intrusion, loitering, visitor check-ins, late arrivals, attendance,
footfall and daily summaries) to WhatsApp through Meta's **WhatsApp Cloud API**. This guide lists
everything that must be configured outside SMC. Nothing here can be done by SMC itself: it needs
your Meta business accounts.

Until one of the two setups below is complete, SMC Connect shows **Demo mode** and sends nothing.

| Setup | Who connects WhatsApp | Credentials live in |
|---|---|---|
| **A. Embedded Signup** (recommended) | Each organization's admin, with **Continue with Meta** in SMC Connect | The SMC database, encrypted with `SMC_CREDENTIALS_KEY` |
| **B. Manual** | The server administrator | The SMC server's environment |

## What you need first

- A **Meta Business portfolio** (business.facebook.com), with business verification done.
  Verification is needed to message people at scale and to onboard other businesses.
- A **Meta app** of type *Business* (developers.facebook.com → My Apps → Create app), with the
  **WhatsApp** product added.
- SMC reachable over **HTTPS** at a public address, for example `https://smc.example.com`. Meta's
  JavaScript SDK and webhooks don't work over plain HTTP.

## A. Embedded Signup

Embedded Signup lets each organization authorize **its own** WhatsApp Business Account (WABA) and
phone number. Your Meta app then acts as a **Tech Provider**.

1. **Become a Tech Provider.** In the app dashboard: WhatsApp → Quickstart → follow the Tech
   Provider onboarding. Request **Advanced access** for `whatsapp_business_management` and
   `whatsapp_business_messaging` (App Review). Until it's granted, only businesses where you are an
   admin can complete the sign-up.
2. **Add Facebook Login for Business** to the app and create a **configuration** with the
   *WhatsApp Embedded Signup* login variation. Give it the two permissions above. Copy the
   **Configuration ID**.
3. In Facebook Login for Business → Settings:
   - add your SMC domain to **Allowed domains for the JavaScript SDK**;
   - turn on **Login with the JavaScript SDK**.
4. **Webhook**: WhatsApp → Configuration →
   - Callback URL: `https://<your SMC address>/api/connect/webhook`
   - Verify token: the value you set in `WHATSAPP_WEBHOOK_VERIFY_TOKEN`
   - Subscribe to the fields **messages** and **message_template_status_update**.

   SMC subscribes each newly connected WABA to your app automatically (`POST /<WABA_ID>/subscribed_apps`).
5. **Server environment** (`backend/.env`, never committed):

   ```
   META_APP_ID=<app id>
   META_APP_SECRET=<app secret>
   META_EMBEDDED_SIGNUP_CONFIG_ID=<configuration id>
   SMC_CREDENTIALS_KEY=<output of: python -m app.manage generate-credentials-key>
   WHATSAPP_WEBHOOK_VERIFY_TOKEN=<any long random string>
   SMC_ORG_ID=<a short id for this organization, for example acme>
   ```

   Keep `SMC_CREDENTIALS_KEY` **outside** the database and its backups. Anyone with both can read
   the stored tokens. If the key is lost, organizations simply connect again.
6. Restart the backend. In SMC → profile menu → **SMC Connect** → **Connect WhatsApp Business** →
   **Continue with Meta**.
   - For a number that is new to the Cloud API, choose a 6-digit PIN in SMC **before** opening
     Meta's window. SMC uses it to register the number.
   - The code Meta returns is valid for 30 seconds. SMC uses it immediately.
   - SMC checks that the number belongs to the account the admin authorized, stores the token
     encrypted, subscribes the webhooks, registers the number and syncs the templates.

## B. Manual setup (one installation, one number)

1. In WhatsApp Manager, add and verify the business phone number.
2. In Business Settings → System users:
   - create a system user and assign it the WhatsApp Business Account;
   - generate a token with `whatsapp_business_messaging` and `whatsapp_business_management`.
3. Configure the webhook as in step A4.
4. **Server environment**:

   ```
   WHATSAPP_ACCESS_TOKEN=<system-user token>
   WHATSAPP_PHONE_NUMBER_ID=<phone number id>
   WHATSAPP_BUSINESS_ACCOUNT_ID=<WABA id>
   WHATSAPP_APP_SECRET=<app secret>
   WHATSAPP_WEBHOOK_VERIFY_TOKEN=<any long random string>
   ```
5. In SMC Connect → **Connect WhatsApp Business**: enter the number, then **Verify with Meta**.
   SMC shows **Connected** only when Meta answers for that number with that token.

## Message templates (required for alerts and reports)

Meta allows a business to **start** a conversation only with an **approved message template**.
Free text is accepted only for 24 hours after the recipient last messaged your number. SMC
alerts and reports start conversations, so:

1. In WhatsApp Manager → Message templates, create one **Utility** template per notification.
   Example for intrusion:
   `Intrusion detected at {{1}}, camera {{2}}, {{3}}. {{4}}`
2. Wait for Meta's approval.
3. In SMC Connect → Message templates:
   - click **Sync from Meta**;
   - pick the template for each notification;
   - map each `{{n}}` to an SMC field (zone, camera, time, message …).

Without a linked template, a message is logged as **Not sent** with the reason, and SMC doesn't
call Meta. If Meta pauses or rejects a template later, SMC hears it through the webhook and stops
using it.

## Recipients and consent

- Messages go only to recipients marked **Opted in**. In Recipients, use **Record consent**: pick
  how they agreed and confirm. SMC stores the method, the time and the admin who recorded it.
- A recipient who replies **STOP** to your WhatsApp number is opted out automatically. **START**
  opts them in again.
- Test messages also go only to opted-in recipients.

## How sending works

- SMC events are turned into queued messages by a background thread. A second thread sends them.
  Camera streaming and AI detection never wait for WhatsApp.
- Network errors, Meta 5xx responses and rate limits are retried 4 times: after 30 s, 2 min,
  10 min and 30 min. Other Meta errors fail at once, with Meta's message shown in the history.
- An event is sent at most once per recipient, even after a restart.
- Statuses come from Meta: **Sent** (accepted), then **Delivered** and **Read** (webhook), or
  **Failed** with Meta's reason. SMC never marks anything delivered on its own.

## Organizations

Every SMC Connect table carries the organization id (`SMC_ORG_ID`), and every query filters on it.
Meta's webhook is routed to an organization by the phone number or business account it concerns.
An SMC installation serves one organization, so give each installation its own `SMC_ORG_ID`. A
phone number can be connected to only one organization.

## Troubleshooting

| Meta error | Meaning |
|---|---|
| 131047 *Re-engagement message* | Free text outside the 24-hour window: link an approved template |
| 131030 *Recipient not in allowed list* | Meta's **test** number can only message up to 5 allowed numbers: add the recipient in the app dashboard, or use a real business number |
| 133010 *Account not registered* | The number wasn't registered: connect again and enter the PIN |
| 190 *Invalid OAuth access token* | The token expired or was revoked: connect again (Embedded Signup) or replace `WHATSAPP_ACCESS_TOKEN` |
| 130429 / 131056 *Rate limit* | Retried automatically |

SMC Connect's connection state and Meta's last error are shown at the top of its page; each
message's details (reason, attempts, template or free text) are in **Notification history** (click a row).
