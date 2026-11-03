import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";

interface SignIn { user_code: string; verification_uri: string; expires_at: string; interval: number }
interface EmailState {
  state: "none" | "pending" | "connected" | "error";
  tenant_id?: string; client_id?: string; sender?: string | null; account?: string | null; connected_at?: string | null;
  error?: { code: string; message: string; reconnect: boolean };
  sign_in?: SignIn;
  limits?: { per_rule_per_hour: number; per_hour: number };
}

const KEY = ["admin", "email"] as const;

/** Email alerts sent through Microsoft 365 with a Microsoft sign-in: give the tenant, then sign in at the link with the code shown. */
export function EmailSettings() {
  const client = useQueryClient();
  const state = useQuery({ queryKey: KEY, queryFn: ({ signal }) => api<EmailState>("/api/admin/email", { signal }) });
  const data = state.data;
  const [form, setForm] = useState({ tenant_id: "", client_id: "", sender: "" });
  const [loaded, setLoaded] = useState(false);
  const [to, setTo] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (data && !loaded) {
      setForm({ tenant_id: data.tenant_id ?? "", client_id: data.client_id ?? "", sender: data.sender ?? "" });
      setLoaded(true);
    }
  }, [data, loaded]);

  const refresh = () => client.invalidateQueries({ queryKey: KEY });
  const connect = useMutation({
    mutationFn: () => api<{ state: string; sign_in: SignIn }>("/api/admin/email/connect", { method: "POST", body: { tenant_id: form.tenant_id, client_id: form.client_id, sender: form.sender.trim() || null } }),
    onSuccess: async () => { setError(null); setMessage(null); await refresh(); },
    onError: problem => { setMessage(null); setError(failureText(problem, "The sign-in could not be started.")); },
  });
  const poll = useMutation({
    mutationFn: () => api<EmailState>("/api/admin/email/poll", { method: "POST" }),
    onSuccess: async result => {
      if (result.state === "connected") setMessage(`Connected as ${result.account ?? "the signed-in account"}.`);
      await refresh();
    },
  });
  const test = useMutation({
    mutationFn: () => api("/api/admin/email/test", { method: "POST", body: { to } }),
    onSuccess: async () => { setError(null); setMessage(`A test email was sent to ${to}.`); await refresh(); },
    onError: async problem => { setMessage(null); setError(failureText(problem, "The test email could not be sent.")); await refresh(); },
  });
  const disconnect = useMutation({
    mutationFn: () => api("/api/admin/email", { method: "DELETE" }),
    onSuccess: async () => { setError(null); setMessage("Disconnected."); setLoaded(false); await refresh(); },
    onError: problem => setError(failureText(problem, "Could not disconnect.")),
  });

  // While the sign-in is open, ask whether it has been completed, at the rate Microsoft asks for.
  const pending = data?.state === "pending" ? data.sign_in : undefined;
  useEffect(() => {
    if (!pending) return;
    const timer = window.setInterval(() => poll.mutate(), Math.max(pending.interval, 2) * 1000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pending?.user_code, pending?.interval]);

  const connected = data?.state === "connected";
  const ready = form.tenant_id.trim() !== "" && form.client_id.trim() !== "";
  return (
    <>
      {message ? <p className="notice" data-tone="good" role="status">{message}</p> : null}
      {error ? <p className="notice" data-tone="bad" role="alert">{error}</p> : null}
      <section className="field-group settings-block" aria-label="Microsoft 365 connection">
        <h2 className="field-heading">Microsoft 365</h2>
        <p className="muted">Alert emails are sent through Microsoft Graph with a Microsoft sign-in, not a mail password. The only permission used is <strong>Mail.Send</strong>. Tokens are stored encrypted and never shown again.</p>
        {data?.state === "error" && data.error ? <p className="notice" data-tone="bad" role="alert">{data.error.message}</p> : null}
        {connected ? (
          <p role="status">Connected as <strong>{data?.account ?? "the signed-in account"}</strong>{data?.sender && data.sender !== data.account ? <>, sending as <strong>{data.sender}</strong></> : null}.</p>
        ) : null}
        {pending ? (
          <div className="notice" data-tone="info" role="status" aria-label="Sign in with Microsoft">
            <p>Open <a href={pending.verification_uri} target="_blank" rel="noopener noreferrer">{pending.verification_uri}</a> and enter this code:</p>
            <p><strong className="device-code" style={{ fontSize: "1.6rem", letterSpacing: "0.1em" }}>{pending.user_code}</strong></p>
            <p className="muted">Sign in with the account that should send the alerts. This page continues by itself once you have. The code works until {new Date(pending.expires_at).toLocaleTimeString()}.</p>
          </div>
        ) : null}
        <label className="field"><span>Tenant ID or domain</span>
          <input value={form.tenant_id} placeholder="00000000-0000-0000-0000-000000000000 or contoso.onmicrosoft.com" disabled={Boolean(pending)} onChange={event => setForm({ ...form, tenant_id: event.target.value })} />
        </label>
        <label className="field"><span>Application (client) ID</span>
          <input value={form.client_id} placeholder="From your app registration" disabled={Boolean(pending)} onChange={event => setForm({ ...form, client_id: event.target.value })} />
          <small className="muted field-hint">In Microsoft Entra, register an app, add the delegated permission Microsoft Graph &gt; Mail.Send, and turn on &ldquo;Allow public client flows&rdquo; (Authentication). No secret is needed.</small>
        </label>
        <label className="field"><span>Send as (optional)</span>
          <input value={form.sender} placeholder="A shared mailbox the account may send as" disabled={Boolean(pending)} onChange={event => setForm({ ...form, sender: event.target.value })} />
        </label>
        <div className="field-actions">
          <button type="button" className="button" disabled={!ready || connect.isPending || Boolean(pending)} onClick={() => connect.mutate()}>
            {connected || data?.state === "error" ? "Sign in again" : "Connect with Microsoft"}
          </button>
          {data && data.state !== "none" ? <button type="button" className="button" onClick={() => disconnect.mutate()} disabled={disconnect.isPending}>{pending ? "Cancel" : "Disconnect"}</button> : null}
        </div>
      </section>
      {connected ? (
        <section className="field-group settings-block" aria-label="Send a test email">
          <h2 className="field-heading">Send a test</h2>
          <label className="field"><span>Send a test email to</span>
            <input type="email" value={to} placeholder="you@example.com" onChange={event => setTo(event.target.value)} />
          </label>
          <div className="field-actions">
            <button type="button" className="button" disabled={!to.trim() || test.isPending} onClick={() => test.mutate()}>Send test email</button>
          </div>
          <p className="muted">To email alerts, add the action &ldquo;Email (Microsoft 365)&rdquo; to a rule. To keep one busy rule from flooding inboxes, at most {data?.limits?.per_rule_per_hour ?? 20} emails an hour are sent per rule and {data?.limits?.per_hour ?? 100} in all; further alerts still appear in the alarm inbox and are marked failed in the rule&rsquo;s delivery log.</p>
        </section>
      ) : null}
    </>
  );
}
