import { useState, type FormEvent } from "react";
import { ApiError } from "@/api/client";
import { useOidcStatus, useSignIn } from "@/api/queries";
import type { MfaChallenge } from "@/api/types";
import { AuthScreen } from "./AuthScreen";
import { usePasskeySecondFactor, usePasskeySignIn, useSecondFactor } from "./api";
import { passkeysSupported } from "./webauthn";

const messageOf = (error: unknown): string | null =>
  error instanceof ApiError ? error.detail : error instanceof Error ? error.message : error ? "Sign-in failed. Try again." : null;

export function SignInPage() {
  const signIn = useSignIn();
  const passkey = usePasskeySignIn();
  const oidc = useOidcStatus();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [challenge, setChallenge] = useState<MfaChallenge | null>(null);

  function submit(event: FormEvent) {
    event.preventDefault();
    signIn.mutate({ username: username.trim(), password }, { onSuccess: result => { if ("mfa_required" in result) { setChallenge(result); setPassword(""); } } });
  }

  if (challenge) {
    return <SecondFactorStep challenge={challenge} username={username.trim()} onBack={() => { setChallenge(null); signIn.reset(); }} />;
  }

  const error = messageOf(signIn.error) ?? messageOf(passkey.error);
  return (
    <AuthScreen title="Sign in">
      {oidc.data?.enabled ? (
        <>
          <a className="button auth-wide" data-variant="primary" href="/api/auth/oidc/login">
            Sign in with Microsoft Entra ID
          </a>
          <p className="auth-divider"><span>or use a local account</span></p>
        </>
      ) : null}
      <form className="auth-form" onSubmit={submit} noValidate>
        <div className="field">
          <label htmlFor="sign-in-username">Username</label>
          <input id="sign-in-username" autoComplete="username webauthn" value={username} onChange={event => setUsername(event.target.value)} required autoFocus />
        </div>
        <div className="field">
          <label htmlFor="sign-in-password">Password</label>
          <input id="sign-in-password" type="password" autoComplete="current-password" value={password} onChange={event => setPassword(event.target.value)} required />
        </div>
        {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
        <button className="button auth-wide" data-variant={oidc.data?.enabled ? undefined : "primary"} type="submit" disabled={signIn.isPending || !username || !password}>
          {signIn.isPending ? <span className="spinner" aria-hidden="true" /> : null}
          Sign in
        </button>
        {passkeysSupported() ? (
          <button className="button auth-wide" type="button" disabled={passkey.isPending} onClick={() => { signIn.reset(); passkey.mutate(); }}>
            {passkey.isPending ? <span className="spinner" aria-hidden="true" /> : null}
            Use a passkey
          </button>
        ) : null}
      </form>
    </AuthScreen>
  );
}

const METHOD_LABEL = { passkey: "Use a passkey", totp: "Use an authenticator code", recovery: "Use a recovery code" } as const;

/** The second step: a passkey, an authenticator code or a recovery code, whichever the account has. */
function SecondFactorStep({ challenge, username, onBack }: { challenge: MfaChallenge; username: string; onBack: () => void }) {
  const methods = challenge.methods.filter(method => method !== "passkey" || passkeysSupported());
  const [method, setMethod] = useState(methods[0] ?? "recovery");
  const [code, setCode] = useState("");
  const verify = useSecondFactor();
  const passkey = usePasskeySecondFactor();
  const error = messageOf(verify.error) ?? messageOf(passkey.error);

  function submit(event: FormEvent) {
    event.preventDefault();
    if (method !== "passkey") verify.mutate({ challenge: challenge.challenge, method, code: code.trim() });
  }

  return (
    <AuthScreen title="Verify it's you">
      <p className="muted">Signing in as {username}. {method === "passkey" ? "Use a passkey to finish." : method === "totp" ? "Enter the code from your authenticator app." : "Enter one of your recovery codes. Each works once."}</p>
      {method === "passkey" ? (
        <div className="auth-form">
          {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
          <button className="button auth-wide" data-variant="primary" type="button" disabled={passkey.isPending} onClick={() => passkey.mutate(challenge.challenge)}>
            {passkey.isPending ? <span className="spinner" aria-hidden="true" /> : null}
            Use passkey
          </button>
        </div>
      ) : (
        <form className="auth-form" onSubmit={submit} noValidate>
          <div className="field">
            <label htmlFor="second-factor-code">{method === "totp" ? "Authentication code" : "Recovery code"}</label>
            <input id="second-factor-code" value={code} autoFocus autoComplete="one-time-code" inputMode={method === "totp" ? "numeric" : "text"}
                   maxLength={method === "totp" ? 8 : 24} onChange={event => setCode(event.target.value)} required />
          </div>
          {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
          <button className="button auth-wide" data-variant="primary" type="submit" disabled={verify.isPending || !code.trim()}>
            {verify.isPending ? <span className="spinner" aria-hidden="true" /> : null}
            Verify
          </button>
        </form>
      )}
      <div className="auth-form">
        {methods.filter(other => other !== method).map(other => (
          <button key={other} type="button" className="button auth-wide" data-variant="ghost" onClick={() => { setMethod(other); setCode(""); verify.reset(); passkey.reset(); }}>{METHOD_LABEL[other]}</button>
        ))}
        <button type="button" className="button auth-wide" data-variant="ghost" onClick={onBack}>Back to sign-in</button>
      </div>
    </AuthScreen>
  );
}
