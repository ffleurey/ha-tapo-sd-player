# Tapo SD player

Browse a Tapo camera's SD-card clips and play them, or the live view, from the
camera itself. Opens from the sidebar ("Tapo SD"). Nothing is stored in Home
Assistant.

## Requirements

- Tapo cameras recording to an SD card, reachable from Home Assistant on the LAN.
- The password of the TP-Link cloud account the cameras are registered to: the
  app logs in to each camera locally (user `admin`) with it, as
  [pytapo](https://github.com/JurajNyiri/pytapo) does. No cloud traffic.
- Some firmware only streams with the camera's **media encryption** on
  ([Supported](#supported)): with it off, the list works but clips and live
  fail, and the page says so. Set `turn_on_media_encryption` and the app
  switches it on the first time a stream fails for that reason (the camera's
  own setting, kept after reboots).

## Configuration

```yaml
cloud_password: "..."
live_max_minutes: 10
turn_on_media_encryption: false
cameras:
  - name: Garden
    host: 192.168.1.50
  - name: Garage
    host: 192.168.1.51
    cloud_password: "only if this camera is on another account"
```

Camera names are used in URLs: letters, digits, `-` and `_`.

## Use

- One tab per camera. The list starts with the last 24 h, newest first; the row
  at the bottom loads the 24 h before (days without clips are skipped).
- The date restarts the list from the end of that day; **Latest** goes back.
- **detections** hides clips without a detection; **audio** adds the sound.
- ⤓ downloads a clip as MP4.

## Supported

Tested cameras (list, clips with audio, live), October 2026:

| Model | Hardware | Firmware | Media encryption |
|---|---|---|---|
| C210 | 3.0 | 1.2.7 Build 260818 | works off |
| C310 | 1.0 | 1.3.16 Build 240909 | needs on |
| C410 (battery) | 2.0 | 1.3.5 Build 260430 | works off |
| C510W | 2.0 | 1.3.4 Build 260523 | works off |

Other models and firmware: untested, may work; reports welcome.
Installation: Home Assistant OS or Supervised, aarch64 or amd64.
Browsers: tested in Firefox on a desktop; other browsers, phones and the HA app
less tested.

## Limits

- A camera serves one media session at a time: two viewers on one camera, or the
  Tapo app at the same time, interrupt each other ("camera busy, try again").
- Battery cameras (C410) must be awake; live view stops after `live_max_minutes`.
- Some cameras (C410) do not report when their recordings start, so their list
  never says it reached the beginning.
- The camera API is undocumented: a firmware update may break the app.
- Port 8099 (Network tab) serves the player **without any login**: leave it off
  unless the LAN is trusted.
