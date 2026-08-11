# Jellyfin Customization

Jellyfin can be customized without the Fanart plugin. The built-in TMDb and
OMDb providers, local artwork, and images already stored for library items are
enough for branding, CSS themes, and a featured-media interface. Fanart.tv is
an optional additional image source, not a prerequisite for any visual layer.

## Safety boundary

Treat server plugins and web-interface transformations as executable code.
Back up the stopped Jellyfin configuration before an upgrade or plugin change,
match every plugin build to the exact Jellyfin ABI, and introduce one component
at a time. Verify the web interface, authenticated API, direct play, hardware
transcoding, and the actual household clients after every restart.

Customizations that patch or transform Jellyfin Web generally affect only
clients built on Jellyfin Web. Native clients can retain their own interface.
Never assume that a dashboard change will appear on Android TV, Swiftfin,
Findroid, Kodi, or another native client.

## Plugin-free options

### Built-in branding

Dashboard **Branding** supports a splash screen and Custom CSS. The current
Compose deployment persists these settings in `/config`, so container
replacement does not discard them.

Custom CSS is the lowest-risk way to change colors, spacing, card dimensions,
backdrops, typography, and page density. Jellyfin documents that CSS applies
only to Jellyfin Web clients. External CSS, fonts, and images are fetched by
each client; prefer reviewed, version-pinned assets served locally instead of a
mutable remote `@import`.

### Latest additions

Jellyfin's built-in **Latest Media** home section provides a plugin-free recent
movies row ordered by library addition time. Placing Latest Media immediately
after My Media makes newly imported movies easy to find while preserving each
viewer's library allowlist. It reflects items accepted into Jellyfin, not failed
or rejected downloader activity; use Radarr's activity/history when import
provenance matters.

Home-section order is a per-user, per-client preference. Configure the built-in
`emby` preference used by Jellyfin Web and web-derived television clients, take
a private preference snapshot first, and verify each effective library view
afterward. Native clients may retain their own home layout. The separate
**hide played items from latest media** preference should not be silently
changed when reordering sections.

### Existing and local artwork

Jellyfin ships with TMDb, OMDb, and local metadata support. Curated local
posters, backdrops, logos, and thumbnails can be stored beside media using
Jellyfin's recognized naming conventions. This gives deterministic presentation
without a Fanart.tv account or another metadata provider.

Local media artwork remains content state, not application configuration. Back
it up before replacing it, and do not let a broad metadata refresh overwrite
curated images unintentionally.

## Optional visual plugins

### Media Bar

Media Bar adds a featured-content hero to Jellyfin Web and can use artwork
already exposed by Jellyfin. It does not require the Fanart plugin. The
IAmParadox27 wrapper depends on File Transformation and incorporates frontend
content from MakD's Jellyfin Media Bar project.

Both dependencies are version-sensitive and alter the web delivery path. Pin a
release built for the deployed Jellyfin version, preserve a rollback backup,
and test login, navigation, mobile sizing, playback launch, and container
restart before keeping it.

### Home Screen Sections

Home Screen Sections provides server-selected rows such as discovery and
recommendation sections. It also depends on File Transformation and primarily
benefits Jellyfin Web. Install it separately from Media Bar so regressions can
be attributed and rolled back cleanly.

### Intro Skipper

Intro Skipper adds functional rather than purely visual value. Detection uses
media analysis and should first be scheduled against a small series during an
idle window. Current web controls can involve File Transformation; verify the
plugin documentation for the deployed Jellyfin release before installation.

## Options to avoid by default

- **Media Cleaner:** automated deletion can conflict with Radarr, Sonarr,
  seeding, retention, and backup policy.
- **JavaScript Injector:** arbitrary JavaScript executes in authenticated web
  clients and can access session context. Use it only for a specific,
  locally-reviewed script.
- **Unpinned remote themes:** an upstream change can modify every web client
  without a server deployment or review.
- **Large plugin bundles:** simultaneous installation obscures the cause of
  startup, migration, and interface failures.

## Recommended progression

1. Keep the built-in metadata providers and current local artwork.
2. Put built-in Latest Media near the top of the home screen and verify that
   each role sees only allowed libraries.
3. Define the desired visual direction, then apply a small local Custom CSS
   baseline and verify representative web clients.
4. Add File Transformation and Media Bar as one backed-up change if a dynamic
   hero is desired.
5. Evaluate Intro Skipper on one series.
6. Add Home Screen Sections only if the primary clients can display it and its
   recommendation rows provide clear value.

This sequence provides substantial customization without Fanart.tv and keeps
each rollback bounded.
