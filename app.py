#!/usr/bin/env python3
# app.py — Spotify vote queue, Render-ready
#
# Notes:
# - This version pins playback and queue operations to the same Spotify device.
# - It logs queue/playback failures instead of swallowing them silently.
# - Run this app with a single worker/process on Render. It keeps queue/token/state
#   in memory, so multiple workers will drift out of sync.

from flask import (
    Flask,
    request,
    render_template_string,
    redirect,
    url_for,
    session,
    jsonify,
    has_request_context,
)
import os
import time
import threading
from collections import defaultdict

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

# ─── Config (env-driven for Render) ────────────────────────────────────────────
SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID", "").strip()
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET", "").strip()
SPOTIPY_REDIRECT_URI = os.getenv(
    "SPOTIPY_REDIRECT_URI", "http://127.0.0.1:5000/callback"
).strip()

SCOPES = "user-modify-playback-state user-read-playback-state"

QUEUE_AHEAD_SECONDS = int(os.getenv("QUEUE_AHEAD_SECONDS", "10"))
POLL_SECONDS = int(os.getenv("POLL_SECONDS", "3"))
AUTO_RESUME = os.getenv("AUTO_RESUME", "true").lower() == "true"
PAUSE_GRACE_SECONDS = int(os.getenv("PAUSE_GRACE_SECONDS", "6"))
START_COOLDOWN_SECONDS = float(os.getenv("START_COOLDOWN_SECONDS", "2"))

# ─── Flask ────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")

if os.getenv("RENDER", "0") == "1":
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PREFERRED_URL_SCHEME="https",
    )

# ─── Spotify clients ───────────────────────────────────────────────────────────
sp_search = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
    )
)

# ─── In-memory state ───────────────────────────────────────────────────────────
votes = defaultdict(int)
tracks = {}  # track_id -> dict(name, artist, image, preview_url)

TOKEN_INFO = None
TOKEN_LOCK = threading.Lock()

STATE_LOCK = threading.Lock()
CURRENT_URI = None
CURRENT_DURATION_SEC = None
QUEUED_NEXT_FOR_URI = None
AUTO_ENABLED = True

DEVICE_ID = None
DEVICE_NAME = None

COOLDOWN_UNTIL = 0.0
LAST_PAUSED_TS = None

BG_THREAD_STARTED = False
BG_THREAD_LOCK = threading.Lock()


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ─── OAuth helpers ─────────────────────────────────────────────────────────────
def _oauth():
    if not (SPOTIPY_CLIENT_ID and SPOTIPY_CLIENT_SECRET and SPOTIPY_REDIRECT_URI):
        raise RuntimeError(
            "Missing SPOTIPY_CLIENT_ID/SECRET or SPOTIPY_REDIRECT_URI"
        )
    return SpotifyOAuth(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
        redirect_uri=SPOTIPY_REDIRECT_URI,
        scope=SCOPES,
        cache_handler=None,
        show_dialog=False,
        open_browser=False,
    )


def _save_token_info(token_info):
    global TOKEN_INFO
    with TOKEN_LOCK:
        TOKEN_INFO = token_info
    session["token_info"] = token_info


def _get_token_info():
    global TOKEN_INFO
    with TOKEN_LOCK:
        ti = TOKEN_INFO

    if not ti and has_request_context():
        ti = session.get("token_info")
        if ti:
            with TOKEN_LOCK:
                TOKEN_INFO = ti

    if not ti:
        return None

    if ti.get("expires_at", 0) - int(time.time()) < 60:
        try:
            ti = _oauth().refresh_access_token(ti["refresh_token"])
            log("Token refreshed")
        except Exception as e:
            log(f"Token refresh failed: {e}")
            return None
        with TOKEN_LOCK:
            TOKEN_INFO = ti
        if has_request_context():
            session["token_info"] = ti

    return ti


def _user_sp():
    ti = _get_token_info()
    if not ti:
        return None
    return spotipy.Spotify(auth=ti["access_token"])


# ─── Devices ───────────────────────────────────────────────────────────────────
def _list_devices(sp_user):
    try:
        return sp_user.devices().get("devices", [])
    except spotipy.SpotifyException as e:
        log(f"devices() failed: {e}")
        return []


def _resolve_target_device(sp_user, prefer_active=False):
    """
    Return a device dict to target and refresh global DEVICE_ID/DEVICE_NAME.
    If prefer_active=True, prefer the currently active device first.
    """
    global DEVICE_ID, DEVICE_NAME

    devices = _list_devices(sp_user)
    if not devices:
        return None

    chosen = None

    if prefer_active:
        chosen = next((d for d in devices if d.get("is_active")), None)

    if not chosen and DEVICE_ID:
        chosen = next((d for d in devices if d.get("id") == DEVICE_ID), None)

    if not chosen:
        chosen = next((d for d in devices if d.get("is_active")), None)

    if not chosen:
        chosen = devices[0]

    DEVICE_ID = chosen.get("id")
    DEVICE_NAME = chosen.get("name")
    return chosen


def _get_active_device(sp_user):
    devices = _list_devices(sp_user)
    return next((d for d in devices if d.get("is_active")), None)


# ─── Tracks / queue helpers ────────────────────────────────────────────────────
def _track_dict_from_sp_item(t):
    images = t.get("album", {}).get("images", [])
    image = (
        images[1]["url"]
        if len(images) > 1
        else (images[0]["url"] if images else None)
    )
    return {
        "name": t["name"],
        "artist": t["artists"][0]["name"],
        "image": image,
        "preview_url": t.get("preview_url"),
    }


def _ensure_track_cached_by_tid(tid, sp_user=None):
    if tid in tracks:
        return
    try:
        sp = sp_user or _user_sp() or sp_search
        info = sp.track(tid)
        tracks[tid] = _track_dict_from_sp_item(info)
    except Exception as e:
        log(f"Failed to cache track {tid}: {e}")


def _ordered_ids(exclude_tid=None):
    ordered = sorted(votes.items(), key=lambda x: x[1], reverse=True)
    return [tid for tid, cnt in ordered if cnt > 0 and tid != exclude_tid]


def _candidate_next_uri(current_uri):
    exclude_tid = (
        current_uri.split(":")[-1]
        if current_uri and current_uri.startswith("spotify:track:")
        else None
    )
    ids = _ordered_ids(exclude_tid=exclude_tid)
    return f"spotify:track:{ids[0]}" if ids else None


def _progress_state(sp_user):
    """Return (current_uri, progress_sec, duration_sec, is_playing)."""
    try:
        pb = sp_user.current_playback()
    except spotipy.SpotifyException as e:
        log(f"current_playback() failed: {e}")
        return None, None, None, None

    if not pb or not pb.get("item"):
        return None, None, None, None

    uri = pb["item"].get("uri")
    prog_ms = pb.get("progress_ms") or 0
    dur_ms = pb["item"].get("duration_ms") or 0
    is_playing = bool(pb.get("is_playing"))
    return uri, prog_ms // 1000, dur_ms // 1000, is_playing


def _update_now_playing(sp_user):
    """
    Refresh CURRENT_URI/CURRENT_DURATION_SEC.
    Reset dedupe if the song changed, and start a short cooldown.
    Remove the newly playing track from the vote queue.
    """
    global CURRENT_URI, CURRENT_DURATION_SEC, QUEUED_NEXT_FOR_URI, COOLDOWN_UNTIL

    uri, prog_sec, dur_sec, is_playing = _progress_state(sp_user)
    now = time.time()
    changed = False

    with STATE_LOCK:
        if uri != CURRENT_URI:
            changed = True
            if uri:
                log(f"Now playing → {uri} ({prog_sec}/{dur_sec}s)")
            else:
                log("Now playing → nothing")
            CURRENT_URI = uri
            CURRENT_DURATION_SEC = dur_sec if uri else None
            QUEUED_NEXT_FOR_URI = None
            COOLDOWN_UNTIL = now + START_COOLDOWN_SECONDS

    if uri and uri.startswith("spotify:track:"):
        tid = uri.split(":")[-1]
        _ensure_track_cached_by_tid(tid, sp_user=sp_user)
        if changed:
            votes.pop(tid, None)

    return uri, prog_sec, dur_sec, is_playing


# ─── Background loop ───────────────────────────────────────────────────────────
def _background_loop():
    global QUEUED_NEXT_FOR_URI, LAST_PAUSED_TS, COOLDOWN_UNTIL

    log("Background loop started")

    while True:
        try:
            if not AUTO_ENABLED:
                time.sleep(POLL_SECONDS)
                continue

            sp_user = _user_sp()
            if not sp_user:
                time.sleep(POLL_SECONDS)
                continue

            device = _resolve_target_device(sp_user, prefer_active=False)
            if not device:
                log("No Spotify device available for queue control")
                time.sleep(POLL_SECONDS)
                continue

            uri, prog_sec, dur_sec, is_playing = _update_now_playing(sp_user)
            if not uri or dur_sec is None:
                time.sleep(POLL_SECONDS)
                continue

            now = time.time()
            in_cooldown = now < COOLDOWN_UNTIL

            if not is_playing:
                if LAST_PAUSED_TS is None:
                    LAST_PAUSED_TS = now
            else:
                LAST_PAUSED_TS = None

            if in_cooldown:
                time.sleep(POLL_SECONDS)
                continue

            if (
                AUTO_RESUME
                and not is_playing
                and LAST_PAUSED_TS
                and (now - LAST_PAUSED_TS) >= PAUSE_GRACE_SECONDS
            ):
                try:
                    sp_user.start_playback(device_id=DEVICE_ID)
                    log(f"Auto-resume after grace period on {DEVICE_NAME} ({DEVICE_ID})")
                    with STATE_LOCK:
                        COOLDOWN_UNTIL = time.time() + 1.0
                except spotipy.SpotifyException as e:
                    log(f"Auto-resume failed on {DEVICE_NAME} ({DEVICE_ID}): {e}")
                time.sleep(POLL_SECONDS)
                continue

            threshold = max(0, dur_sec - QUEUE_AHEAD_SECONDS)
            if prog_sec is not None and prog_sec >= threshold and is_playing:
                next_uri = _candidate_next_uri(uri)
                with STATE_LOCK:
                    already = (
                        QUEUED_NEXT_FOR_URI == next_uri and next_uri is not None
                    )

                if next_uri and not already:
                    try:
                        sp_user.add_to_queue(next_uri, device_id=DEVICE_ID)
                        log(
                            f"Queued next on {DEVICE_NAME} ({DEVICE_ID}): "
                            f"{next_uri} (prog={prog_sec}s / dur={dur_sec}s)"
                        )
                        with STATE_LOCK:
                            QUEUED_NEXT_FOR_URI = next_uri
                            COOLDOWN_UNTIL = time.time() + 1.0
                    except spotipy.SpotifyException as e:
                        log(
                            f"add_to_queue failed for {next_uri} on "
                            f"{DEVICE_NAME} ({DEVICE_ID}): {e}"
                        )

        except Exception as e:
            log(f"Background loop error: {e}")

        time.sleep(POLL_SECONDS)


def _start_background_thread_once():
    global BG_THREAD_STARTED

    with BG_THREAD_LOCK:
        if BG_THREAD_STARTED:
            return

        # Avoid starting twice under the Werkzeug reloader parent process.
        if os.getenv("WERKZEUG_RUN_MAIN") == "false":
            return

        threading.Thread(target=_background_loop, daemon=True).start()
        BG_THREAD_STARTED = True


# ─── Template ──────────────────────────────────────────────────────────────────
TEMPLATE = """
<!doctype html>
<html>
<head>
  <title>Spotify Vote Queue</title>
  <style>
    body { font-family: Arial, sans-serif; max-width: 900px; margin: 2rem auto; }
    ul, ol { padding-left: 1.25rem; }
    .row { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
    img.thumb { width:48px; height:48px; object-fit:cover; border-radius:6px; }
    .muted { color:#666; }
    .btn { padding:6px 10px; border:1px solid #ccc; border-radius:6px; background:#f7f7f7; cursor:pointer; text-decoration:none; color:black; }
    .btn:hover { background:#eee; }
    .actions form { display:inline; margin-left:6px; }
    .topbar { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
  </style>
</head>
<body>
  <div class="topbar">
    <h1>Spotify Vote Queue <span class="muted" style="font-size:0.8rem;">device-pinned build</span></h1>
    {% if authed %}
      <span class="muted">Logged in ✔</span>
      <a class="btn" href="{{ url_for('play_first') }}">▶️ Start with top track</a>
      <span class="muted">Next enqueued ~{{ ahead }}s before end • Live updates</span>
      {% if device_name %}
        <span class="muted">Target device: {{ device_name }}</span>
      {% endif %}
    {% else %}
      <a class="btn" href="{{ url_for('login') }}">🔐 Log in to Spotify</a>
    {% endif %}
    <form method="post" action="/clear"><button class="btn" type="submit">Clear votes</button></form>
  </div>

  <h2>Now Playing</h2>
  <div id="nowPlaying">
    {% if now_playing %}
    <div class="row">
      {% if now_playing.image %}<img class="thumb" src="{{ now_playing.image }}">{% endif %}
      <div>
        <div><strong>{{ now_playing.name }}</strong> — {{ now_playing.artist }}</div>
        <div class="muted">{{ now_playing.extra }}</div>
      </div>
    </div>
    {% else %}
      <p class="muted">Nothing playing.</p>
    {% endif %}
  </div>

  <h2>Search</h2>
  <form method="get" action="/">
    <input type="text" name="q" style="width:60%" placeholder="Enter song or artist" value="{{ query or '' }}">
    <button class="btn" type="submit">Search</button>
  </form>

  {% if results %}
    <h3>Results</h3>
    <ul>
    {% for track_id, t in results.items() %}
      <li class="row">
        {% if t.image %}<img class="thumb" src="{{ t.image }}">{% endif %}
        <div>
          <div><strong>{{ t.name }}</strong> — {{ t.artist }}</div>
          <div class="muted">
            <a href="https://open.spotify.com/track/{{ track_id }}" target="_blank">Open in Spotify Web</a>
            {% if t.preview_url %} • <a href="{{ t.preview_url }}" target="_blank">Preview (30s)</a>{% endif %}
          </div>
        </div>
        <div class="actions">
          <form method="post" action="/vote" style="display:inline;">
            <input type="hidden" name="track_id" value="{{ track_id }}">
            <button class="btn" type="submit">Upvote</button>
          </form>
          <form method="post" action="/downvote" style="display:inline;">
            <input type="hidden" name="track_id" value="{{ track_id }}">
            <button class="btn" type="submit">Downvote</button>
          </form>
        </div>
      </li>
    {% endfor %}
    </ul>
  {% endif %}

  <h2>Web Queue (by votes)</h2>
  <div id="queueList">
    {% if queue %}
      <ol>
      {% for track_id, count in queue %}
        <li class="row">
          {% if tracks[track_id].image %}<img class="thumb" src="{{ tracks[track_id].image }}">{% endif %}
          <div>
            <div><strong>{{ tracks[track_id].name }}</strong> — {{ tracks[track_id].artist }}</div>
            <div class="muted">{{ count }} votes</div>
          </div>
          <div class="actions">
            <form method="post" action="/vote" style="display:inline;">
              <input type="hidden" name="track_id" value="{{ track_id }}">
              <button class="btn" type="submit">Upvote</button>
            </form>
            <form method="post" action="/downvote" style="display:inline;">
              <input type="hidden" name="track_id" value="{{ track_id }}">
              <button class="btn" type="submit">Downvote</button>
            </form>
          </div>
        </li>
      {% endfor %}
      </ol>
    {% else %}
      <p class="muted">No votes yet. Search above and vote to build the queue.</p>
    {% endif %}
  </div>

  <script>
    async function refreshStatus() {
      try {
        const res = await fetch('/status.json', { cache: 'no-store' });
        if (!res.ok) return;
        const data = await res.json();

        const npDiv = document.getElementById('nowPlaying');
        if (data.now_playing) {
          const img = data.now_playing.image ?
            '<img class="thumb" src="' + data.now_playing.image + '">' : '';
          const extra = data.now_playing.extra || '';
          npDiv.innerHTML = `
            <div class="row">
              ${img}
              <div>
                <div><strong>${data.now_playing.name}</strong> — ${data.now_playing.artist}</div>
                <div class="muted">${extra}</div>
              </div>
            </div>
          `;
        } else {
          npDiv.innerHTML = '<p class="muted">Nothing playing.</p>';
        }

        const qDiv = document.getElementById('queueList');
        if (data.queue && data.queue.length) {
          const items = data.queue.map(item => {
            const img = item.track.image ?
              '<img class="thumb" src="' + item.track.image + '">' : '';
            return `
              <li class="row">
                ${img}
                <div>
                  <div><strong>${item.track.name}</strong> — ${item.track.artist}</div>
                  <div class="muted">${item.votes} votes</div>
                </div>
                <div class="actions">
                  <form method="post" action="/vote" style="display:inline;">
                    <input type="hidden" name="track_id" value="${item.track_id}">
                    <button class="btn" type="submit">Upvote</button>
                  </form>
                  <form method="post" action="/downvote" style="display:inline;">
                    <input type="hidden" name="track_id" value="${item.track_id}">
                    <button class="btn" type="submit">Downvote</button>
                  </form>
                </div>
              </li>
            `;
          }).join('');
          qDiv.innerHTML = `<ol>${items}</ol>`;
        } else {
          qDiv.innerHTML = '<p class="muted">No votes yet. Search above and vote to build the queue.</p>';
        }
      } catch (e) { /* silent */ }
    }
    setInterval(refreshStatus, 2000);
    document.addEventListener('DOMContentLoaded', refreshStatus);
  </script>
</body>
</html>
"""


# ─── Routes ────────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    _start_background_thread_once()

    query = request.args.get("q")
    results = {}

    if query:
        try:
            items = sp_search.search(q=query, type="track", limit=10)["tracks"]["items"]
            for t in items:
                tid = t["id"]
                if tid not in tracks:
                    tracks[tid] = _track_dict_from_sp_item(t)
                results[tid] = tracks[tid]
                votes[tid] = votes.get(tid, 0)
        except Exception as e:
            log(f"Search failed for query={query!r}: {e}")

    with STATE_LOCK:
        np_uri = CURRENT_URI
        dur = CURRENT_DURATION_SEC

    np_tid = (
        np_uri.split(":")[-1]
        if np_uri and np_uri.startswith("spotify:track:")
        else None
    )

    queue = [
        (tid, c)
        for tid, c in sorted(votes.items(), key=lambda x: x[1], reverse=True)
        if c > 0 and tid != np_tid
    ]

    now_playing = None
    if np_tid:
        _ensure_track_cached_by_tid(np_tid)
        meta = tracks.get(np_tid)
        if meta:
            now_playing = dict(meta)
            now_playing["extra"] = "Currently playing" + (f" • {dur}s" if dur else "")

    return render_template_string(
        TEMPLATE,
        results=results,
        queue=queue,
        tracks=tracks,
        query=query,
        authed=bool(_get_token_info()),
        ahead=QUEUE_AHEAD_SECONDS,
        now_playing=now_playing,
        device_name=DEVICE_NAME,
    )


@app.route("/vote", methods=["POST"])
def vote():
    tid = request.form.get("track_id")
    if tid:
        votes[tid] = votes.get(tid, 0) + 1
    return redirect(url_for("index"))


@app.route("/downvote", methods=["POST"])
def downvote():
    tid = request.form.get("track_id")
    if tid:
        votes[tid] = max(0, votes.get(tid, 0) - 1)
    return redirect(url_for("index"))


@app.route("/clear", methods=["POST"])
def clear():
    votes.clear()
    return redirect(url_for("index"))


@app.route("/login")
def login():
    return redirect(_oauth().get_authorize_url())


@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Missing code", 400

    token_info = _oauth().get_access_token(code=code, check_cache=False)
    _save_token_info(token_info)
    log("Logged in to Spotify")
    return redirect(url_for("index"))


@app.route("/play_first")
def play_first():
    global AUTO_ENABLED, COOLDOWN_UNTIL, QUEUED_NEXT_FOR_URI

    ids = _ordered_ids()
    if not ids:
        return redirect(url_for("index"))

    sp_user = _user_sp()
    if not sp_user:
        return redirect(url_for("login"))

    device = _resolve_target_device(sp_user, prefer_active=True)
    if not device:
        return (
            "No Spotify device found. Start playback on your phone or desktop first, "
            "then come back and press this again.",
            400,
        )

    top_id = ids[0]
    top_uri = f"spotify:track:{top_id}"

    try:
        # Start only on the chosen device. Do not rely on an implicit device later.
        sp_user.start_playback(device_id=DEVICE_ID, uris=[top_uri])
        log(f"Start with top track on {DEVICE_NAME} ({DEVICE_ID}) → {top_uri}")
    except spotipy.SpotifyException as e:
        return f"Failed to start on target device: {e}", 400

    votes.pop(top_id, None)
    time.sleep(0.7)
    _update_now_playing(sp_user)

    with STATE_LOCK:
        COOLDOWN_UNTIL = time.time() + START_COOLDOWN_SECONDS
        QUEUED_NEXT_FOR_URI = None

    AUTO_ENABLED = True
    return redirect(url_for("index"))


@app.route("/status.json")
def status_json():
    sp_user = _user_sp()
    if sp_user:
        try:
            _update_now_playing(sp_user)
        except Exception as e:
            log(f"status.json update_now_playing failed: {e}")

    with STATE_LOCK:
        uri = CURRENT_URI
        dur = CURRENT_DURATION_SEC

    np = None
    progress = None
    is_playing = None

    if sp_user and uri:
        try:
            _uri, progress, _dur, is_playing = _progress_state(sp_user)
            if _uri != uri:
                uri = _uri
                dur = _dur
        except Exception as e:
            log(f"status.json progress read failed: {e}")

    if uri and uri.startswith("spotify:track:"):
        tid = uri.split(":")[-1]
        _ensure_track_cached_by_tid(tid, sp_user=sp_user)
        meta = tracks.get(tid)
        if meta:
            extra_bits = []
            if dur is not None:
                extra_bits.append(f"{dur}s total")
            if progress is not None and dur is not None:
                rem = max(0, dur - progress)
                extra_bits.append(f"{rem}s left")
            if is_playing is False:
                extra_bits.append("paused")

            np = {
                "track_id": tid,
                "name": meta["name"],
                "artist": meta["artist"],
                "image": meta["image"],
                "extra": " • ".join(extra_bits) if extra_bits else None,
            }

    exclude_tid = (
        uri.split(":")[-1]
        if uri and uri.startswith("spotify:track:")
        else None
    )
    ids = _ordered_ids(exclude_tid=exclude_tid)

    queue_payload = []
    for tid in ids:
        _ensure_track_cached_by_tid(tid, sp_user=sp_user)
        meta = tracks.get(tid, {})
        queue_payload.append(
            {
                "track_id": tid,
                "votes": int(votes.get(tid, 0)),
                "track": {
                    "name": meta.get("name"),
                    "artist": meta.get("artist"),
                    "image": meta.get("image"),
                },
            }
        )

    return jsonify(
        {
            "version": "device-pinned",
            "authed": bool(_get_token_info()),
            "ahead_seconds": QUEUE_AHEAD_SECONDS,
            "now_playing": np,
            "queue": queue_payload,
            "device_id": DEVICE_ID,
            "device_name": DEVICE_NAME,
            "ts": int(time.time()),
        }
    )


# ─── Startup ───────────────────────────────────────────────────────────────────
_start_background_thread_once()

if __name__ == "__main__":
    host = "0.0.0.0" if os.getenv("RENDER", "0") == "1" else "127.0.0.1"
    port = int(os.getenv("PORT", "5000"))

    if os.getenv("WEB_CONCURRENCY", "1") != "1":
        log(
            "WARNING: WEB_CONCURRENCY is not 1. This app stores queue/playback "
            "state in memory and should run as a single worker."
        )

    app.run(host=host, port=port, debug=True)
