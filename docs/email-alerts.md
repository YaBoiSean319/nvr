# Email alerts with Microsoft 365 (#231)

Alert emails are sent through **Microsoft Graph** (`sendMail`) with a Microsoft sign-in, not a mail password: Microsoft is retiring basic SMTP login for Microsoft 365. The only permission used is the delegated **Mail.Send**.

## Set it up

1. In **Microsoft Entra** (entra.microsoft.com) > App registrations, register an app (single tenant is fine). Under **API permissions** add Microsoft Graph > Delegated > **Mail.Send**. Under **Authentication** turn on **Allow public client flows**. No client secret is needed. Note the **Application (client) ID** and your **Tenant ID**.
2. In the NVR: **Settings > Email alerts**. Enter the tenant ID (or domain, such as `contoso.onmicrosoft.com`) and the application ID, and press **Connect with Microsoft**.
3. The page shows a **link and a code**. Open the link on any device, enter the code and sign in with the account that should send the alerts. The page notices by itself and shows "Connected as ...".
4. Send a test email from the same page.
5. Add the rule action **Email (Microsoft 365)** to a rule: recipients, and an optional subject (`{rule}`, `{camera}`, `{label}`, `{time}`).

To send as a shared mailbox, put its address in **Send as**; the signed-in account needs permission to send as it.

## What it does and does not do

- The message names the rule, the camera and the time and links to the alarm (which needs a sign-in). It carries **no footage and no camera addresses**.
- Failed sends are retried with backoff, then marked failed in the rule's delivery log. If Microsoft refuses the saved sign-in (expired, withdrawn, password changed), the page says **Reconnect** and shows the reason.
- **Limits:** at most 20 emails an hour per rule and 100 an hour in all. Past the limit, further sends are marked failed (`email_rate_limited`); the alarms still appear in the inbox. A digest of what was held back is not built yet.
- Tokens are stored encrypted with the credential key, like camera credentials, never shown again, never in logs or audit entries. Connecting, failing, testing and disconnecting are audited. Only administrators can manage it.
- The refresh token lets the NVR keep sending until it is revoked or Microsoft's sign-in policy expires it; a conditional access policy that requires a fresh sign-in will need a reconnect.

## If sign-in works but sending does not

The settings page and the test email now show what Microsoft actually said (its error code and message) and what usually causes it. The common ones:

| Microsoft says | Usually |
| --- | --- |
| `ErrorAccessDenied` | The signed-in account has no Exchange Online mailbox or licence, or a policy blocks it. Sign in with an account that has a mailbox |
| `MailboxNotEnabledForRESTAPI` | The account has no Exchange Online mailbox for the API (unlicensed, or the mailbox is on-premises) |
| `ErrorSendAsDenied` | The account may not send as the shared mailbox in "Send as": give it Send As in Exchange |
| `AADSTS65001` | Consent was not given for Mail.Send: sign in again and accept, or an administrator grants it |
| `AADSTS50158` / `AADSTS53003` | A conditional access policy (compliant device, location, blocked app) is stopping the app |
| `AADSTS7000218` | The app registration needs "Allow public client flows" turned on |
| `invalid_grant` | The saved sign-in expired or was withdrawn (password change, policy): sign in again |

**Sent, but never arrives.** The test email asks Microsoft to keep a copy in the sending account's **Sent Items**, and the page then says who it was sent as, to whom, when, and Microsoft's request id. If Microsoft says it accepted the message, Exchange has it, and the problem is after sending:

1. **Sent Items of the sending account** (open it in Outlook on the web): the test email is there if Exchange took it.
2. **That account's Inbox**: a bounce ("Undeliverable") names the reason, for example a recipient that does not exist or an external address the tenant will not send to.
3. **The recipient's Junk or quarantine folder.**
4. **Exchange admin centre > Mail flow > Message trace** for the sender and the recipient around that time, which says delivered, failed, quarantined or pending, and which rule did it.
5. **Mail flow rules (transport rules)**, outbound spam policies and sending limits in the tenant, which can drop or hold messages from a particular account.

## Emails about the NVR itself

**Settings > Email alerts > NVR problems**: choose who is emailed when the NVR finds a problem in itself (a camera that stopped recording, a full or failing disk, the AI worker stopping, a covered camera, a failed backup): one email when it starts and one when it is mended. Nothing is sent about a camera during a [maintenance window](maintenance-windows.md); a problem still there when the window ends is emailed then. Failed sends are retried with backoff, and the page lists the recent ones.

**Held back.** At most 30 problem emails an hour are sent (and the rule limits above). Every alert email a limit stops is counted, shown on the page, and told about in **one summary email** once things have been quiet for 10 minutes ("N alert emails held back"), sent to the people chosen for problems. The alerts themselves are in the alarm inbox and the incident list.

## For installers

`NVR_MS_LOGIN_URL` (default `https://login.microsoftonline.com`) and `NVR_MS_GRAPH_URL` (default `https://graph.microsoft.com/v1.0`) can point at another cloud (such as a sovereign one) or a test stand-in.

## Not built yet

Per-person recipients in notification preferences, an attached snapshot, and other providers. Not yet checked against a real Microsoft 365 tenant: connect one and send a test.
