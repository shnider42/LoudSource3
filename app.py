#!/usr/bin/env python3
from flask import (
    Flask,
    request,
    render_template_string,
    redirect,
    url_for,
    jsonify,
)
import os
import time
import re
import threading
import requests

import spotipy
from spotipy.oauth2 import SpotifyClientCredentials, SpotifyOAuth

import storage


# ─── Config ────────────────────────────────────────────────────────────────────
DEBUG_VERBOSE = os.getenv("DEBUG_VERBOSE", "true").lower() == "true"

SPOTIPY_CLIENT_ID = os.getenv("SPOTIPY_CLIENT_ID", "").strip()
SPOTIPY_CLIENT_SECRET = os.getenv("SPOTIPY_CLIENT_SECRET", "").strip()
SPOTIPY_REDIRECT_URI = os.getenv(
    "SPOTIPY_REDIRECT_URI", "http://127.0.0.1:5000/callback"
).strip()

SCOPES = (
    "user-modify-playback-state "
    "user-read-playback-state "
    "user-read-currently-playing"
)

QUEUE_AHEAD_SECONDS = int(os.getenv("QUEUE_AHEAD_SECONDS", "10"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "1"))
AUTO_RESUME = os.getenv("AUTO_RESUME", "true").lower() == "true"
PAUSE_GRACE_SECONDS = int(os.getenv("PAUSE_GRACE_SECONDS", "6"))
START_COOLDOWN_SECONDS = float(os.getenv("START_COOLDOWN_SECONDS", "2"))


# ─── Flask ─────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")

if os.getenv("RENDER", "0") == "1":
    app.config.update(
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PREFERRED_URL_SCHEME="https",
    )

sp_search = spotipy.Spotify(
    auth_manager=SpotifyClientCredentials(
        client_id=SPOTIPY_CLIENT_ID,
        client_secret=SPOTIPY_CLIENT_SECRET,
    )
)

BG_THREAD_STARTED = False
BG_THREAD_LOCK = threading.Lock()

BOOT_TS = int(time.time())
BOOT_ID = f"{BOOT_TS}-{os.getpid()}"

LAST_PROGRESS_LOG_TS = 0.0
LAST_DISABLED_LOG_TS = 0.0
LAST_NO_TOKEN_LOG_TS = 0.0


def log(msg: str) -> None:
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
    storage.save_token(token_info)


def _get_token_info():
    ti = storage.get_token()
    if not ti:
        return None

    if ti.get("expires_at", 0) - int(time.time()) < 60:
        try:
            refresh_token = ti["refresh_token"]
            new_ti = _oauth().refresh_access_token(refresh_token)
            if not new_ti.get("refresh_token"):
                new_ti["refresh_token"] = refresh_token
            storage.save_token(new_ti)
            ti = new_ti
            log("Token refreshed")
        except Exception as e:
            log(f"Token refresh failed: {e}")
            return None

    return ti


def _user_sp():
    ti = _get_token_info()
    if not ti:
        return None
    return spotipy.Spotify(auth=ti["access_token"])


# ─── Device helpers ────────────────────────────────────────────────────────────
def _list_devices(sp_user):
    try:
        return sp_user.devices().get("devices", [])
    except spotipy.SpotifyException as e:
        log(f"devices() failed: {e}")
        return []


def _active_device(sp_user):
    devices = _list_devices(sp_user)
    active = next((d for d in devices if d.get("is_active")), None)
    storage.update_state(active_device_name=active.get("name") if active else None)
    return active


def _device_debug_payload(sp_user):
    devices = _list_devices(sp_user)
    return [
        {
            "id": d.get("id"),
            "name": d.get("name"),
            "type": d.get("type"),
            "is_active": bool(d.get("is_active")),
            "is_private_session": bool(d.get("is_private_session")),
            "is_restricted": bool(d.get("is_restricted")),
            "volume_percent": d.get("volume_percent"),
        }
        for d in devices
    ]


# ─── Queue / track helpers ─────────────────────────────────────────────────────
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
    if storage.get_track(tid):
        return

    try:
        sp = sp_user or _user_sp() or sp_search
        info = sp.track(tid)
        storage.upsert_track(tid, _track_dict_from_sp_item(info))
    except Exception as e:
        log(f"Failed to cache track {tid}: {e}")


def _ordered_ids(exclude_tid=None):
    return [tid for tid, _ in storage.get_ordered_votes(exclude_tid=exclude_tid)]


def _candidate_next_tid(current_uri):
    exclude_tid = (
        current_uri.split(":")[-1]
        if current_uri and current_uri.startswith("spotify:track:")
        else None
    )
    ids = _ordered_ids(exclude_tid=exclude_tid)
    return ids[0] if ids else None


def _is_valid_track_id(tid):
    return isinstance(tid, str) and re.fullmatch(r"[A-Za-z0-9]{22}", tid) is not None


def _state_snapshot():
    return storage.get_state()


# ─── Playback snapshot helpers ─────────────────────────────────────────────────
def _snapshot_from_current_playback(sp_user):
    try:
        pb = sp_user.current_playback()
    except spotipy.SpotifyException as e:
        log(f"current_playback() failed: {e}")
        return None

    if not pb or not pb.get("item"):
        return None

    device = pb.get("device") or {}
    device_name = device.get("name")
    if device_name is not None:
        storage.update_state(active_device_name=device_name)

    item = pb["item"]
    progress_ms = pb.get("progress_ms") or 0
    duration_ms = item.get("duration_ms") or 0
    uri = item.get("uri")
    is_playing = bool(pb.get("is_playing"))

    return {
        "uri": uri,
        "track_id": uri.split(":")[-1] if uri and uri.startswith("spotify:track:") else None,
        "progress_ms": int(progress_ms),
        "duration_ms": int(duration_ms),
        "remaining_ms": max(0, int(duration_ms) - int(progress_ms)),
        "progress_sec": int(progress_ms) // 1000,
        "duration_sec": int(duration_ms) // 1000,
        "remaining_sec": max(0, int(duration_ms - progress_ms)) // 1000,
        "is_playing": is_playing,
        "source": "current_playback",
    }


def _snapshot_from_currently_playing(sp_user):
    try:
        cp = sp_user.currently_playing()
    except spotipy.SpotifyException as e:
        log(f"currently_playing() failed: {e}")
        return None

    if not cp or not cp.get("item"):
        return None

    item = cp["item"]
    progress_ms = cp.get("progress_ms") or 0
    duration_ms = item.get("duration_ms") or 0
    uri = item.get("uri")
    is_playing = bool(cp.get("is_playing", True))

    return {
        "uri": uri,
        "track_id": uri.split(":")[-1] if uri and uri.startswith("spotify:track:") else None,
        "progress_ms": int(progress_ms),
        "duration_ms": int(duration_ms),
        "remaining_ms": max(0, int(duration_ms) - int(progress_ms)),
        "progress_sec": int(progress_ms) // 1000,
        "duration_sec": int(duration_ms) // 1000,
        "remaining_sec": max(0, int(duration_ms - progress_ms)) // 1000,
        "is_playing": is_playing,
        "source": "currently_playing",
    }


def _playback_snapshot(sp_user):
    snapshot = _snapshot_from_current_playback(sp_user)
    if snapshot:
        return snapshot
    return _snapshot_from_currently_playing(sp_user)


def _update_now_playing_from_snapshot(sp_user, snapshot):
    if not snapshot:
        return False

    uri = snapshot["uri"]
    dur_sec = snapshot["duration_sec"]
    prog_sec = snapshot["progress_sec"]
    is_playing = snapshot["is_playing"]

    now = time.time()
    ss = storage.get_state()
    prev_uri = ss["current_uri"]

    changed = uri != prev_uri
    popped_tid = None

    if changed:
        storage.update_state(
            current_uri=uri,
            current_duration_sec=dur_sec if uri else None,
            current_progress_sec=prog_sec if uri else None,
            current_is_playing=is_playing,
            queued_next_for_uri=None,
            cooldown_until=now + START_COOLDOWN_SECONDS,
        )

        if uri and uri.startswith("spotify:track:"):
            popped_tid = uri.split(":")[-1]
            storage.remove_vote(popped_tid)
    else:
        storage.update_state(
            current_duration_sec=dur_sec if uri else None,
            current_progress_sec=prog_sec if uri else None,
            current_is_playing=is_playing,
        )

    if uri and uri.startswith("spotify:track:"):
        _ensure_track_cached_by_tid(uri.split(":")[-1], sp_user=sp_user)

    if changed:
        log(
            f"Now playing → {uri} "
            f"({prog_sec}/{dur_sec}s via {snapshot['source']})"
        )
        if popped_tid:
            log(f"Removed current track from vote queue: {popped_tid}")

    return changed


def _ui_playback_view(sp_user):
    ss = _state_snapshot()

    state_uri = ss["current_uri"]
    state_dur = ss["current_duration_sec"]
    state_prog = ss["current_progress_sec"]
    state_is_playing = ss["current_is_playing"]
    auto_enabled = ss["auto_enabled"]

    if auto_enabled and state_uri:
        return {
            "uri": state_uri,
            "duration_sec": state_dur,
            "progress_sec": state_prog,
            "is_playing": state_is_playing,
            "source": "state",
        }

    if sp_user:
        live = _playback_snapshot(sp_user)
        if live:
            return {
                "uri": live["uri"],
                "duration_sec": live["duration_sec"],
                "progress_sec": live["progress_sec"],
                "is_playing": live["is_playing"],
                "source": "live",
            }

    return {
        "uri": state_uri,
        "duration_sec": state_dur,
        "progress_sec": state_prog,
        "is_playing": state_is_playing,
        "source": "state_fallback",
    }


def _should_attempt_queue(snapshot):
    if not snapshot:
        return False
    if not snapshot["uri"] or snapshot["duration_ms"] <= 0:
        return False
    if not snapshot["is_playing"]:
        return False
    return snapshot["remaining_ms"] <= (QUEUE_AHEAD_SECONDS * 1000)


def _queue_next_for_snapshot(sp_user, snapshot):
    next_tid = _candidate_next_tid(snapshot["uri"])
    if not next_tid:
        log("Queue skip: no next candidate from votes")
        return False, "no_next_candidate"

    if not _is_valid_track_id(next_tid):
        log(f"Bad next_tid: {next_tid!r}")
        return False, "bad_track_id"

    next_uri = f"spotify:track:{next_tid}"

    ss = storage.get_state()
    current_uri = ss["current_uri"]
    already_queued_for = ss["queued_next_for_uri"]

    log(
        "DBG queue precheck "
        f"snapshot_uri={snapshot['uri']} "
        f"current_uri={current_uri} "
        f"already_queued_for={already_queued_for} "
        f"next_tid={next_tid}"
    )

    if current_uri != snapshot["uri"]:
        log(
            "Queue skip: current changed before queue attempt "
            f"snapshot={snapshot['uri']} current={current_uri}"
        )
        return False, "current_changed"

    if already_queued_for == snapshot["uri"]:
        log(f"Queue skip: already queued for current song {snapshot['uri']}")
        return False, "already_queued_for_current_song"

    ti = _get_token_info()
    if not ti:
        log("Queue skip: missing token info")
        return False, "missing_token"

    headers = {"Authorization": f"Bearer {ti['access_token']}"}

    try:
        resp = requests.post(
            "https://api.spotify.com/v1/me/player/queue",
            headers=headers,
            params={"uri": next_uri},
            timeout=15,
            allow_redirects=False,
        )

        log(
            f"Queue attempt: next_uri={next_uri} "
            f"remaining={snapshot['remaining_sec']}s "
            f"status={resp.status_code} body={resp.text!r}"
        )

        if resp.status_code in (200, 204):
            storage.mark_queued_for_snapshot(
                snapshot["uri"],
                time.time() + 1.0,
            )
            log(f"Queued next successfully: {next_uri}")
            return True, next_uri

        return False, f"status={resp.status_code} body={resp.text}"
    except Exception as e:
        log(f"Queue request exception for {next_uri}: {e}")
        return False, str(e)


# ─── Background loop ───────────────────────────────────────────────────────────
def _background_loop():
    global LAST_PROGRESS_LOG_TS, LAST_DISABLED_LOG_TS, LAST_NO_TOKEN_LOG_TS

    log("Background loop started")

    while True:
        try:
            now = time.time()
            ss = storage.get_state()
            auto_enabled = ss["auto_enabled"]

            if not auto_enabled:
                if now - LAST_DISABLED_LOG_TS >= 10:
                    LAST_DISABLED_LOG_TS = now
                    log("Monitor idle: AUTO_ENABLED is false")
                time.sleep(POLL_SECONDS)
                continue

            sp_user = _user_sp()
            if not sp_user:
                if now - LAST_NO_TOKEN_LOG_TS >= 10:
                    LAST_NO_TOKEN_LOG_TS = now
                    log("Monitor idle: no Spotify user token/client")
                time.sleep(POLL_SECONDS)
                continue

            snapshot = _playback_snapshot(sp_user)
            if DEBUG_VERBOSE:
                log(f"DBG snapshot={snapshot}")

            if snapshot:
                _update_now_playing_from_snapshot(sp_user, snapshot)
            else:
                _active_device(sp_user)
                log("DBG snapshot is None; preserving current playback state")
                time.sleep(POLL_SECONDS)
                continue

            ss = storage.get_state()
            now = time.time()
            in_cooldown = now < (ss["cooldown_until"] or 0)
            current_device_name = ss["active_device_name"]

            if now - LAST_PROGRESS_LOG_TS >= 5:
                LAST_PROGRESS_LOG_TS = now
                next_tid = _candidate_next_tid(snapshot["uri"])
                should_queue = _should_attempt_queue(snapshot)
                log(
                    "Monitor: "
                    f"track={snapshot['track_id']} "
                    f"progress={snapshot['progress_sec']}/{snapshot['duration_sec']}s "
                    f"remaining={snapshot['remaining_sec']}s "
                    f"playing={snapshot['is_playing']} "
                    f"device={current_device_name} "
                    f"cooldown={in_cooldown} "
                    f"threshold_sec={QUEUE_AHEAD_SECONDS} "
                    f"should_queue={should_queue} "
                    f"next_candidate={next_tid}"
                )

            last_paused_ts = ss["last_paused_ts"]
            if not snapshot["is_playing"]:
                if last_paused_ts is None:
                    last_paused_ts = now
                    storage.update_state(last_paused_ts=last_paused_ts)
            else:
                if last_paused_ts is not None:
                    storage.update_state(last_paused_ts=None)
                last_paused_ts = None

            if in_cooldown:
                time.sleep(POLL_SECONDS)
                continue

            if (
                AUTO_RESUME
                and not snapshot["is_playing"]
                and last_paused_ts
                and (now - last_paused_ts) >= PAUSE_GRACE_SECONDS
            ):
                try:
                    sp_user.start_playback()
                    storage.update_state(cooldown_until=time.time() + 1.0)
                    log(f"Auto-resume on active device ({current_device_name})")
                except spotipy.SpotifyException as e:
                    log(f"Auto-resume failed on active device: {e}")
                time.sleep(POLL_SECONDS)
                continue

            if _should_attempt_queue(snapshot):
                log(
                    f"Threshold reached: current={snapshot['uri']} "
                    f"remaining={snapshot['remaining_sec']}s "
                    f"next_candidate={_candidate_next_tid(snapshot['uri'])}"
                )
                _queue_next_for_snapshot(sp_user, snapshot)

        except Exception as e:
            log(f"Background loop error: {e}")

        time.sleep(POLL_SECONDS)


def _start_background_thread_once():
    global BG_THREAD_STARTED
    with BG_THREAD_LOCK:
        if BG_THREAD_STARTED:
            return

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
    <h1>Spotify Vote Queue <span class="muted" style="font-size:0.8rem;">sqlite persistence build</span></h1>
    {% if authed %}
      <span class="muted">Logged in ✔</span>
      <a class="btn" href="{{ url_for('play_first') }}">▶️ Start with top track</a>
      <span class="muted">Poll {{ poll_seconds }}s • Queue at {{ ahead }}s remaining</span>
      {% if device_name %}
        <span class="muted">Active device: {{ device_name }}</span>
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

    sp_user = _user_sp()
    if sp_user:
        _active_device(sp_user)

    if query:
        try:
            items = sp_search.search(q=query, type="track", limit=10)["tracks"]["items"]
            for t in items:
                tid = t["id"]
                meta = _track_dict_from_sp_item(t)
                storage.upsert_track(tid, meta)
                results[tid] = meta
        except Exception as e:
            log(f"Search failed for query={query!r}: {e}")

    ui_pb = _ui_playback_view(sp_user)
    ss = _state_snapshot()

    np_uri = ui_pb["uri"]
    dur = ui_pb["duration_sec"]
    prog = ui_pb["progress_sec"]
    is_playing = ui_pb["is_playing"]

    device_name = ss["active_device_name"]

    np_tid = (
        np_uri.split(":")[-1]
        if np_uri and np_uri.startswith("spotify:track:")
        else None
    )

    queue = storage.get_ordered_votes(exclude_tid=np_tid)
    for tid, _ in queue:
        _ensure_track_cached_by_tid(tid, sp_user=sp_user)

    track_ids_for_page = [tid for tid, _ in queue]
    if np_tid:
        _ensure_track_cached_by_tid(np_tid, sp_user=sp_user)
        track_ids_for_page.append(np_tid)

    tracks_snapshot = storage.get_tracks(track_ids_for_page)

    now_playing = None
    if np_tid:
        meta = storage.get_track(np_tid)
        if meta:
            extra_bits = []
            if dur is not None:
                extra_bits.append(f"{dur}s total")
            if prog is not None and dur is not None:
                extra_bits.append(f"{max(0, dur - prog)}s left")
            if is_playing is False:
                extra_bits.append("paused")
            now_playing = dict(meta)
            now_playing["extra"] = " • ".join(extra_bits) if extra_bits else "Currently playing"

    return render_template_string(
        TEMPLATE,
        results=results,
        queue=queue,
        tracks=tracks_snapshot,
        query=query,
        authed=bool(_get_token_info()),
        ahead=QUEUE_AHEAD_SECONDS,
        poll_seconds=POLL_SECONDS,
        now_playing=now_playing,
        device_name=device_name,
    )


@app.route("/vote", methods=["POST"])
def vote():
    tid = request.form.get("track_id")
    if tid:
        storage.vote_delta(tid, +1)
    return redirect(url_for("index"))


@app.route("/downvote", methods=["POST"])
def downvote():
    tid = request.form.get("track_id")
    if tid:
        storage.vote_delta(tid, -1)
    return redirect(url_for("index"))


@app.route("/clear", methods=["POST"])
def clear():
    storage.clear_votes()
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
    if not token_info.get("refresh_token"):
        existing = storage.get_token()
        if existing and existing.get("refresh_token"):
            token_info["refresh_token"] = existing["refresh_token"]
    _save_token_info(token_info)
    log("Logged in to Spotify")
    return redirect(url_for("index"))


@app.route("/play_first")
def play_first():
    ids = _ordered_ids()
    if not ids:
        return redirect(url_for("index"))

    sp_user = _user_sp()
    if not sp_user:
        return redirect(url_for("login"))

    active = _active_device(sp_user)
    if not active:
        return (
            "No active Spotify device found. Start playback manually on your phone, desktop app, or Spotify Web Player first, then try again.",
            400,
        )

    top_id = ids[0]
    top_uri = f"spotify:track:{top_id}"

    try:
        sp_user.start_playback(uris=[top_uri])
        current_device_name = _state_snapshot()["active_device_name"]
        log(f"Start with top track on active device ({current_device_name}) → {top_uri}")
    except spotipy.SpotifyException as e:
        return f"Failed to start on active device: {e}", 400

    storage.remove_vote(top_id)

    time.sleep(0.7)

    snapshot = _playback_snapshot(sp_user)
    if snapshot:
        _update_now_playing_from_snapshot(sp_user, snapshot)

    auto_enabled_set_ts = int(time.time())
    storage.update_state(
        cooldown_until=time.time() + START_COOLDOWN_SECONDS,
        queued_next_for_uri=None,
        auto_enabled=True,
        auto_enabled_set_ts=auto_enabled_set_ts,
        boot_id=BOOT_ID,
    )

    log(
        f"Automation armed: AUTO_ENABLED=True "
        f"boot_id={BOOT_ID} pid={os.getpid()} "
        f"set_ts={auto_enabled_set_ts}"
    )
    return redirect(url_for("index"))


@app.route("/status.json")
def status_json():
    sp_user = _user_sp()
    if sp_user:
        try:
            _active_device(sp_user)
        except Exception as e:
            log(f"status.json device refresh failed: {e}")

    ui_pb = _ui_playback_view(sp_user)
    ss = _state_snapshot()

    uri = ui_pb["uri"]
    dur = ui_pb["duration_sec"]
    prog = ui_pb["progress_sec"]
    is_playing = ui_pb["is_playing"]

    queued_for = ss["queued_next_for_uri"]
    device_name = ss["active_device_name"]

    np = None
    if uri and uri.startswith("spotify:track:"):
        tid = uri.split(":")[-1]
        _ensure_track_cached_by_tid(tid, sp_user=sp_user)
        meta = storage.get_track(tid)
        if meta:
            extra_bits = []
            if dur is not None:
                extra_bits.append(f"{dur}s total")
            if prog is not None and dur is not None:
                extra_bits.append(f"{max(0, dur - prog)}s left")
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

    for tid in ids:
        _ensure_track_cached_by_tid(tid, sp_user=sp_user)

    tracks_snapshot = storage.get_tracks(ids)

    queue_payload = []
    vote_map = dict(storage.get_ordered_votes(exclude_tid=exclude_tid))
    for tid in ids:
        meta = tracks_snapshot.get(tid, {})
        queue_payload.append(
            {
                "track_id": tid,
                "votes": int(vote_map.get(tid, 0)),
                "track": {
                    "name": meta.get("name"),
                    "artist": meta.get("artist"),
                    "image": meta.get("image"),
                },
            }
        )

    next_candidate_tid = _candidate_next_tid(uri)
    ss = _state_snapshot()

    return jsonify(
        {
            "version": "sqlite-persistent-v1",
            "authed": bool(_get_token_info()),
            "ahead_seconds": QUEUE_AHEAD_SECONDS,
            "poll_seconds": POLL_SECONDS,
            "auto_enabled": ss["auto_enabled"],
            "now_playing": np,
            "queue": queue_payload,
            "active_device_name": device_name,
            "queued_next_for_uri": queued_for,
            "next_candidate_tid": next_candidate_tid,
            "ts": int(time.time()),
            "ui_source": ui_pb["source"],
            "boot_id": BOOT_ID,
            "pid": os.getpid(),
            "boot_ts": BOOT_TS,
            "uptime_sec": int(time.time()) - BOOT_TS,
            "auto_enabled_set_ts": ss["auto_enabled_set_ts"],
        }
    )


@app.route("/devices.json")
def devices_json():
    sp_user = _user_sp()
    if not sp_user:
        return jsonify({"authed": False, "devices": []})
    _active_device(sp_user)
    return jsonify(
        {
            "authed": True,
            "active_device_name": _state_snapshot()["active_device_name"],
            "devices": _device_debug_payload(sp_user),
            "ts": int(time.time()),
        }
    )


@app.route("/queue_sanity")
def queue_sanity():
    ti = _get_token_info()
    if not ti:
        return redirect(url_for("login"))

    test_uri = request.args.get("uri", "spotify:track:4uLU6hMCjMI75M1A2tKUQC")
    headers = {"Authorization": f"Bearer {ti['access_token']}"}

    result = {
        "test_uri": test_uri,
        "authed": True,
    }

    try:
        devices_resp = requests.get(
            "https://api.spotify.com/v1/me/player/devices",
            headers=headers,
            timeout=15,
        )
        result["devices_status"] = devices_resp.status_code
        try:
            result["devices_json"] = devices_resp.json()
        except Exception:
            result["devices_text"] = devices_resp.text
    except Exception as e:
        result["devices_error"] = str(e)

    try:
        playback_resp = requests.get(
            "https://api.spotify.com/v1/me/player",
            headers=headers,
            timeout=15,
        )
        result["playback_status"] = playback_resp.status_code
        try:
            result["playback_json"] = playback_resp.json()
        except Exception:
            result["playback_text"] = playback_resp.text
    except Exception as e:
        result["playback_error"] = str(e)

    try:
        queue_resp = requests.post(
            "https://api.spotify.com/v1/me/player/queue",
            headers=headers,
            params={"uri": test_uri},
            timeout=15,
            allow_redirects=False,
        )
        result["queue_status"] = queue_resp.status_code
        result["queue_text"] = queue_resp.text
        result["queue_ok"] = queue_resp.status_code in (200, 204)
    except Exception as e:
        result["queue_error"] = str(e)
        result["queue_ok"] = False

    return jsonify(result)


@app.route("/queue_sanity2")
def queue_sanity2():
    ti = _get_token_info()
    if not ti:
        return redirect(url_for("login"))

    test_uri = request.args.get("uri", "spotify:track:4uLU6hMCjMI75M1A2tKUQC")
    headers = {"Authorization": f"Bearer {ti['access_token']}"}

    result = {
        "test_uri": test_uri,
        "authed": True,
    }

    devices_resp = requests.get(
        "https://api.spotify.com/v1/me/player/devices",
        headers=headers,
        timeout=15,
        allow_redirects=False,
    )
    result["devices_status"] = devices_resp.status_code
    try:
        result["devices_json"] = devices_resp.json()
    except Exception:
        result["devices_text"] = devices_resp.text

    playback_resp = requests.get(
        "https://api.spotify.com/v1/me/player",
        headers=headers,
        timeout=15,
        allow_redirects=False,
    )
    result["playback_status"] = playback_resp.status_code
    try:
        result["playback_json"] = playback_resp.json()
    except Exception:
        result["playback_text"] = playback_resp.text

    queue_post = requests.post(
        "https://api.spotify.com/v1/me/player/queue",
        headers=headers,
        params={"uri": test_uri},
        timeout=15,
        allow_redirects=False,
    )
    result["queue_post_status"] = queue_post.status_code
    result["queue_post_url"] = queue_post.url
    result["queue_post_headers"] = dict(queue_post.headers)
    result["queue_post_text"] = queue_post.text

    time.sleep(1.0)

    queue_get = requests.get(
        "https://api.spotify.com/v1/me/player/queue",
        headers=headers,
        timeout=15,
        allow_redirects=False,
    )
    result["queue_get_status"] = queue_get.status_code
    result["queue_get_url"] = queue_get.url

    try:
        qj = queue_get.json()
        result["queue_get_json"] = qj
        queue_items = qj.get("queue", []) or []
        result["queue_uris"] = [item.get("uri") for item in queue_items[:10]]
        result["queue_names"] = [item.get("name") for item in queue_items[:10]]
        result["test_uri_found_in_queue"] = any(
            item.get("uri") == test_uri for item in queue_items
        )
    except Exception:
        result["queue_get_text"] = queue_get.text
        result["test_uri_found_in_queue"] = False

    return jsonify(result)


# ─── Startup ───────────────────────────────────────────────────────────────────
storage.init_db()
_start_background_thread_once()
log(f"Service booted: boot_id={BOOT_ID} pid={os.getpid()}")

if __name__ == "__main__":
    host = "0.0.0.0" if os.getenv("RENDER", "0") == "1" else "127.0.0.1"
    port = int(os.getenv("PORT", "5000"))

    if DEBUG_VERBOSE:
        log("Main started correctly")

    if os.getenv("WEB_CONCURRENCY", "1") != "1":
        log(
            "WARNING: WEB_CONCURRENCY is not 1. "
            "Use a single worker for this SQLite build."
        )

    app.run(host=host, port=port, debug=True)