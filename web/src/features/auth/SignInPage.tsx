import { useState, type FormEvent } from "react";
import { ApiError } from "@/api/client";
import { useOidcStatus, useSignIn } from "@/api/queries";
import { AuthScreen } from "./AuthScreen";

export function SignInPage() {
  const signIn = useSignIn();
  const oidc = useOidcStatus();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  function submit(event: FormEvent) {
    event.preventDefault();
    signIn.mutate({ username: username.trim(), password });
  }

  const error = signIn.error instanceof ApiError ? signIn.error.detail : signIn.error ? "Sign-in failed. Try again." : null;
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
          <input id="sign-in-username" autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} required autoFocus />
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
      </form>
    </AuthScreen>
  );
}
