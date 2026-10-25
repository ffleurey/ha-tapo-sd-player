"""Tapo SD-card clip player: lists clips and streams them straight from the
camera (pytapo download session -> ffmpeg remux -> fragmented MP4), no disk."""
import asyncio, datetime, json, logging, os, time
from aiohttp import web
from pytapo import Tapo
from pytapo.media_stream._utils import StreamType

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("sdproxy")
# Same shape as an HA app's /data/options.json, see config.example.json
CONF = json.load(open(os.environ.get("CONFIG", "/data/options.json")))
ALARM = {2: "motion", 4: "line crossing", 6: "person"}  # 4 seen on a C310 with line crossing on
HERE = os.path.dirname(os.path.abspath(__file__))


class Cam:
    def __init__(self, name, host, password):
        self.name, self.host, self.pw = name, host, password
        self.tapo = None
        self.ctl = asyncio.Lock()  # pytapo control calls are sync, one at a time
        self.sd = asyncio.Lock()  # the camera serves one SD session at a time
        self.audio_rate, self.audio_fmt = 8000, "alaw"
        self.current = None  # task streaming from this camera
        self.lists = {}  # before or "now" -> (time, page)
        self.sd_start = None  # oldest recording on the SD card
        self.problem = None  # why the last stream failed, for the page

    async def call(self, fn, *args):
        async with self.ctl:
            loop = asyncio.get_running_loop()
            if self.tapo is None:
                t0 = time.time()
                self.tapo = await loop.run_in_executor(None, Tapo, self.host, "admin", self.pw, self.pw)
                await loop.run_in_executor(None, self.tapo.getTimeCorrection)
                try:
                    mic = (await loop.run_in_executor(None, self.tapo.getAudioConfig))["audio_config"]["microphone"]
                    # C510W: G711alaw 8 kHz, C210: G711ulaw 16 kHz; the wrong law clips badly
                    self.audio_rate = int(mic["sampling_rate"]) * 1000
                    self.audio_fmt = "mulaw" if "ulaw" in mic.get("encode_type", "") else "alaw"
                except Exception:
                    pass
                log.info("%s login %.1fs audio %s %d Hz", self.name, time.time() - t0, self.audio_fmt, self.audio_rate)
            try:
                return await loop.run_in_executor(None, fn, self.tapo, *args)
            except Exception:
                self.tapo = None  # force a fresh login next time
                raise


cams = {c["name"]: Cam(c["name"], c["host"], c.get("cloud_password") or CONF["cloud_password"])
        for c in CONF["cameras"]}


async def index(_):
    return web.FileResponse(os.path.join(HERE, "index.html"))


async def api_cams(_):
    return web.json_response(list(cams))


WINDOW = 86400  # one search per page: C210/C510W reject any search spanning 25 h or more (-71105)


async def api_clips(req):
    """Every clip starting in the 24 h before `before` (unix time, default now), newest
    first, and `next`, the cursor for the following page (null at the start of the SD card).
    One clip search and one detection search on the camera per page."""
    cam = cams[req.match_info["cam"]]
    t0 = time.time()
    before = int(req.query.get("before") or t0)
    # the newest page changes as the camera records: keep it 30 s; older pages 10 min
    key, ttl = ("now", 30) if "before" not in req.query else (before, 600)
    hit = cam.lists.get(key)
    if hit and t0 - hit[0] < ttl:
        return web.json_response(hit[1])

    def work(t):
        if cam.sd_start is None:
            try:
                cam.sd_start = min(int(h[k]["record_start_time"]) for h in t.getSDCard() for k in h)
            except Exception:
                cam.sd_start = 0
        start = max(before - WINDOW, cam.sd_start)
        # a clip running across `start` comes back in both pages: it belongs to the older one
        items = sorted((c for r in t.getRecordingsUTC(start, before - 1) for c in r.values()
                        if start <= c["startTime"] < before), key=lambda x: x["startTime"], reverse=True)
        events = (t.getEvents(start - 60, before + 60) or []) if items else []
        return items, events, (start if start > cam.sd_start else None)

    try:
        items, events, nxt = await cam.call(work)
    except Exception as e:
        log.warning("%s list failed: %r", cam.name, e)
        return web.json_response({"error": f"camera unreachable or asleep ({e})"}, status=502)
    out = []
    for it in items:
        s, e = it["startTime"], it["endTime"]
        kinds = sorted({ALARM.get(ev["alarm_type"], f"type {ev['alarm_type']}")
                        for ev in events if ev["start_time"] < e and ev["end_time"] > s})
        out.append({"start": s, "end": e, "events": kinds})
    res = {"clips": out, "next": nxt}
    cam.lists[key] = (t0, res)
    log.info("%s before %s: %d clips %.1fs", cam.name, key, len(out), time.time() - t0)
    return web.json_response(res)


ENCRYPTION_OFF = "Media encryption is off"  # pytapo's exception text
BUSY = (-52405, -52407, -52417, -52435)  # camera stream slot held by someone else (pytapo)


LIVE_MAX = 60 * CONF.get("live_max_minutes", 10)  # battery cameras: never leave live running


async def clip(req):
    return await serve(req, int(req.match_info["start"]), int(req.match_info["end"]))


async def live(req):
    return await serve(req, None, None)


async def serve(req, start, end):
    cam = cams[req.match_info["cam"]]
    audio = req.query.get("audio") == "1"
    # Last request wins: a viewer switching clips stops the previous stream on this
    # camera, and the lock is only released once that stream is fully closed.
    if cam.current is not None and not cam.current.cancelling():
        cam.current.cancel()
    try:
        await asyncio.wait_for(cam.sd.acquire(), 10)
    except asyncio.TimeoutError:
        return web.Response(status=503, text="previous stream did not stop")
    cam.current = asyncio.current_task()
    try:
        return await stream(req, cam, start, end, audio)
    finally:
        cam.current = None
        cam.sd.release()


async def api_problem(req):
    return web.json_response({"error": cams[req.match_info["cam"]].problem})


async def stream(req, cam, start, end, audio):
    resp = await stream_or_error(req, cam, start, end, audio)
    if resp.status >= 500:
        cam.problem = resp.text
    return resp


async def stream_or_error(req, cam, start, end, audio):
    cam.problem = None
    try:
        await cam.call(lambda t: t.getUserID())  # make sure we are logged in
    except Exception as e:
        return web.Response(status=502, text=f"Camera unreachable or asleep ({e}).")
    t0 = time.time()
    what = "live" if start is None else f"clip {end - start}s"
    log.info("%s %s requested", cam.name, what)
    for attempt in range(4):
        s = Stream(cam, start, end, audio)
        try:
            await s.open()
            first = await s.first_chunk()
        except asyncio.CancelledError:
            await asyncio.shield(s.close())
            log.info("%s %s stopped before the first data", cam.name, what)
            raise
        if first:
            break
        await s.close()
        if ENCRYPTION_OFF in s.status:
            # with encryption off some firmware (C310 1.3.16) keys the stream with a secret pytapo lacks
            if not CONF.get("turn_on_media_encryption"):
                return web.Response(status=502, text="This camera only streams with media encryption on: "
                                    "set turn_on_media_encryption in the app options (see Documentation).")
            await cam.call(lambda t: t.setMediaEncrypt(True))
            log.info("%s media encryption turned on", cam.name)
            continue
        if s.status != "busy":
            return web.Response(status=502, text=f"Camera sent no video ({s.status}).")
        log.info("%s busy, retry %d", cam.name, attempt + 1)
        await asyncio.sleep(0.5 * (attempt + 1))  # the camera frees a closed slot after a moment
    else:
        return web.Response(status=503, text="Camera busy (Tapo app or another viewer?), try again.")

    headers = {"Content-Type": "video/mp4", "Cache-Control": "no-store"}
    if start is not None and req.query.get("download") == "1":
        when = datetime.datetime.fromtimestamp(start).strftime("%Y-%m-%d_%H-%M-%S")
        headers["Content-Disposition"] = f'attachment; filename="{cam.name}_{when}.mp4"'
    resp = web.StreamResponse(headers=headers)
    sent, why = 0, "finished"
    try:
        await resp.prepare(req)
        chunk = first
        while chunk:
            await resp.write(chunk)
            sent += len(chunk)
            chunk = await asyncio.wait_for(s.ff.stdout.read(65536), 20)
            if start is None and time.time() - t0 > LIVE_MAX:
                why = "live time limit"
                break
    except asyncio.CancelledError:
        why = "stopped (client left or newer request)"
        raise
    except (ConnectionResetError, asyncio.TimeoutError) as e:
        why = f"stopped ({e!r})"
    finally:
        await asyncio.shield(s.close())
        log.info("%s %s %s: first data %.2fs, %.1fs, %.1f MB in, %.1f MB out, audio %s",
                 cam.name, what, why, s.first or 0, time.time() - t0, s.mb / 1e6, sent / 1e6, audio)
    return resp


class Stream:
    """One camera download session piped through one ffmpeg."""

    def __init__(self, cam, start, end, audio):
        self.cam, self.start, self.end, self.audio = cam, start, end, audio
        self.first, self.mb, self.status = None, 0, "running"
        self.ff = self.feeder = self.atr = None
        self.t0 = time.time()

    async def open(self):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", os.environ.get("FFLOG", "error"),
               "-probesize", "256k", "-analyzeduration", "500k", "-f", "mpegts", "-i", "pipe:0"]
        if self.audio:
            ar, aw = os.pipe()
            cmd += ["-probesize", "2048", "-analyzeduration", "0", "-f", self.cam.audio_fmt,
                    "-ar", str(self.cam.audio_rate), "-ac", "1", "-i", f"pipe:{ar}"]
        cmd += ["-map", "0:v", "-c:v", "copy"]
        if self.audio:
            cmd += ["-map", "1:a", "-c:a", "aac", "-ar", "48000", "-b:a", "64k"]  # 8/16 kHz AAC is not decoded everywhere
        # 0.5 s fragments instead of one per 2 s GOP: the browser gets data sooner and evenly
        cmd += ["-movflags", "frag_keyframe+empty_moov+default_base_moof", "-frag_duration", "500000", "-f", "mp4", "pipe:1"]
        self.ff = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            pass_fds=(ar,) if self.audio else ())
        if self.audio:
            os.close(ar)
            self.atr, _ = await asyncio.get_running_loop().connect_write_pipe(
                asyncio.Protocol, os.fdopen(aw, "wb", 0))
        self.feeder = asyncio.create_task(self.feed())

    async def feed(self):
        # camera -> ffmpeg; in download mode the camera stops by itself at end_time,
        # live ("preview", as pytapo's Streamer) runs until we close it
        tapo = self.cam.tapo
        if self.start is None:
            params = {"preview": {"audio": ["default"], "channels": [0], "resolutions": ["HD"]}}
            session = tapo.getMediaSession(StreamType.Stream)
            session.set_window_size(50)
        else:
            params = {"download": {
                "client_id": tapo.getUserID(), "channels": [0], "media_type": 0,
                "start_time": str(self.start), "end_time": str(self.end), "player_id": tapo.playerID}}
            session = tapo.getMediaSession(StreamType.Download)
            session.set_window_size(200)
        payload = json.dumps({"type": "request", "seq": 1, "params": {"method": "get", **params}})
        try:
            async with session:  # closes the camera connection on exit, cancel included
                async for r in session.transceive(payload):
                    if r.mimetype == "video/mp2t":
                        if self.first is None:
                            self.first = time.time() - self.t0
                        self.mb += len(r.plaintext)
                        self.ff.stdin.write(r.plaintext)
                        if self.audio and r.audioPayload:
                            self.atr.write(r.audioPayload)  # buffered, never blocks the video
                        # no drain: ffmpeg opens its inputs one after the other, a
                        # blocked video pipe would starve the audio one (RAM only)
                    elif r.mimetype == "application/json":
                        j = json.loads(r.plaintext.decode())
                        p = j.get("params") or {}
                        code = p.get("error_code")
                        if j.get("type") == "response" and code:
                            self.status = "busy" if code in BUSY else f"refused {code}"
                            log.warning("%s camera refused: %s", self.cam.name, p)
                            return
                        if p.get("event_type") == "stream_status" and p.get("status") == "finished":
                            break
            self.status = "finished"
        except asyncio.CancelledError:
            self.status = "cancelled"
            raise
        except Exception as e:
            self.status = f"error {e!r}"
        finally:
            self.ff.stdin.close()
            if self.atr:
                self.atr.close()

    async def first_chunk(self):
        """First ffmpeg output, or None if the camera refused or sent nothing."""
        read = asyncio.ensure_future(self.ff.stdout.read(65536))
        await asyncio.wait({read, self.feeder}, timeout=15, return_when=asyncio.FIRST_COMPLETED)
        if not read.done() and self.feeder.done() and self.first is not None:
            await asyncio.wait({read}, timeout=5)  # short clip: camera done, ffmpeg still muxing
        if read.done() and read.result():
            return read.result()
        read.cancel()
        if self.status == "running":
            self.status = "no data in 15 s"
        return None

    async def close(self):
        """Stop the camera session and ffmpeg, and wait until both are gone."""
        if self.feeder and not self.feeder.done():
            self.feeder.cancel()
        if self.feeder:
            await asyncio.gather(self.feeder, return_exceptions=True)
        if self.ff and self.ff.returncode is None:
            self.ff.kill()
            await self.ff.wait()


async def warm_up(app):
    # log in to every camera at start: a C210 login takes 6 to 7 s
    async def one(cam):
        try:
            await cam.call(lambda t: None)
        except Exception as e:
            log.info("%s not reachable at start: %r", cam.name, e)
    app["warm"] = [asyncio.create_task(one(c)) for c in cams.values()]


app = web.Application()
app.on_startup.append(warm_up)
app.add_routes([web.get("/", index), web.get("/api/cams", api_cams),
                web.get("/api/{cam}/clips", api_clips), web.get("/api/{cam}/problem", api_problem),
                web.get("/clip/{cam}/{start}/{end}.mp4", clip),
                web.get("/live/{cam}.mp4", live)])
# handler_cancellation: a viewer leaving stops its camera session at once
web.run_app(app, port=int(CONF.get("port", 8099)), handler_cancellation=True)
