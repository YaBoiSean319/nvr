import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/api/client";
import { failureText } from "@/features/admin/tabDraft";

type Policy = "off" | "administrators" | "all";
interface RemoteWarning { severity: "bad" | "warn"; code: string; message: string }
interface RemoteAccess {
  client_address: string;
  peer_address: string;
  peer_is_trusted_proxy: boolean;
  remote: boolean;
  https: boolean;
  host: string;
  trusted_proxies: string[];
  local_networks: string[];
  allowed_origins: string[];
  public_url: string | null;
  legacy_forwarded_trust: boolean;
  cookie_secure: boolean;
  mfa_policy: Policy;
  remote_mfa: boolean;
  warnings: RemoteWarning[];
}

const KEY = ["admin", "remote-access"] as const;

/**
 * How this page reached the NVR (address, HTTPS, proxy) and what is unsafe about it, with the option to
 * require a second factor of sessions from outside the local network. Reached through a tunnel or proxy
 * you run; the NVR has no relay of its own.
 */
export function RemoteAccessSettings() {
  const client = useQueryClient();
  const shown = useQuery({ queryKey: KEY, queryFn: ({ signal }) => api<RemoteAccess>("/api/admin/remote-access", { signal }) });
  const change = useMutation({
    mutationFn: (value: { mfa_policy: Policy; remote_mfa: boolean }) => api("/api/admin/security", { method: "PUT", body: value }),
    onSuccess: () => client.invalidateQueries({ queryKey: KEY }),
  });
  const data = shown.data;
  if (!data) return <p className="muted" role="status">{shown.isError ? "How the NVR is reached could not be loaded." : "Loading…"}</p>;
  return (
    <>
      <section className="field-group" aria-label="How you are connected">
        <h2 className="field-heading">How you are connected</h2>
        <dl className="detail-list">
          <dt>From</dt>
          <dd>{data.client_address} · {data.remote ? "outside the local network" : "the local network"}</dd>
          <dt>Over</dt>
          <dd>{data.https ? "HTTPS" : "Plain HTTP"}{data.host ? ` to ${data.host}` : ""}</dd>
          <dt>Through</dt>
          <dd>
            {data.peer_address}
            {data.peer_is_trusted_proxy ? " (a trusted proxy: the address and HTTPS above are what it reported)" : " (connected directly, or a proxy that is not trusted: its forwarded headers are ignored)"}
          </dd>
          <dt>Trusted proxies</dt>
          <dd>{data.trusted_proxies.length ? data.trusted_proxies.join(", ") : "None. Set NVR_TRUSTED_PROXIES to the address of your tunnel or reverse proxy."}</dd>
          <dt>Public address</dt>
          <dd>{data.public_url ?? "Not set (NVR_PUBLIC_URL)"}</dd>
          <dt>Session cookie</dt>
          <dd>{data.cookie_secure ? "Marked Secure (sent only over HTTPS)" : "Not marked Secure"}</dd>
        </dl>
      </section>

      <section className="field-group" aria-label="Warnings">
        <h2 className="field-heading">Warnings</h2>
        {data.warnings.length ? (
          <ul className="plain-list" aria-label="Remote access warnings">
            {data.warnings.map(warning => (
              <li key={warning.code}>
                <p className="notice" role="note" data-tone={warning.severity === "bad" ? "bad" : "warn"} data-code={warning.code}>{warning.message}</p>
              </li>
            ))}
          </ul>
        ) : <p className="notice" role="status" data-tone="good">Nothing to warn about for the way you are connected now.</p>}
      </section>

      <section className="field-group" aria-label="Remote sessions">
        <h2 className="field-heading">Remote sessions</h2>
        <label className="check">
          <input type="checkbox" checked={data.remote_mfa} disabled={change.isPending}
                 onChange={event => change.mutate({ mfa_policy: data.mfa_policy, remote_mfa: event.target.checked })} />
          Require a second factor for sessions from outside the local network
        </label>
        <p className="muted field-hint">
          Someone who signs in from outside the local network (as seen through your trusted proxy) without a passkey or authenticator app can only set one up until they have one;
          a session started on the local network is asked as soon as it is used from outside. Set up your own second factor first. Microsoft Entra accounts and API tokens are unchanged.
          Where the local network ends is set with NVR_LOCAL_NETWORKS.
        </p>
        {change.isError ? <p className="notice" role="alert" data-tone="bad">{failureText(change.error)}</p> : null}
      </section>
    </>
  );
}
