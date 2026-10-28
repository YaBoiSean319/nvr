# Remote access: post-merge checks for each tunnel setup

The automated tests put the NVR behind a fixture reverse proxy (`tests/fixtures/reverse_proxy.py`) that behaves as nginx, Cloudflare Tunnel and `tailscale serve` are documented to behave towards an upstream. **None of the three real setups has been run.** Do each check you intend to rely on and record the result under "Results". Do not put keys, tokens, tunnel names or addresses in this file.

For every setup, after it is up, sign in as an administrator through the tunnel and open **Settings > Remote access**:

- [ ] "From" shows *your* public or tailnet address, not `127.0.0.1` or a Docker address, and "outside the local network".
- [ ] "Over" says HTTPS (Tailscale, Cloudflare) and the host you typed in the browser; "Through" says the peer is a trusted proxy.
- [ ] No "forwarded headers ignored" and no "plain HTTP" warning.
- [ ] Open Live view in a camera: video plays (the WebSocket works through the tunnel).
- [ ] From a page on another site (any HTML page you control, or the browser console on another origin: `new WebSocket("wss://<your host>/api/cameras/<id>/live/stream")` from `https://example.org`), the socket is refused.
- [ ] Turn on **Require a second factor for sessions from outside the local network**; with an operator account without a factor, signing in through the tunnel shows only "Set up a second factor", while signing in from the local network does not.
- [ ] Sign-in attempts are counted per caller: fail five times from one device and sign in from another device; the second is not held up.

## Tailscale (`compose.tailscale.yaml`)

1. In the Tailscale admin console, enable MagicDNS and HTTPS certificates, and make an auth key.
2. `TS_AUTHKEY=... TS_TAILNET=<your tailnet> docker compose -f compose.yaml -f compose.tailscale.yaml up -d`.
3. [ ] `docker compose logs tailscale` shows it joined the tailnet and is serving `https://nvr.<tailnet>.ts.net`.
4. [ ] From another device on the tailnet, `https://nvr.<tailnet>.ts.net` opens the sign-in page with a valid certificate.
5. [ ] `tailscale serve` adds `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host` (check "Over" and "From" in Settings). If your version does not, record that here: HTTPS will then show as plain HTTP.
6. [ ] A device that is not on the tailnet cannot reach the address at all.
7. [ ] Passkeys can be added (they need the HTTPS address).

## Cloudflare Tunnel (`compose.cloudflared.yaml`)

1. In Cloudflare Zero Trust, create a tunnel, add a public hostname pointing at `http://127.0.0.1:8280`, and add an Access policy that allows only your people.
2. `CLOUDFLARE_TUNNEL_TOKEN=... NVR_PUBLIC_HOST=<hostname> docker compose -f compose.yaml -f compose.cloudflared.yaml up -d`.
3. [ ] `docker compose logs cloudflared` shows the tunnel connected.
4. [ ] The hostname asks for Cloudflare Access sign-in first, then shows the NVR's sign-in page.
5. [ ] "From" shows the caller's address rather than a Cloudflare address (Cloudflare puts the caller in `X-Forwarded-For`; record what you see).
6. [ ] Live view video plays (WebSockets enabled on the hostname).
7. [ ] Direct access to the NVR's own address from outside the site is impossible (no port was opened).

## WireGuard (`compose.wireguard.yaml`)

1. Set up the hub and the site's `wg0.conf` as in the guide; on your phone or laptop, add the hub as a peer.
2. `docker compose -f compose.yaml -f compose.wireguard.yaml up -d`.
3. [ ] `docker compose exec wireguard wg show` shows a recent handshake with the hub.
4. [ ] From a device on the tunnel, `http://<NVR tunnel address>:8280` opens the sign-in page.
5. [ ] Settings > Remote access shows the caller as inside the local network (the tunnel subnet is listed in `NVR_LOCAL_NETWORKS`) and no plain-HTTP warning. Remove the subnet from `NVR_LOCAL_NETWORKS` and confirm the caller is then shown as outside, with the plain-HTTP warning.
6. [ ] With the tunnel down on the device, the NVR is unreachable from it.

## Results

| Setup | Date | Tool versions | Result |
| --- | --- | --- | --- |
| Tailscale | | | not run |
| Cloudflare Tunnel | | | not run |
| WireGuard | | | not run |
