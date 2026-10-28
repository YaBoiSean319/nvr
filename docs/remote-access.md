# Remote access without inbound ports

The NVR has no relay of its own. To use it away from the building, run a tunnel or reverse proxy that you control and let the NVR know about it. Three setups are documented and ship as Compose overrides; each is listed as a post-merge check with its steps in [acceptance/remote-access.md](acceptance/remote-access.md), because none could be run against a real account when they were written.

| Setup | Opens a port at the site? | HTTPS | Compose file |
| --- | --- | --- | --- |
| **Tailscale** (`tailscale serve`) | No | Yes, a certificate for `<name>.<tailnet>.ts.net` | `compose.tailscale.yaml` |
| **Cloudflare Tunnel** | No | Yes, Cloudflare's | `compose.cloudflared.yaml` |
| **WireGuard** (the site dials out to a hub you run) | No (the hub needs one UDP port) | The tunnel is encrypted; the NVR itself is plain HTTP inside it | `compose.wireguard.yaml` |

Use `docker compose -f compose.yaml -f compose.<setup>.yaml up -d`. Every override sets the variables the NVR needs; the sections below say what they mean.

## Telling the NVR about the proxy

A proxy hides who is calling behind its own address and reports the caller in headers (`X-Forwarded-For`, `X-Forwarded-Proto`, `X-Forwarded-Host`), which anyone who can reach the NVR directly could send too. So the NVR believes those headers **only from a peer listed in `NVR_TRUSTED_PROXIES`** (addresses and networks, comma-separated; names are not accepted):

- **The caller's address** is the first entry of `X-Forwarded-For`, read from the right, that is not itself a trusted proxy. Entries a caller prepends are therefore never believed. Entries may carry a port (`1.2.3.4:5678`, `[::1]:80`); `::ffff:a.b.c.d` is read as `a.b.c.d`. If any entry is not an address, or there are more than ten, the header is unusable. Only the `X-Forwarded-*` headers are read, not the newer `Forwarded` header: make the proxy send `X-Forwarded-For`.
- **HTTPS** is the last `X-Forwarded-Proto`, and **the host** the last `X-Forwarded-Host` (when it is a plausible host name); otherwise the connection's own scheme and `Host`. Passkeys use them to know the address the browser used.
- From anyone else the headers are ignored, even on a deployment that has a trusted proxy. The NVR does not leave this to its web server: uvicorn's own forwarded-header handling, which trusts any sender on `127.0.0.1`, is turned off, and the NVR's own does the work (so redirects and links also use the scheme and host a trusted proxy reports). **If you ran the NVR behind a proxy on the same host before this, list `127.0.0.1` in `NVR_TRUSTED_PROXIES`**; until you do, its callers are treated as remote.
- `NVR_TRUST_FORWARDED_FOR=true`, from before trusted proxies existed, still counts sign-in attempts by the last `X-Forwarded-For` entry from any sender. It is only safe when the NVR can be reached through the proxy alone; Settings warns about it, and it is replaced by listing the proxy.

The proxies the overrides use run on the host network and reach the NVR on `127.0.0.1`, so they set `NVR_TRUSTED_PROXIES=127.0.0.1`. For another proxy, give its address or network (for example the Docker network of a Traefik or nginx container).

**Wherever the caller cannot be known, it counts as outside the local network, never as local:** forwarded headers from a peer that is not listed (a proxy the NVR was not told about), a trusted proxy that sends no usable `X-Forwarded-For` (missing, malformed or more than ten entries), and everything that rests on `NVR_TRUST_FORWARDED_FOR`, which any sender could write. This is what stops someone outside from looking local by sending a header of their own.

## Where "local" ends

A caller is *remote* when its address is outside the local networks: loopback, private (`10/8`, `172.16/12`, `192.168/16`), link-local and IPv6 unique-local. Add more with `NVR_LOCAL_NETWORKS`. **Tailscale's range (`100.64/10`) and a WireGuard tunnel's addresses are not local unless you list them**, so the second factor for remote sessions applies to people on a tailnet or tunnel; listing the range treats them as being at the site. An address that cannot be read counts as remote.

## The live video socket

Browsers must send an `Origin` when they open the live video WebSocket, and the NVR refuses it unless it names the host the request was made to (as a trusted proxy reports it, since proxies often rewrite `Host`) or an origin listed in `NVR_ALLOWED_ORIGINS` / `NVR_PUBLIC_URL`. Anything else is refused before the handshake completes (HTTP 403). Clients that send no `Origin` (not browsers) are unaffected: they still need a session.

## Settings > Remote access

Administrators see how the page reached the NVR (the caller's address and whether it is inside the local network, HTTPS or plain HTTP, the host, the peer and whether it is a trusted proxy, the trusted proxies, the public address, the session cookie) and these warnings, worst first:

- **Plain HTTP from outside the local network.** Passwords and video can be read on the way.
- **No second factor expected from remote sessions** while the NVR is exposed (reached from outside, behind a trusted proxy, or given an `NVR_PUBLIC_URL`).
- **Forwarded headers from a peer that is not a trusted proxy**, which were ignored: everybody behind it looks like one address, and HTTPS cannot be seen.
- `NVR_TRUST_FORWARDED_FOR` without trusted proxies; entries of `NVR_TRUSTED_PROXIES` that are not addresses; an `NVR_PUBLIC_URL` that is plain `http://`; the session cookie not marked Secure over HTTPS (set `NVR_COOKIE_SECURE=true`).

**Require a second factor for sessions from outside the local network** makes a local account without a passkey or authenticator app reach only enrolment when it signs in from outside, and asks a session started locally as soon as it is used from outside (it is checked on every request). Set up your own factor first. People who already have one are asked for it as always. It applies to browser sessions of local accounts: Microsoft Entra accounts (their tenant's multi-factor), API tokens and video-wall sessions are unchanged, as they are for the policy in Users & access. The change is audited (`remote_mfa_changed`).

## Environment

| Variable | Meaning |
| --- | --- |
| `NVR_TRUSTED_PROXIES` | Addresses or networks whose forwarded headers are believed |
| `NVR_LOCAL_NETWORKS` | Further networks counted as local |
| `NVR_ALLOWED_ORIGINS` | Further origins allowed to open the live video socket |
| `NVR_PUBLIC_URL` | The address people use (also allowed for the live socket, and used for passkeys and links) |
| `NVR_COOKIE_SECURE` | Mark the session cookie Secure (implied by an `https://` `NVR_PUBLIC_URL`) |

## Tailscale

`compose.tailscale.yaml` runs the Tailscale container beside the NVR on the host network and serves `https://<TS_HOSTNAME>.<TS_TAILNET>.ts.net` (default hostname `nvr`) to your tailnet, proxying to the NVR on `127.0.0.1:8280` (`deploy/tailscale-serve.json`). Create an auth key in the Tailscale admin console and enable HTTPS certificates and MagicDNS for the tailnet. Use ACLs to say who on the tailnet can reach the machine.

## Cloudflare Tunnel

`compose.cloudflared.yaml` runs `cloudflared` on the host network. Create the tunnel in the Cloudflare dashboard, give it a public hostname pointing at `http://127.0.0.1:8280`, put a Cloudflare Access policy in front of it, and pass the tunnel token and the hostname (`CLOUDFLARE_TUNNEL_TOKEN`, `NVR_PUBLIC_HOST`). WebSockets work by default. Cloudflare sees the traffic (it terminates TLS): that is the trade for needing no VPN client.

## WireGuard

The site dials out to a hub (a small host with a public address, which you run); your devices connect to the same hub. Nothing is opened at the site; the hub needs one UDP port. Put the site's client configuration (with `PersistentKeepalive = 25`, the NVR's tunnel address, and `AllowedIPs` covering your devices' tunnel addresses) at `deploy/wireguard/wg_confs/wg0.conf` and start `compose.wireguard.yaml`. Browse to `http://<NVR tunnel address>:8280`: the tunnel encrypts it, so there is no proxy to trust. By default people on the tunnel count as outside the local network, so Settings shows a plain-HTTP warning (which the tunnel makes harmless) and the second factor can be required of them; set `WG_LOCAL_NETWORKS` to the tunnel's addresses to treat them as local instead, which silences the warning and lifts that requirement. (Passkeys need an HTTPS address; use Tailscale or Cloudflare if you want them.) Keep the private keys out of the repository; `deploy/wireguard/` is for your own configuration.

## What the NVR does not do

It does not run or monitor the tunnel, does not open ports (UPnP), and sends nothing to a service of ours. A tunnel that is down means no remote access, and recording carries on.
