import { useState, type FormEvent } from "react";
import { ApiError } from "@/api/client";
import { useCreateAdministrator } from "@/api/queries";
import { AuthScreen } from "./AuthScreen";

const USERNAME = /^[a-zA-Z0-9_.@-]{3,128}$/;

function passwordProblem(password: string): string | null {
  if (password.length < 14) return "Use at least 14 characters.";
  if (!/[A-Za-z]/.test(password) || !/[0-9]/.test(password)) return "Include at least one letter and one number.";
  return null;
}

/** First run: create the emergency local administrator. There is no default account. */
export function SetupPage() {
  const create = useCreateAdministrator();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [touched, setTouched] = useState(false);

  const usernameError = touched && !USERNAME.test(username) ? "3–128 letters, numbers or . _ @ -" : null;
  const passwordError = touched ? passwordProblem(password) : null;
  const confirmError = touched && confirm !== password ? "The passwords do not match." : null;

  function submit(event: FormEvent) {
    event.preventDefault();
    setTouched(true);
    if (!USERNAME.test(username) || passwordProblem(password) || confirm !== password) return;
    create.mutate({ username, password });
  }

  const error = create.error instanceof ApiError ? create.error.detail : create.error ? "Setup failed. Try again." : null;
  return (
    <AuthScreen title="Create the administrator account">
      <p className="auth-lead">
        This one-time step creates the emergency local administrator. Do this on a trusted network; the setup page closes as soon as the account exists.
      </p>
      <form className="auth-form" onSubmit={submit} noValidate>
        <div className="field">
          <label htmlFor="setup-username">Username</label>
          <input id="setup-username" autoComplete="username" value={username} onChange={event => setUsername(event.target.value)} aria-invalid={!!usernameError} aria-describedby="setup-username-error" autoFocus />
          {usernameError ? <span id="setup-username-error" className="error">{usernameError}</span> : null}
        </div>
        <div className="field">
          <label htmlFor="setup-password">Password</label>
          <input id="setup-password" type="password" autoComplete="new-password" value={password} onChange={event => setPassword(event.target.value)} aria-invalid={!!passwordError} aria-describedby="setup-password-hint" />
          <span id="setup-password-hint" className={passwordError ? "error" : "hint"}>{passwordError ?? "At least 14 characters, including a letter and a number."}</span>
        </div>
        <div className="field">
          <label htmlFor="setup-confirm">Confirm password</label>
          <input id="setup-confirm" type="password" autoComplete="new-password" value={confirm} onChange={event => setConfirm(event.target.value)} aria-invalid={!!confirmError} aria-describedby="setup-confirm-error" />
          {confirmError ? <span id="setup-confirm-error" className="error">{confirmError}</span> : null}
        </div>
        {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
        <button className="button auth-wide" data-variant="primary" type="submit" disabled={create.isPending}>
          {create.isPending ? <span className="spinner" aria-hidden="true" /> : null}
          Create administrator
        </button>
      </form>
    </AuthScreen>
  );
}
