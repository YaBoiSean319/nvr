import { useState, type FormEvent, type ReactNode } from "react";
import { ApiError } from "@/api/client";
import { Modal } from "@/components/Modal";
import { formatRelative } from "@/lib/time";
import { useSecurityActions, useSecuritySummary, type SecurityResult } from "./api";
import { passkeysSupported } from "./webauthn";

const message = (error: unknown): string => (error instanceof ApiError ? error.detail : error instanceof Error ? error.message : "That did not work. Try again.");

/** Recovery codes, shown once; there is no other way to see them again. */
function RecoveryCodesDialog({ codes, onClose }: { codes: string[] | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const text = (codes ?? []).join("\n");
  return (
    <Modal open={codes !== null} onOpenChange={open => { if (!open) onClose(); }} title="Save your recovery codes"
           description="If you lose your authenticator or passkey, each of these signs you in once. They are shown only now."
           footer={<button type="button" className="button" data-variant="primary" onClick={() => { setCopied(false); onClose(); }}>I have saved them</button>}>
      <ul className="recovery-codes mono" aria-label="Recovery codes">
        {(codes ?? []).map(code => <li key={code}>{code}</li>)}
      </ul>
      <div className="field-actions">
        <button type="button" className="button" onClick={() => { void navigator.clipboard?.writeText(text).then(() => setCopied(true)).catch(() => setCopied(false)); }}>Copy</button>
        <a className="button" download="nvr-recovery-codes.txt" href={`data:text/plain;charset=utf-8,${encodeURIComponent(text + "\n")}`}>Download</a>
        {copied ? <span role="status" className="test-ok">Copied.</span> : null}
      </div>
    </Modal>
  );
}

interface Reauth { title: string; description: string; run: (password: string) => Promise<SecurityResult> }

/** Changing how someone signs in asks for their password again. */
function PasswordDialog({ pending, onDone, onClose }: { pending: Reauth | null; onDone: (result: SecurityResult) => void; onClose: () => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!pending) return;
    setBusy(true);
    setError(null);
    try {
      const result = await pending.run(password);
      setPassword("");
      onDone(result);
    } catch (failure) {
      setError(message(failure));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Modal open={pending !== null} onOpenChange={open => { if (!open) { setPassword(""); setError(null); onClose(); } }} title={pending?.title ?? ""} description={pending?.description}
           footer={<>
             <button type="button" className="button" onClick={() => { setPassword(""); setError(null); onClose(); }}>Cancel</button>
             <button type="submit" form="reauth-form" className="button" data-variant="primary" disabled={busy || !password}>Confirm</button>
           </>}>
      <form id="reauth-form" className="auth-form" onSubmit={event => void submit(event)} noValidate>
        <div className="field">
          <label htmlFor="reauth-password">Your password</label>
          <input id="reauth-password" type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} autoFocus />
        </div>
        {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      </form>
    </Modal>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="field-group" aria-label={title}>
      <h3 className="field-heading">{title}</h3>
      {children}
    </section>
  );
}

/**
 * Passkeys, an authenticator app and recovery codes for one's own account. ``onChange`` runs once a
 * factor exists and any new recovery codes have been put away, so a gate can let the person through.
 */
export function SecurityPanel({ onChange }: { onChange?: () => void }) {
  const summary = useSecuritySummary();
  const actions = useSecurityActions();
  const [codes, setCodes] = useState<string[] | null>(null);
  const [pending, setPending] = useState<Reauth | null>(null);
  const [totp, setTotp] = useState<{ secret: string; otpauth_uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [passkeyName, setPasskeyName] = useState("");
  const [error, setError] = useState<string | null>(null);

  if (summary.isPending) return <p className="muted">Loading…</p>;
  if (summary.error || !summary.data) return <p className="notice" data-tone="bad" role="alert">{message(summary.error)}</p>;
  const data = summary.data;
  if (!data.local) return <p className="notice" role="note">This account signs in with Microsoft Entra. Its second factor is managed by your organization.</p>;
  const hasFactor = data.totp || data.passkeys.length > 0;

  function finished(result: SecurityResult) {
    setError(null);
    if (result.recovery_codes) setCodes(result.recovery_codes);
    else onChange?.();
  }

  async function attempt(run: () => Promise<SecurityResult>) {
    setError(null);
    try {
      finished(await run());
    } catch (failure) {
      setError(message(failure));
    }
  }

  async function begin() {
    setError(null);
    try {
      setTotp(await actions.beginTotp.mutateAsync());
      setCode("");
    } catch (failure) {
      setError(message(failure));
    }
  }

  const ask = (title: string, description: string, run: (password: string) => Promise<SecurityResult>) => setPending({ title, description, run });

  return (
    <div className="settings-stack security-panel">
      {data.required && !hasFactor ? (
        <p className="notice" data-tone="warn" role="status">Your administrator requires a second factor to sign in. Set up an authenticator app or a passkey to continue.</p>
      ) : hasFactor ? (
        <p className="notice" data-tone="good" role="status">Signing in asks for a second factor.</p>
      ) : (
        <p className="muted">Add a second factor so a stolen password is not enough to sign in.</p>
      )}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}

      <Section title="Authenticator app">
        {data.totp ? (
          <div className="field-actions">
            <span>An authenticator app is set up.</span>
            <button type="button" className="button" onClick={() => ask("Remove the authenticator app", "Signing in will no longer ask for its code.", password => actions.removeTotp.mutateAsync(password))}>Remove</button>
          </div>
        ) : totp ? (
          <form className="tab-form" aria-label="Set up an authenticator app" onSubmit={event => { event.preventDefault(); void attempt(() => actions.confirmTotp.mutateAsync(code.trim()).then(result => { setTotp(null); return result; })); }} noValidate>
            <p>In your authenticator app, add an account with this key, or open <a href={totp.otpauth_uri}>the setup link</a> on the device that has the app.</p>
            <p className="mono secret-key" aria-label="Setup key">{totp.secret}</p>
            <label className="field"><span>Code from the app</span>
              <input value={code} inputMode="numeric" autoComplete="one-time-code" maxLength={8} onChange={event => setCode(event.target.value)} />
            </label>
            <div className="field-actions">
              <button type="submit" className="button" data-variant="primary" disabled={!code.trim() || actions.confirmTotp.isPending}>Confirm</button>
              <button type="button" className="button" onClick={() => setTotp(null)}>Cancel</button>
            </div>
          </form>
        ) : (
          <div className="field-actions">
            <button type="button" className="button" onClick={() => void begin()}>Set up an authenticator app</button>
          </div>
        )}
      </Section>

      <Section title="Passkeys">
        {data.passkeys.length ? (
          <ul className="plain-list" aria-label="Your passkeys">
            {data.passkeys.map(item => (
              <li key={item.id}>
                <strong>{item.name}</strong> · added {formatRelative(item.created_at)} · {item.last_used_at ? `used ${formatRelative(item.last_used_at)}` : "never used"}{" "}
                <button type="button" className="button" aria-label={`Remove passkey ${item.name}`}
                        onClick={() => ask(`Remove ${item.name}`, "This passkey will no longer sign you in.", password => actions.removePasskey.mutateAsync({ id: item.id, password }))}>Remove</button>
              </li>
            ))}
          </ul>
        ) : <p className="muted">No passkeys yet.</p>}
        {passkeysSupported() ? (
          <form className="field-actions" aria-label="Add a passkey" onSubmit={event => { event.preventDefault(); void attempt(() => actions.addPasskey.mutateAsync(passkeyName.trim() || "Passkey").then(result => { setPasskeyName(""); return result; })); }}>
            <label className="field"><span>Name</span><input value={passkeyName} maxLength={64} placeholder="Laptop" onChange={event => setPasskeyName(event.target.value)} /></label>
            <button type="submit" className="button" disabled={actions.addPasskey.isPending}>Add a passkey</button>
          </form>
        ) : <p className="muted">This browser cannot create passkeys.</p>}
        {data.passkeys.length ? (
          <label className="check">
            <input type="checkbox" checked={data.passkey_sign_in}
                   onChange={event => {
                     const enabled = event.target.checked; // Read now: the box flips back until the password is confirmed.
                     ask(enabled ? "Sign in with only a passkey" : "Ask for my password too",
                       enabled ? "A passkey on its own will sign you in, without your password." : "Signing in will ask for your password again.",
                       password => actions.setPasskeySignIn.mutateAsync({ enabled, password }));
                   }} />
            {" "}Let me sign in with only a passkey
          </label>
        ) : null}
      </Section>

      {hasFactor ? (
        <Section title="Recovery codes">
          <p>{data.recovery_codes_remaining} of 10 recovery codes left. Each works once if you lose your authenticator or passkey.</p>
          <div className="field-actions">
            <button type="button" className="button" onClick={() => ask("Make new recovery codes", "The old codes will stop working.", password => actions.regenerateCodes.mutateAsync(password))}>Make new recovery codes</button>
          </div>
        </Section>
      ) : null}

      <RecoveryCodesDialog codes={codes} onClose={() => { setCodes(null); onChange?.(); }} />
      <PasswordDialog pending={pending} onClose={() => setPending(null)} onDone={result => { setPending(null); finished(result); }} />
    </div>
  );
}
