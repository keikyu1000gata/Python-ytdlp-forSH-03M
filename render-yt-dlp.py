from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid

from flask import Flask, Response, jsonify, request, send_file, stream_with_context
import requests
import yt_dlp

app = Flask(__name__)

URL_STORE = {}
HLS_STORE = {}
INFO_CACHE = {}
SEARCH_CACHE = {}
CHAT_STATE = {}
DL_TASKS = {}
WEBM_TASKS = {}
WEBM_FILES = {}
LOCK = threading.Lock()
THREAD_LOCAL = threading.local()

PC_DL_DIR = os.path.join(os.path.expanduser("~"), "Desktop", "SH_Downloads")
WEBM_TMP_DIR = os.path.join(tempfile.gettempdir(), "sh_tube_webm_cache")
os.makedirs(WEBM_TMP_DIR, exist_ok=True)


def get_session():
    if not hasattr(THREAD_LOCAL, "session"):
        THREAD_LOCAL.session = requests.Session()
    return THREAD_LOCAL.session


def find_deno_opts():
    for p in [
        shutil.which("deno"),
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links\deno.exe"),
        os.path.expandvars(r"%USERPROFILE%\.deno\bin\deno.exe"),
    ]:
        if p and os.path.isfile(p):
            return {"deno": {"path": p}}
    return {"deno": {}}


def find_ffmpeg():
    p = shutil.which("ffmpeg")
    if p:
        return p
    winget_dir = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_dir):
        for root, _, files in os.walk(winget_dir):
            if "ffmpeg.exe" in files:
                return os.path.join(root, "ffmpeg.exe")
    return None


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def register_url(vid, mime, real_url, headers):
    uid = uuid.uuid4().hex[:12]
    now = time.time()
    with LOCK:
        if len(URL_STORE) > 800:
            for k in sorted(URL_STORE.keys(), key=lambda k: URL_STORE[k]["ts"])[:300]:
                URL_STORE.pop(k, None)
        URL_STORE[uid] = {
            "ts": now, "vid": vid, "mime": mime,
            "url": real_url, "headers": headers or {}
        }
    return "/p/" + uid


def register_hls(real_url, headers):
    uid = uuid.uuid4().hex[:12]
    now = time.time()
    with LOCK:
        if len(HLS_STORE) > 500:
            for k in sorted(HLS_STORE.keys(), key=lambda k: HLS_STORE[k]["ts"])[:200]:
                HLS_STORE.pop(k, None)
        HLS_STORE[uid] = {"ts": now, "url": real_url, "headers": headers or {}}
    return f"/hls/{uid}.m3u8"


def extract_info(video_id, clients=None):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "js_runtimes": find_deno_opts(),
        "remote_components": {"ejs:github"},
    }
    if clients:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": clients}}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False)


def probe_url(item):
    if item.get("is_hls"):
        return True
    sess = get_session()
    hdrs = dict(item["headers"])
    hdrs["Range"] = "bytes=0-16383"
    try:
        with sess.get(item["url"], headers=hdrs, stream=True, timeout=4) as r:
            if r.status_code in (200, 206):
                for ch in r.iter_content(chunk_size=1024):
                    if ch:
                        return True
                return True
    except Exception:
        pass
    return False


def is_safe_vp9(vc):
    # 10bit/HDR(vp09.02等)は除外し、通常の8bit VP9/VP8のみ許可
    if "vp09.02" in vc or "vp9.2" in vc or "vp09.03" in vc:
        return False
    return ("vp9" in vc or "vp09" in vc or "vp8" in vc)


def collect_candidates(info):
    v_cands, a_cands, hls_cands = [], [], []
    is_live = bool(info.get("is_live"))
    manifest = info.get("manifest_url") or ""

    for f in info.get("formats", []):
        u = f.get("url")
        if not u:
            continue
        proto = (f.get("protocol") or "").lower()
        ext = (f.get("ext") or "").lower()
        vc = (f.get("vcodec") or "none").lower()
        ac = (f.get("acodec") or "none").lower()
        hdrs = f.get("http_headers") or {}

        if "m3u8" in proto:
            if vc != "none":
                hls_cands.append({
                    "kind": "hls", "height": int(f.get("height") or 0),
                    "codec": vc, "mime": "application/vnd.apple.mpegurl",
                    "muxed": True, "url": u, "headers": hdrs, "is_hls": True
                })
            continue

        if ext in ("mhtml", "webp", "jpg") or vc == "images" or vc.startswith("av01"):
            continue

        if vc != "none":
            h = int(f.get("height") or 0)
            if h <= 0:
                continue
            is_vp9 = is_safe_vp9(vc)
            is_h264 = ("avc" in vc or "h264" in vc or ext == "mp4")
            if not is_vp9 and not is_h264:
                continue
            is_muxed = (ac != "none")
            mime = "video/webm" if (is_vp9 or ext == "webm") else "video/mp4"
            # ★VP9(WebM)を圧倒的に最優先スコアにする
            score = (2000 if is_vp9 else 200) + (500 if is_muxed else 0)
            v_cands.append({
                "kind": "video", "height": h, "codec": vc, "mime": mime,
                "muxed": is_muxed, "url": u, "headers": hdrs, "score": score
            })
        elif ac != "none":
            is_opus = ("opus" in ac or "vorbis" in ac or ext == "webm")
            mime = "audio/webm" if is_opus else "audio/mp4"
            note = (f.get("format_note") or "").lower()
            score = (800 if is_opus else 400) - (150 if "drc" in note else 0)
            a_cands.append({
                "kind": "audio", "height": 0, "codec": ac, "mime": mime,
                "abr": int((f.get("abr") or 64) * 1000), "url": u, "headers": hdrs, "score": score
            })

    by_key = {}
    for v in v_cands:
        ctype = "vp9" if is_safe_vp9(v["codec"]) else "avc"
        by_key.setdefault((v["height"], ctype), []).append(v)

    picked_v = []
    for grp in by_key.values():
        grp.sort(key=lambda x: x["score"], reverse=True)
        picked_v.append(grp[0])

    a_cands.sort(key=lambda x: x["score"], reverse=True)
    return picked_v, a_cands[:4], hls_cands, is_live, manifest


def build_payload(video_id):
    now = time.time()
    cached = INFO_CACHE.get(video_id)
    if cached and (now - cached[0] < 240):
        return cached[1]

    all_v, all_a, hls_list = [], [], []
    is_live, manifest = False, ""
    seen = set()

    # ★デフォルト(None)で大量のVP9(WebM)を拾い、足りなければmweb等を追加
    for clients in (None, ["mweb"], ["web_embedded"]):
        try:
            info = extract_info(video_id, clients)
            if info.get("is_live"):
                is_live = True
            pv, pa, phls, lv, mf = collect_candidates(info)
            if lv:
                is_live = True
            if mf and not manifest:
                manifest = mf
            if is_live and phls:
                hls_list.extend(phls)

            with ThreadPoolExecutor(max_workers=8) as ex:
                futs = {ex.submit(probe_url, it): it for it in (pv + pa)}
                for fut in as_completed(futs):
                    it = futs[fut]
                    try:
                        if not fut.result():
                            continue
                    except Exception:
                        continue
                    k = (it["kind"], it["height"], it["codec"], it.get("muxed"))
                    if k in seen:
                        continue
                    seen.add(k)
                    if it["kind"] == "video":
                        all_v.append(it)
                    else:
                        all_a.append(it)

            vp9_cnt = sum(1 for v in all_v if is_safe_vp9(v["codec"]))
            if is_live or (vp9_cnt >= 3 and len(all_a) >= 1):
                break
        except Exception as e:
            print(f"[EXTRACT WARN {clients}]", e)

    v_streams, a_streams, fmt_streams, adap_streams = [], [], [], []
    for v in all_v:
        pu = register_url(video_id, v["mime"], v["url"], v["headers"])
        obj = {
            "url": pu, "quality": f"{v['height']}p", "qualityLabel": f"{v['height']}p",
            "mimeType": v["mime"], "type": f"{v['mime']}; codecs=\"{v['codec']}\"",
            "codec": v["codec"], "videoOnly": (not v["muxed"])
        }
        v_streams.append(obj)
        if v["muxed"]:
            fmt_streams.append(obj)
        else:
            adap_streams.append(obj)

    for a in all_a:
        pu = register_url(video_id, a["mime"], a["url"], a["headers"])
        obj = {
            "url": pu, "mimeType": a["mime"], "type": f"{a['mime']}; codecs=\"{a['codec']}\"",
            "codec": a["codec"], "bitrate": a["abr"]
        }
        a_streams.append(obj)
        adap_streams.append(obj)

    hls_proxy = ""
    if is_live:
        for hv in hls_list:
            hp = register_hls(hv["url"], hv["headers"])
            v_streams.append({
                "url": hp, "quality": f"{hv['height']}p", "qualityLabel": f"{hv['height']}p",
                "mimeType": hv["mime"], "codec": hv["codec"], "videoOnly": False, "isHls": True
            })
        if manifest:
            hls_proxy = register_hls(manifest, {})

    payload = {
        "videoId": video_id, "isLive": is_live, "liveNow": is_live,
        "videoStreams": v_streams, "audioStreams": a_streams,
        "formatStreams": fmt_streams, "adaptiveFormats": adap_streams,
        "hls": hls_proxy, "hlsUrl": hls_proxy
    }
    if v_streams or a_streams or hls_proxy:
        INFO_CACHE[video_id] = (now, payload)
    vp9_n = sum(1 for v in all_v if is_safe_vp9(v["codec"]))
    print(f"[OK] {video_id} live={is_live} -> VP9:{vp9_n}本 / 計V:{len(v_streams)}本 / A:{len(a_streams)}本")
    return payload


@app.route("/api/v1/videos/<video_id>")
def invidious_video(video_id):
    try:
        return jsonify(build_payload(video_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/streams/<video_id>")
def piped_streams(video_id):
    try:
        return jsonify(build_payload(video_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 🔥 ごりおし WebM(VP9/VP8) 1本化エンジン (SH-03Mのvideo/avc制限を100%回避!)
@app.route("/prepare_webm/<video_id>")
def prepare_webm(video_id):
    h = min(max(int(request.args.get("h") or 360), 144), 1080)
    key = f"{video_id}_{h}"
    out_path = os.path.join(WEBM_TMP_DIR, f"{key}.webm")

    if os.path.isfile(out_path) and os.path.getsize(out_path) > 10000:
        WEBM_FILES[key] = out_path
        return jsonify({"ok": True, "ready": True, "url": f"/dl_webm/{key}", "note": f"キャッシュWebM({h}p)"})

    ffmpeg_path = find_ffmpeg()
    if not ffmpeg_path:
        return jsonify({"ok": False, "error": "ffmpeg未検出"})

    with LOCK:
        st = WEBM_TASKS.get(key)
        if st:
            if st["status"] == "done":
                return jsonify({"ok": True, "ready": True, "url": f"/dl_webm/{key}", "note": st["note"]})
            if st["status"] == "err":
                WEBM_TASKS.pop(key, None)
                return jsonify({"ok": False, "error": st["note"]})
            return jsonify({"ok": True, "ready": False, "note": st["note"]})
        WEBM_TASKS[key] = {"status": "running", "note": f"PC側で{h}p WebM(VP9)結合中..."}

    def _build_webm():
        try:
            files = [os.path.join(WEBM_TMP_DIR, f) for f in os.listdir(WEBM_TMP_DIR) if f.endswith(".webm")]
            if len(files) > 20:
                files.sort(key=os.path.getmtime)
                for old_f in files[:-15]:
                    os.remove(old_f)
        except Exception:
            pass

        # VP9 + Opus(WebM) があれば無劣化コピーで1秒合体!
        fmt = f"bestvideo[height<={h}][vcodec^=vp9][ext=webm]+bestaudio[ext=webm]/bestvideo[height<={h}][ext=webm]+bestaudio[ext=webm]/best[height<={h}]/best"
        ydl_opts = {
            "quiet": True, "format": fmt,
            "outtmpl": out_path.replace(".webm", ".%(ext)s"),
            "merge_output_format": "webm",
            "ffmpeg_location": ffmpeg_path,
            "js_runtimes": find_deno_opts(),
            "remote_components": {"ejs:github"},
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=True)

            raw_path = out_path
            if not os.path.isfile(raw_path):
                for ext in (".mp4", ".mkv"):
                    cand = out_path.replace(".webm", ext)
                    if os.path.isfile(cand):
                        raw_path = cand
                        break

            vc = (info.get("vcodec") or "").lower()
            # もしVP9以外(H.264/AV1等)が落ちてきた場合は超高速VP8(WebM)へ変換してSH-03Mで確実再生
            if raw_path != out_path or (not is_safe_vp9(vc)):
                WEBM_TASKS[key]["note"] = f"WebM(VP8)へ変換中({h}p)..."
                tmp_conv = out_path + ".conv.webm"
                subprocess.run([
                    ffmpeg_path, "-y", "-i", raw_path,
                    "-c:v", "libvpx", "-deadline", "realtime", "-cpu-used", "6",
                    "-b:v", "500k", "-vf", f"scale=-2:'min({h},ih)'",
                    "-c:a", "libvorbis", "-q:a", "3", tmp_conv
                ], check=True)
                if raw_path != out_path and os.path.isfile(raw_path):
                    os.remove(raw_path)
                os.replace(tmp_conv, out_path)

            WEBM_FILES[key] = out_path
            WEBM_TASKS[key] = {"status": "done", "note": f"1本化WebM準備OK ({h}p)"}
            print(f"[FORCE WEBM OK] {key}")
        except Exception as e:
            WEBM_TASKS[key] = {"status": "err", "note": str(e)}
            print("[FORCE WEBM ERR]", e)

    threading.Thread(target=_build_webm, daemon=True).start()
    return jsonify({"ok": True, "ready": False, "note": f"PC側で{h}p WebM生成開始..."})


@app.route("/dl_webm/<key>")
def serve_forced_webm(key):
    path = WEBM_FILES.get(key) or os.path.join(WEBM_TMP_DIR, f"{key}.webm")
    if not os.path.isfile(path):
        return "Not Found", 404
    return send_file(path, mimetype="video/webm", conditional=True)


@app.route("/api/v1/search")
def invidious_search():
    q = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit") or 15), 40)
    if not q:
        return jsonify([])
    ckey = f"{q}_{limit}"
    now = time.time()
    cached = SEARCH_CACHE.get(ckey)
    if cached and (now - cached[0] < 120):
        return jsonify(cached[1])
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}) as ydl:
            res = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
        out = []
        for e in res.get("entries", []) or []:
            vid = e.get("id")
            if not vid:
                continue
            out.append({
                "type": "video", "videoId": vid, "title": e.get("title") or "",
                "author": e.get("uploader") or e.get("channel") or "",
                "lengthSeconds": int(e.get("duration") or 0),
            })
        SEARCH_CACHE[ckey] = (now, out)
        return jsonify(out)
    except Exception:
        return jsonify([]), 500


@app.route("/api/v1/more_shorts")
def more_shorts():
    q = (request.args.get("q") or "面白い").strip()
    page = int(request.args.get("page") or 1)
    exclude = set((request.args.get("exclude") or "").split(","))
    queries = [f"{q} #shorts", f"{q} ショート動画", f"{q} 切り抜き #shorts", f"バズり #shorts {q}"]
    use_q = queries[(page - 1) % len(queries)]
    fetch_n = min(15 + page * 10, 45)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}) as ydl:
            res = ydl.extract_info(f"ytsearch{fetch_n}:{use_q}", download=False)
        out = []
        for e in res.get("entries", []) or []:
            vid = e.get("id")
            if not vid or vid in exclude:
                continue
            sec = int(e.get("duration") or 0)
            title = e.get("title") or ""
            if sec <= 180 or "#short" in title.lower():
                out.append({
                    "id": vid, "title": title, "ch": e.get("uploader") or e.get("channel") or "",
                    "sec": sec, "dur": f"{sec}s" if sec > 0 else "SHORT", "isShort": True
                })
                if len(out) >= 10:
                    break
        return jsonify({"ok": True, "items": out})
    except Exception as e:
        return jsonify({"ok": False, "items": [], "error": str(e)})


@app.route("/p/<uid>")
def proxy_media(uid):
    with LOCK:
        item = URL_STORE.get(uid)
    if not item:
        return "Expired", 404

    sess = get_session()
    hdrs = dict(item["headers"])

    if request.args.get("dl") == "1":
        r = sess.get(item["url"], headers=hdrs, stream=True, timeout=(10, 30))
        fname = re.sub(r'[\\/:*?"<>|]', "_", request.args.get("name") or f"video_{uid}.webm")
        rh = {"Content-Type": item["mime"], "Content-Disposition": f'attachment; filename="{fname}"'}
        if "Content-Length" in r.headers:
            rh["Content-Length"] = r.headers["Content-Length"]
        def gen():
            try:
                for ch in r.iter_content(chunk_size=65536):
                    if ch:
                        yield ch
            finally:
                r.close()
        return Response(stream_with_context(gen()), status=r.status_code, headers=rh)

    range_hdr = request.headers.get("Range")
    start = 0
    end = 1048575
    if range_hdr:
        m = re.match(r"bytes=(\d+)-(\d*)", range_hdr)
        if m:
            start = int(m.group(1))
            end = min(int(m.group(2)), start + 1048575) if m.group(2) else (start + 1048575)

    hdrs["Range"] = f"bytes={start}-{end}"
    r = sess.get(item["url"], headers=hdrs, timeout=(10, 20))
    if r.status_code not in (200, 206):
        return f"Upstream HTTP {r.status_code}", r.status_code

    data = r.content
    resp_headers = {"Content-Type": item["mime"], "Content-Length": str(len(data)), "Accept-Ranges": "bytes"}
    if "Content-Range" in r.headers:
        resp_headers["Content-Range"] = r.headers["Content-Range"]
    elif range_hdr:
        resp_headers["Content-Range"] = f"bytes {start}-{start + len(data) - 1}/*"

    return Response(data, status=(206 if (range_hdr or "Content-Range" in resp_headers) else 200), headers=resp_headers)


@app.route("/hls/<uid>.m3u8")
def proxy_hls(uid):
    with LOCK:
        item = HLS_STORE.get(uid)
    if not item:
        return "Expired", 404
    r = get_session().get(item["url"], headers=item["headers"], timeout=8)
    if r.status_code >= 400:
        return "HLS Error", r.status_code
    lines = []
    for line in r.text.splitlines():
        s = line.strip()
        if s and not s.startswith("#") and s.startswith("http"):
            lines.append(register_hls(s, item["headers"]) if ".m3u8" in s else register_url("live", "video/mp2t", s, item["headers"]))
        elif 'URI="http' in s:
            lines.append(re.sub(
                r'URI="(https?://[^"]+)"',
                lambda m: f'URI="{register_hls(m.group(1), item["headers"]) if ".m3u8" in m.group(1) else register_url("live", "video/mp4", m.group(1), item["headers"])}"',
                line
            ))
        else:
            lines.append(line)
    return Response("\n".join(lines), status=200, headers={"Content-Type": "application/vnd.apple.mpegurl"})


@app.route("/live_chat/<video_id>")
def get_live_chat(video_id):
    sess = get_session()
    hdrs = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0 Safari/537.36", "Accept-Language": "ja-JP,ja;q=0.9"}
    now = time.time()
    st = CHAT_STATE.get(video_id)
    cont = st["cont"] if (st and now - st["ts"] < 240) else None
    try:
        if not cont:
            r0 = sess.get(f"https://www.youtube.com/live_chat?v={video_id}&is_popout=1", headers=hdrs, timeout=6)
            m = re.search(r'"continuation"\s*:\s*"(0ofMyAO[^"]+|[^"]{30,})"', r0.text)
            if not m:
                return jsonify({"ok": False, "msgs": [], "note": "チャットなし"})
            cont = m.group(1)
        r1 = sess.post(
            "https://www.youtube.com/youtubei/v1/live_chat/get_live_chat?key=AIzaSyAO_FJ2SlqU8Q4STEHLGCilw_Y9_11qcW8",
            headers=hdrs, json={"context": {"client": {"clientName": "WEB", "clientVersion": "2.20241101.00.00"}}, "continuation": cont}, timeout=6,
        )
        data = r1.json()
        lcc = data.get("continuationContents", {}).get("liveChatContinuation", {})
        for c in lcc.get("continuations", []):
            for k in ("invalidationContinuationData", "timedContinuationData", "liveChatReplayContinuationData"):
                if k in c and "continuation" in c[k]:
                    CHAT_STATE[video_id] = {"cont": c[k]["continuation"], "ts": now}
                    break
        msgs = []
        for act in lcc.get("actions", []):
            item = act.get("addChatItemAction", {}).get("item", {})
            mr = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
            if not mr:
                continue
            author = mr.get("authorName", {}).get("simpleText", "匿名")
            txt = "".join([x.get("text", "") for x in mr.get("message", {}).get("runs", []) if "text" in x])
            amt = mr.get("purchaseAmountText", {}).get("simpleText", "")
            if txt or amt:
                msgs.append({"id": mr.get("id", ""), "author": author, "text": (f"[{amt}] " if amt else "") + txt})
        return jsonify({"ok": True, "msgs": msgs[-20:]})
    except Exception as e:
        CHAT_STATE.pop(video_id, None)
        return jsonify({"ok": False, "msgs": [], "note": str(e)})


@app.route("/pc_download/<video_id>")
def pc_download(video_id):
    mode = request.args.get("mode") or "best"
    os.makedirs(PC_DL_DIR, exist_ok=True)
    task_id = uuid.uuid4().hex[:8]
    DL_TASKS[task_id] = {"status": "running", "msg": "PC側でダウンロード開始..."}
    def _run():
        ff = find_ffmpeg()
        fmt = "bestaudio[ext=m4a]/bestaudio" if mode == "audio" else ("18/best[height<=360][ext=mp4]/best[ext=mp4]" if mode == "360p" else ("bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best" if ff else "best[ext=mp4]/best"))
        ydl_opts = {"quiet": True, "format": fmt, "outtmpl": os.path.join(PC_DL_DIR, "%(title).60s [%(id)s].%(ext)s"), "js_runtimes": find_deno_opts(), "remote_components": {"ejs:github"}}
        if ff:
            ydl_opts["ffmpeg_location"] = ff
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])
            DL_TASKS[task_id] = {"status": "done", "msg": f"保存完了! Desktop\\SH_Downloads ({mode})"}
        except Exception as e:
            DL_TASKS[task_id] = {"status": "err", "msg": f"失敗: {e}"}
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "taskId": task_id})


@app.route("/pc_download_status/<task_id>")
def pc_download_status(task_id):
    return jsonify(DL_TASKS.get(task_id, {"status": "unknown", "msg": "不明"}))


if __name__ == "__main__":
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.protocol_version = "HTTP/1.1"
    ip = get_local_ip()
    # Renderが指定するポート番号を取得（ローカルでは8080になる）
    port = int(os.environ.get("PORT", 8080))
    print("========================================")
    print(" SH-Tube Lite Server v8 (WebM/VP9特化版)")
    print(f" API URL: http://{ip}:{port}")
    print(f" ffmpeg : {find_ffmpeg() or '未検出'}")
    print("========================================")
    app.run(host="0.0.0.0", port=port, threaded=True)