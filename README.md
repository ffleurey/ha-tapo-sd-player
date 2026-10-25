# Tapo SD player for Home Assistant

A Home Assistant app that lists the clips on a Tapo camera's SD card, with their
detections (motion, person, line crossing), and plays them, or the live view,
straight from the camera in the HA sidebar. Nothing is copied to Home Assistant.

We wrote it for our own two houses: HA's Tapo integrations either cannot show
SD-card recordings or download each clip to HA before playing it. It does one
thing, for the cameras we have, and we share it as is: use it, copy the idea,
or take parts of it.

[![Add repository](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fffleurey%2Fha-tapo-sd-player)

Or Settings, Apps, App store, ⋮, Repositories, add
`https://github.com/ffleurey/ha-tapo-sd-player`, then install **Tapo SD player**.
Setup and limits: [DOCS.md](tapo_sd_player/DOCS.md).

## How it works

- `app.py` (aiohttp): logs in to each camera with [pytapo](https://github.com/JurajNyiri/pytapo),
  searches the SD card 24 h at a time (`searchVideoWithUTC`) and the detections
  (`searchDetectionList`), and streams a clip or the live view through a pytapo
  media session: MPEG-TS video and G.711 audio piped into ffmpeg, out as
  fragmented MP4 (video copied, audio to AAC).
- `index.html` (no framework): one tab per camera, newest clips first, "load the
  24 h before", a date to jump back, a `<video>` element; live through Media Source.
- Behind HA ingress, so HA's login and the HA phone app apply.

## Status

Tested on our cameras only: C210, C310, C410 and C510W, on HA OS (aarch64 and
amd64). The camera API is undocumented: a firmware update may break it.
Not affiliated with TP-Link. MIT licence.

## Notes for anyone building on it

- pytapo media sessions (`download` for a clip, `preview` for live) deliver
  MPEG-TS video with the G.711 audio as a separate payload: feed both to one
  ffmpeg, and never wait on (`drain()`) its pipes, since ffmpeg opens its inputs one
  after the other and would deadlock.
- The audio law differs per model (A-law 8 kHz on C310 and C510W, mu-law 16 kHz
  on C210 and C410): read `getAudioConfig()`, the wrong one clips badly.
- pytapo calls are blocking: run them in an executor, one at a time per camera.
- A camera serves one media session: cancel the previous stream before
  starting another, and retry briefly on its "busy" error codes.
- C210 and C510W reject a recording search spanning 25 h or more (`-71105`), so
  searching by date fails on the day clocks go back; search 24 h at a time.
- Detection `alarm_type`: 2 motion, 4 line crossing, 6 person.
- Live: a plain `<video src>` on the never-ending stream stutters in Firefox;
  Media Source with a small catch-up (play at 1.1x) stays about 1.5 s behind.
- Safari (WebKit) reports no buffered ranges on such a stream: wait on `readyState`.
- A sleeping battery camera (C410) can refuse a login; the next request logs in again.
