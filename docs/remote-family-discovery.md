# Remote family discovery

MS-CP-3 collects client and network evidence before Jellyfin receives a remote
entry point. Discovery does not enable remote login, expose a port, create an
account, install a client, or change a firewall. Keep the actual household
inventory outside Git and identify households and devices with neutral aliases
rather than people's names.

Use [`config/remote-family-inventory.example.toml`](../config/remote-family-inventory.example.toml)
as the private working template. Protect the state directory with mode `0700`
and the inventory with mode `0600`.

## Current server baseline

The initial read-only observation established that:

- Tailscale is healthy, has IPv4/UDP connectivity, and does not currently use
  Tailscale Serve for Jellyfin;
- Jellyfin has no HTTPS listener on port 443; the existing reverse proxy listens
  on HTTP port 80 only;
- Jellyfin and the request/Servarr host ports are bound on all host interfaces;
- the active LAN firewall profile permits the full high TCP and UDP port range,
  so explicit interface and port restrictions are required before remote
  publication;
- every configured non-administrator Jellyfin viewer still matches the intended
  LAN-only policy; and
- Gruff's sustained upload capacity, WAN address class, and inbound-port
  availability have not yet been measured.

These observations do not prove that a service is internet-reachable. No router
port forward or public listener should be created during discovery.

## Household inventory

Collect one record for every household that may receive access.

### Playback devices

For each television, streaming box, phone, tablet, or computer, record:

- brand and exact model code;
- television or streaming platform and OS version;
- wired, 5 GHz Wi-Fi, or 2.4 GHz Wi-Fi connectivity;
- whether its normal app store offers a maintained Jellyfin client;
- whether Tailscale can be installed and kept signed in;
- whether sideloading is technically possible and acceptable to the operator;
- maximum display resolution and HDR requirement; and
- known direct-play support for H.264, HEVC, AAC, AC-3, E-AC-3, and subtitle
  formats where the client exposes it.

Do not infer compatibility from the television brand alone. Model year, region,
OS version, and the attached streaming device can change the answer.

### Client availability checkpoint

Recheck the maintained Jellyfin client list when the inventory is collected.
Typical paths include the official Android TV/Google TV, Fire TV, Roku, webOS,
Android, and mobile clients, plus Swiftfin in the Apple ecosystem. Samsung
Tizen and other television platforms may lack a simple maintained app-store
path and can require a separate client, browser, attached streaming device, or
reviewed sideload. Cine Pelencho and Fladder remain optional evaluations and
must not gate secure family access.

### Household network

Record bounded measurements rather than credentials or public addresses:

1. Run three download/upload/latency tests at different times from the intended
   playback network.
2. Note whether the test device was wired, 5 GHz Wi-Fi, or 2.4 GHz Wi-Fi.
3. Record whether outbound Tailscale establishes a direct path or uses a relay
   during a test to Gruff.
4. If practical, compare the router's WAN address class with a public-IP check
   and record only `public`, `cgnat`, `private`, or `unknown`; never persist the
   address itself.
5. Note data caps, evening congestion, or ISP equipment that cannot be managed.

Remote-household CGNAT does not prevent ordinary outbound HTTPS. It can affect
VPN path quality. Gruff-side CGNAT or blocked inbound traffic matters if public
HTTPS is selected.

## Gruff upload measurement

Measure Gruff over its wired default route while bulk downloads are paused or
known to be idle. Capture at least three upload samples at different times and
use the lowest credible result for planning. Do not select a transcoding budget
from a single burst result.

Reserve capacity for interactive traffic and concurrent streams. The pilot
should start with one stream whose expected bitrate is comfortably below the
measured floor; hardware-transcode limits are validated later in MS-CP-5.

## Exposure decision

Prefer a private VPN when the actual playback device or household router can run
it reliably. Use individually invited Tailscale identities, least-privilege
ACLs, and only the Jellyfin destination port; do not share the operator's
identity or expose Servarr and administration surfaces.

Use hardened HTTPS only when the required playback client cannot use the
private network. That path requires a reviewed port-443 reverse proxy, a valid
certificate, WebSocket support, request limits, monitoring, and explicit router
and firewall rules. Keep Jellyfin's direct ports, request portals, Radarr,
Sonarr, Prowlarr, qBittorrent, and administrative endpoints LAN/VPN-only.

A household remains `undecided` until its normal television playback path,
client installation path, and network measurements are known. Mixed deployment
is allowed: VPN-capable households can remain private while another household
uses the reviewed HTTPS path.

## Completion gate

MS-CP-3 is complete only when:

- every intended household and primary playback device has an inventory record;
- Gruff has three credible upload measurements;
- every household has three network samples and a CGNAT classification or an
  explicit `unknown` with reason;
- client and Tailscale availability have been verified on the exact device;
- one exposure model is selected per household with its reason recorded; and
- one restricted account and device are nominated for the MS-CP-5 pilot.

Until then, non-administrator remote access remains disabled and MS-CP-4 makes
no exposure change.
