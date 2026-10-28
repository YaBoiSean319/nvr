# Passkeys and authenticator apps

Local accounts can add a second factor to sign-in: a **passkey** (WebAuthn), or an **authenticator app** (TOTP), with one-time **recovery codes** as the way back in. Microsoft Entra sign-in is unchanged: its multi-factor authentication stays with your tenant, and Entra accounts are never asked for a second factor here. API tokens are unchanged too.

## Setting one up

Open the account menu and choose **Security…**.

- **Authenticator app.** Add an account in your app with the key shown (or open the setup link on the device that has the app), then enter the code it shows. The key is stored encrypted with the credential key. Each code works once; the NVR also accepts the code before and after the current one, for clock drift.
- **Passkeys.** Choose **Add a passkey** and follow the browser's prompt. Give it a name so you can tell several apart. Any attestation format is accepted and ignored: the NVR only needs the passkey to prove it holds its private key. ES256, RS256 and EdDSA keys are supported.
- **Recovery codes.** Ten codes are made when you set up your first factor, and shown once. Each works once. Use **Make new recovery codes** to replace them all. They are stored only as digests.

Removing a factor, making new recovery codes, turning passkey-only sign-in on or off, and **adding another factor to an account that already has one**, all ask for your password again, so a borrowed session cannot weaken sign-in or add a factor of its own. After any change to how you sign in, your other sessions end; the one making the change stays. Setting up sign-in security needs a browser session: an API token, which acts for its creator, cannot.

## Signing in

1. Enter the username and password. An account with a second factor gets a short-lived, single-use challenge instead of a session (5 minutes, and five wrong answers burn it).
2. Answer it with a passkey, an authenticator code or a recovery code.

**Passkey alone.** Someone who prefers it can turn on **Let me sign in with only a passkey** in their security settings. **Use a passkey** on the sign-in page then signs them in without a password, and the passkey must have verified them (a PIN or biometric). It is off until they turn it on.

## Requiring a second factor

In **Settings › Users & access › Sign-in security**, an administrator chooses whether a second factor is **not required**, **required for administrators**, or **required for all local accounts**. People who have a factor are always asked for it, whatever the policy.

Where it is required, someone without a factor signs in with their password and reaches only enrolment: the application is replaced by "Set up a second factor", and the server refuses every other request until they have one. A session that is already open is asked as soon as the policy arrives. Turn the policy on after setting up a factor of your own, or you will be asked to straight away.

## Lost factors

An administrator can **Reset** a person's second factors in **Settings › Users & access**: it removes their authenticator app, passkeys and recovery codes and ends their sessions. They sign in with their password and set up a new factor if the policy asks. Resetting is audited, and not available for your own account (use the account menu).

If the only administrator loses their authenticator and recovery codes, run this on the NVR host, where the database is already readable:

```
python -m app.mfa reset USERNAME
```

## Rate limiting

Every password, authenticator or recovery code, passkey and password-confirmation attempt is counted **before** it is judged, in the same transaction that checks whether it may proceed, so a burst of parallel guesses cannot all slip past a check made before any was counted. A right answer takes its own count back. Each challenge also allows only five answers, shared by parallel requests.

Three buckets are kept:

| Bucket | Allowed before waiting | Why |
| --- | --- | --- |
| This account from this address | 4 | Stops a guesser quickly |
| The account, from anywhere | 19 | Guesses spread over many addresses |
| The address, for any account | 19 | One machine trying many accounts |

After the allowance each further attempt must wait, twice as long each time: 2, 4, 8 … seconds, capped at **5 minutes**. A throttled attempt is refused with `429` and a `Retry-After`, even with the right password, or the throttle would not slow guessing. Because the first bucket is per address, an attacker guessing from one address cannot keep the real person waiting when they sign in from another; only guessing spread over many addresses (which reaches the account bucket) can, and then for at most five minutes at a time. Nobody, including the emergency administrator, is ever locked out for good. Failures are forgotten after 15 quiet minutes, a completed sign-in clears the account's buckets, and old rows are pruned.

Refusing an unknown username takes as long as refusing a wrong password, so the two cannot be told apart by timing; the responses are identical.

**Behind a reverse proxy** every connection comes from the proxy, so every person would share one address bucket. List the proxy in `NVR_TRUSTED_PROXIES` ([remote access](remote-access.md)) and the address it reports is used (and it cannot be forged by sending the header yourself). The older option below still works: Set `NVR_TRUST_FORWARDED_FOR=true` only when the NVR is reachable solely through a proxy you control: the address is then the last `X-Forwarded-For` entry, which is the one your proxy added and a client cannot forge. Without a proxy, leave it unset, or clients could choose their own address.

## Passkeys and web addresses

Passkeys are tied to a host name, so they need the NVR to be opened at one (for example `https://nvr.example.com`) in a secure context; an IP address does not work, and the page says so. Set `NVR_PUBLIC_URL` to the address people use; it decides the passkey's origin and relying party. Without it, the origin the browser used for the request is trusted, which is fine for a single address but is not what you want behind a proxy. `NVR_WEBAUTHN_RP_ID` can name a parent domain to share passkeys between hosts. Changing the host name later makes existing passkeys unusable, so authenticator apps and recovery codes are the fallback.

## Audit

These are recorded in the audit log, each with the address where there is one:

| Action | When |
| --- | --- |
| `mfa_totp_enrolled`, `mfa_passkey_enrolled` | A factor was set up |
| `mfa_recovery_codes_generated` | Recovery codes were made (first factor, or regenerated) |
| `mfa_used`, `mfa_recovery_code_used` | A factor (or passkey alone, `passkey_sign_in`) completed a sign-in |
| `mfa_failed` | A code, passkey or password confirmation was wrong, with the method and reason |
| `mfa_factor_removed`, `mfa_passkey_sign_in_changed` | A factor was removed, or passkey-only sign-in changed |
| `mfa_reset` | An administrator (or the host) reset someone's factors |
| `mfa_policy_changed` | The policy changed, with before and after |
| `auth_throttled` | An account or address started waiting |

The authenticator secret and codes are never written to the log.

## Not covered

- The authenticator setup shows the key and a link, not a QR code.
- Passkeys are not tied to hardware attestation, so the NVR cannot tell what kind of authenticator made one.
