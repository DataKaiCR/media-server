# External media application plane

## Selected architecture

External relatives remain in their own Tailscale accounts. DataKai shares only
`home-ingress-01`, and policy permits an approved external identity to reach only
gateway TCP 443. The gateway is not a general proxy and receives no subnet,
DNS, exit-node, Funnel, SSH, or management role.

The accepted application target is separate from household Jellyfin:

```text
approved external Tailscale identity
  -> home-ingress-01:443
  -> dedicated Gruff external-media Traefik entrypoint
     -> watch.external.datakai.net
        -> Cine Pelencho browser assets
        -> jellyfin-external API
     -> requests.external.datakai.net
        -> jellyseerr-external
```

`jellyfin-external` and `jellyseerr-external` use independent configuration,
databases, caches, sessions, users, and watch/request state. The external
Jellyfin instance mounts only the existing Movies and TV directories read-only.
No media bytes are copied. Radarr, Sonarr, download clients, storage,
administration, household Jellyfin, and all other DataKai nodes remain outside
the external entrypoint.

## Why the household instance is not an external target

The first gateway pilot proved network isolation, trusted HTTPS, and the
Jellyfin route. It also proved that four visible passwordless household profiles
could authenticate through the reverse-proxy path. None was an administrator or
could delete/download/manage content, but they exposed additional libraries,
watch state, preferences, and transcoding under identities outside the intended
remote role. Hiding names alone would not remove authentication. The shared
household target therefore fails the application-isolation goal.

## External identity contract

- Every external person receives a unique password-protected Jellyfin account.
- External profiles are hidden from the public selector; clients use explicit
  username/password login.
- No external account is an administrator or receives deletion, download,
  public sharing, Live TV, shared-device control, or user-management rights.
- Only explicit Movies and TV library IDs are enabled.
- A private policy configuration remains the authority for exact identities;
  names, IDs, credentials, and tokens never enter Git or public evidence.
- Jellyseerr imports only approved external users. New-user auto-provisioning is
  disabled after bounded setup.
- Ordinary external Jellyseerr users receive request-only permission, no
  auto-approval, no advanced/4K selection, and optional movie/TV quotas.
- One operator identity may approve requests; external users cannot reach
  Radarr or Sonarr directly.

All external users share one isolated application pair. They may share catalog
metadata, but strong credentials and hidden profiles prevent account switching.
If future policy requires one family to be unable to infer any other external
identity or activity, that is a separate per-family-instance decision.

## Browser and API boundary

`https://watch.external.datakai.net/` serves Cine Pelencho rather than stock
Jellyfin Web. The same immutable, stateless Cine Pelencho container also serves
`https://watch.home.datakai.net/`; this is one maintained browser artifact, not
a second client deployment. Browser same-origin storage keeps household and
external sessions separate, while Traefik binds each origin's `/jellyfin` path
to its own Jellyfin instance. A capability loader may choose a modern or
compatibility browser bundle, but every bundle uses the account policy of the
backend selected by its origin. Installed clients remain separate artifacts and
may connect to the same backend.
No build embeds an account credential or access token.

The external Jellyfin API uses an explicit base path so static Pelencho routes
cannot collide with Jellyfin API, WebSocket, image, subtitle, or stream paths.
The stock Jellyfin web route is denied at the external entrypoint unless a
separately reviewed recovery requirement keeps it. Native/official-client API
compatibility is validated independently before any such client is promoted.

## DNS and TLS

The preferred names are:

- `watch.external.datakai.net`
- `requests.external.datakai.net`

They are distinct from household `*.home.datakai.net` names and identify the
external application plane consistently. DNS may publish a DNS-only answer to
the gateway's stable Tailscale address so recipients in other tailnets can
resolve it. The address remains non-publicly routable; Tailscale machine sharing
and exact grants remain the network authorization boundary. Traefik obtains
certificates through DNS-01 and terminates TLS on a dedicated external backend.
Public HTTP forwarding, Cloudflare proxying, Funnel, and direct Gruff exposure
remain prohibited.

The current Tailscale Serve `*.ts.net` route remains the rollback path until the
custom-name TLS flow passes. Switching TLS termination requires a reviewed raw
TCP relay or equivalent fixed gateway listener; do not assume a custom hostname
can reuse Tailscale Serve's `*.ts.net` certificate.

## Resource envelope

Current measurements put the existing Jellyfin/Jellyseerr pair near 700 MiB
combined memory while idle. Plan for roughly 700 MiB to 1 GiB steady state for
the external pair and reserve 2 GiB for scans and ordinary variation. Initial
container ceilings may be introduced only after scan, playback, and transcode
measurements; an illustrative ceiling is 2 GiB for Jellyfin plus 512 MiB for
Jellyseerr. Metadata/cache planning reserves 5 GiB plus bounded existing NVMe
transcode scratch. Gruff's local media, NVIDIA GPU, and available memory make it
the selected host; Ares is not the media-data or transcoding path.

## Delivery phases

1. Commit inert Compose definitions and isolation tests. Neither external
   service receives a host port or Traefik route during this phase.
2. Start `jellyfin-external` locally, complete the setup wizard through a
   loopback-only maintenance path, create a protected administrator credential,
   add only Movies and TV, and run the private policy audit.
3. Start `jellyseerr-external`, connect it to external Jellyfin and the existing
   request backends, import only approved users, disable new-user login, and
   prove pending-not-approved request behavior.
4. Produce and validate a hosted Cine Pelencho browser artifact with no embedded
   private state. Test API base-path, WebSocket, images, subtitles, direct play,
   transcode, seek, resume, and logout boundaries.
5. Add dedicated Traefik host routes and DNS-01 TLS, then change only the fixed
   gateway listener. Preserve the working `*.ts.net` path as rollback until the
   custom domains pass from the external client.
6. Re-share the gateway, validate exhaustive negative reachability and account
   isolation, test request approval, revoke and re-share, then enable gateway
   autostart only after lifecycle acceptance.

## Stop conditions

Stop and roll back the current phase if any external request can reach household
Jellyfin, stock Jellyfin Web without approval, another external account, an
unmounted library, an automatically approved request, Radarr/Sonarr directly,
an unrelated Traefik router, Gruff's ordinary HTTPS listener, or any DataKai
node/port outside the exact gateway path. Also stop for public routability,
credential disclosure, unbounded transcode/cache growth, or inability to restore
the last accepted `*.ts.net` route.
