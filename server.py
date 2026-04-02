#!/usr/bin/env python3
"""
Бэкенд панели управления загрузкой музыки.
Flask API + интеграция с Soulseek (slskd) и yt-dlp.
"""

import concurrent.futures
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory

app = Flask(__name__, static_folder='static', static_url_path='')

# --- Настройки ---
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".config.json")
SLSKD_URL = os.environ.get("SLSKD_URL", "http://localhost:5030/api/v0")

def _load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}

def _save_config(cfg):
    with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

_cfg = _load_config()
DOWNLOAD_DIR = os.environ.get("DOWNLOAD_DIR", _cfg.get("download_dir", os.path.expanduser("~/Downloads/Music")))
STATE_FILE = os.path.join(DOWNLOAD_DIR, ".panel_state.json")
AUDIO_EXTENSIONS = {'.flac', '.mp3', '.ogg', '.opus', '.m4a', '.aac', '.wav', '.wma', '.ape', '.webm'}
SEARCH_TIMEOUT = 8
DOWNLOAD_TIMEOUT = 90
DOWNLOAD_CHECK_INTERVAL = 2
MIN_WORKERS = 2
MAX_WORKERS = 10
NUM_WORKERS = MAX_WORKERS  # Стартуем на максимуме, адаптируемся вниз
HAS_FFMPEG = shutil.which("ffmpeg") is not None
# На Windows ffmpeg может лежать рядом в папке ffmpeg/
if not HAS_FFMPEG:
    local_ffmpeg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg")
    if os.path.isdir(local_ffmpeg):
        os.environ["PATH"] = local_ffmpeg + os.pathsep + os.environ.get("PATH", "")
        HAS_FFMPEG = shutil.which("ffmpeg") is not None

# --- Глобальное состояние ---
queue_lock = threading.Lock()
state = {
    "queue": [],        # [{id, artist, title, query, status, method, progress, error, file}]
    "paused": False,
    "running": False,
    "stats": {"downloaded": 0, "failed": 0, "total_added": 0}
}
worker_threads = []
stop_event = threading.Event()
pause_event = threading.Event()
pause_event.set()  # Не на паузе

# --- Трекинг скорости (для спидтеста) ---
speed_lock = threading.Lock()
speed_data = {
    "history": [],          # [{ts, size_bytes, duration_sec, source, track}]  последние 100 загрузок
    "current_start": None,  # время начала текущей загрузки
    "current_track": "",    # текущий трек
    "current_bytes": 0,     # текущий прогресс в байтах
    "current_total": 0,     # полный размер текущего файла
    "bytes_per_sec_avg": 0, # скользящая средняя скорость
}


def update_item(item_id, **kwargs):
    """Обновление полей элемента очереди по ID"""
    with queue_lock:
        for q in state["queue"]:
            if q["id"] == item_id:
                q.update(kwargs)
                break


def load_state():
    """Загрузка состояния из файла"""
    global state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                state["queue"] = saved.get("queue", [])
                state["stats"] = saved.get("stats", state["stats"])
        except:
            pass


def save_state():
    """Сохранение состояния"""
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            "queue": state["queue"],
            "stats": state["stats"]
        }, f, ensure_ascii=False, indent=2)


# ===== Soulseek API =====

def slskd_api(endpoint, method="GET", data=None):
    """Запрос к slskd"""
    url = f"{SLSKD_URL}/{endpoint}"
    headers = {"Content-Type": "application/json"}
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        body = resp.read().decode()
        return json.loads(body) if body.strip() else {}
    except:
        return None


def slsk_connected():
    """Проверка подключения к Soulseek"""
    s = slskd_api("server")
    return s and s.get("isLoggedIn", False)


def score_file(file_info, user_info):
    """Оценка файла Soulseek для ранжирования"""
    filename = file_info.get("filename", "").lower()
    ext = os.path.splitext(filename)[1]
    if ext not in AUDIO_EXTENSIONS:
        return -1
    bitrate = file_info.get("bitRate", 0) or 0
    speed = user_info.get("uploadSpeed", 0) or 0
    free = user_info.get("hasFreeUploadSlot", False)
    size = file_info.get("size", 0) or 0

    score = 0
    if ext == '.flac':
        score += 1000
    elif ext == '.wav':
        score += 900
    elif ext == '.ape':
        score += 850
    elif ext == '.mp3':
        score += 400 + min(bitrate, 320)
    elif ext in ('.ogg', '.opus'):
        score += 350 + min(bitrate, 320)
    elif ext == '.m4a':
        score += 350 + min(bitrate, 320)
    elif ext == '.aac':
        score += 300 + min(bitrate, 320)
    else:
        score += 200
    if free:
        score += 200
    if speed > 0:
        score += min(int(speed / 1024 / 10), 100)
    if size > 0:
        mb = size / 1024 / 1024
        if 2 < mb < 100:
            score += min(int(mb * 3), 80)
    return score


# ===== Источники загрузки =====
# Каждый источник: (id, label, search_prefix для yt-dlp или None)
YTDLP_SOURCES = [
    ("ytmusic",     "YouTube Music",  "ytsearch1:{q} official audio"),
    ("soundcloud",  "SoundCloud",     "scsearch1:{q}"),
    ("youtube",     "YouTube",        "ytsearch1:{q}"),
]


def _clean_query(artist, title):
    """Очистка строки от мусора для поиска"""
    q = f"{artist} - {title}"
    q = re.sub(r'\(.*?\)', '', q).strip()
    q = re.sub(r'\[.*?\]', '', q).strip()
    q = re.sub(r'\b(feat\.?|ft\.?)\b', '', q, flags=re.IGNORECASE).strip()
    q = re.sub(r'\s+', ' ', q)
    return q


def _safe_filename(query):
    return re.sub(r'[<>:"/\\|?*]', '_', query)[:200]


def _file_already_exists(query):
    """Проверка, скачан ли уже файл (и в корне, и в подпапках)"""
    safe = _safe_filename(query)
    for ext in ['mp3', 'opus', 'm4a', 'ogg', 'flac', 'wav', 'ape', 'aac']:
        if os.path.exists(os.path.join(DOWNLOAD_DIR, f"{safe}.{ext}")):
            return True
    # Проверяем по имени исполнителя и названию в подпапках
    q_lower = query.lower()
    try:
        for root, dirs, files in os.walk(DOWNLOAD_DIR):
            if '.incomplete' in root:
                continue
            for f in files:
                if q_lower.replace(' - ', ' ') in f.lower().replace(' - ', ' '):
                    return True
    except:
        pass
    return False


# ===== Probing: сбор кандидатов без скачивания =====

def probe_soulseek(artist, title, item_id):
    """Поиск на Soulseek, возврат лучших кандидатов [{score, source, label, username, file_info}]"""
    query = _clean_query(artist, title)
    result = slskd_api("searches", method="POST", data={"searchText": query})
    if not result:
        return []
    search_id = result.get("id")
    if not search_id:
        return []

    try:
        for _ in range(SEARCH_TIMEOUT):
            if stop_event.is_set():
                return []
            time.sleep(1)

        results = slskd_api(f"searches/{search_id}?includeResponses=true")
        if not results:
            return []

        candidates = []
        for resp in results.get("responses", []):
            ui = {
                "uploadSpeed": resp.get("uploadSpeed", 0),
                "hasFreeUploadSlot": resp.get("hasFreeUploadSlot", False)
            }
            for f in resp.get("files", []):
                s = score_file(f, ui)
                if s > 0:
                    ext = os.path.splitext(f.get("filename", ""))[1].lower()
                    bitrate = f.get("bitRate", 0) or 0
                    size_mb = round((f.get("size", 0) or 0) / 1024 / 1024, 1)
                    candidates.append({
                        "score": s,
                        "source": "soulseek",
                        "label": f"Soulseek ({ext} {bitrate}kbps {size_mb}MB)",
                        "username": resp.get("username", ""),
                        "file_info": f,
                    })
        candidates.sort(key=lambda x: x["score"], reverse=True)
        return candidates[:5]
    finally:
        slskd_api(f"searches/{search_id}", method="DELETE")


def probe_ytdlp(search_q, source_id, search_template, item_id):
    """Зондирование yt-dlp источника: получаем метаданные без скачивания"""
    search_url = search_template.format(q=search_q)
    cmd = [
        "yt-dlp", "--dump-json", "--no-download", "--no-playlist",
        "--no-warnings", "--socket-timeout", "15",
        search_url
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        if proc.returncode != 0 or not proc.stdout.strip():
            return None
        info = json.loads(proc.stdout.strip().split('\n')[0])

        # Ищем лучший аудио-формат
        best_abr = 0
        best_acodec = "unknown"
        for fmt in info.get("formats", []):
            abr = fmt.get("abr") or fmt.get("tbr") or 0
            if fmt.get("acodec", "none") != "none" and abr > best_abr:
                best_abr = abr
                best_acodec = fmt.get("acodec", "unknown")

        # Если нет formats — берём из верхнего уровня
        if best_abr == 0:
            best_abr = info.get("abr") or info.get("tbr") or 128
            best_acodec = info.get("acodec") or "unknown"

        # Оценка качества
        score = 0
        if best_acodec in ("opus", "vorbis"):
            score = 350 + min(int(best_abr), 320)
        elif best_acodec in ("mp4a", "aac", "mp4a.40.2"):
            score = 340 + min(int(best_abr), 320)
        elif best_acodec in ("mp3",):
            score = 330 + min(int(best_abr), 320)
        else:
            score = 300 + min(int(best_abr), 256)

        return {
            "score": score,
            "source": source_id,
            "label": f"{source_id} ({best_acodec} ~{int(best_abr)}kbps)",
            "url": info.get("webpage_url") or info.get("url") or search_url,
            "title": info.get("title", ""),
            "abr": best_abr,
            "acodec": best_acodec,
        }
    except Exception:
        return None


def find_best_candidate(artist, title, query, item_id):
    """
    Опрашиваем ВСЕ источники ПАРАЛЛЕЛЬНО, собираем кандидатов, сортируем по качеству.
    Если Soulseek нашёл FLAC (score ≥ 1000), можно не ждать остальных.
    """
    candidates = []
    search_q = _clean_query(artist, title)
    update_item(item_id, progress="🔍 Поиск по всем источникам...")

    def _probe_slsk():
        if slsk_connected():
            return probe_soulseek(artist, title, item_id)
        return []

    def _probe_yt(src):
        sid, _, tmpl = src
        return probe_ytdlp(search_q, sid, tmpl, item_id)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1 + len(YTDLP_SOURCES)) as pool:
        # Запускаем ВСЁ одновременно
        slsk_future = pool.submit(_probe_slsk)
        yt_futures = {pool.submit(_probe_yt, s): s for s in YTDLP_SOURCES}
        all_futures = [slsk_future] + list(yt_futures.keys())

        for future in concurrent.futures.as_completed(all_futures):
            if stop_event.is_set():
                return []
            try:
                result = future.result()
                if result:
                    if isinstance(result, list):
                        candidates.extend(result)
                        # Ранний выход: если нашли FLAC на Soulseek — не ждём остальных
                        best_slsk = max((c["score"] for c in result), default=0)
                        if best_slsk >= 1000:
                            update_item(item_id, progress="🎯 FLAC найден! Отменяю остальные...")
                            break
                    else:
                        candidates.append(result)
            except Exception:
                pass

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


# ===== Скачивание конкретного кандидата =====

def download_soulseek(candidate, query, item_id):
    """Скачивание файла с Soulseek"""
    username = candidate["username"]
    file_info = candidate["file_info"]
    filename = file_info.get("filename", "")
    eu = urllib.parse.quote(username, safe='')

    # Запоминаем файлы ДО скачивания для определения нового файла
    existing_files = set()
    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        if '.incomplete' in root:
            continue
        for f in files:
            existing_files.add(os.path.join(root, f))

    update_item(item_id, progress=f"⬇ Soulseek: качаю от {username}...")
    dl = slskd_api(f"transfers/downloads/{eu}", method="POST", data=[file_info])
    if dl is None:
        return False

    start = time.time()
    last_bytes = 0
    stall = 0
    while time.time() - start < DOWNLOAD_TIMEOUT:
        if stop_event.is_set():
            return False
        transfers = slskd_api(f"transfers/downloads/{eu}")
        if transfers:
            if isinstance(transfers, dict):
                transfers = [transfers]
            for ub in transfers:
                for di in ub.get("directories", []):
                    for tf in di.get("files", []):
                        if tf.get("filename") == filename:
                            st = tf.get("state", "")
                            bt = tf.get("bytesTransferred", 0)
                            sz = tf.get("size", 1) or 1
                            pct = int(bt / sz * 100)
                            update_item(item_id, progress=f"⬇ Soulseek: {pct}% от {username}")
                            with speed_lock:
                                speed_data["current_bytes"] = bt
                                speed_data["current_total"] = sz
                            if "Succeeded" in st:
                                time.sleep(1)  # Ждём пока файл запишется
                                _move_new_soulseek_file(existing_files, query)
                                return True
                            if any(x in st for x in ("Errored", "Rejected", "Cancelled", "TimedOut")):
                                return False
                            if bt > last_bytes:
                                last_bytes = bt
                                stall = 0
                            else:
                                stall += 1
                            if stall > 10:
                                return False
        time.sleep(DOWNLOAD_CHECK_INTERVAL)
    return False


def _move_new_soulseek_file(existing_files, query):
    """Находим новый файл (которого не было до скачивания) и перемещаем в корень"""
    import shutil
    safe = _safe_filename(query)
    for root, dirs, files in os.walk(DOWNLOAD_DIR):
        if root == DOWNLOAD_DIR or '.incomplete' in root:
            continue
        for f in files:
            fp = os.path.join(root, f)
            ext = os.path.splitext(f)[1].lower()
            if ext in AUDIO_EXTENSIONS and fp not in existing_files:
                dst = os.path.join(DOWNLOAD_DIR, f"{safe}{ext}")
                if not os.path.exists(dst):
                    try:
                        shutil.move(fp, dst)
                    except:
                        pass
    # Удаляем пустые подпапки
    for d in os.listdir(DOWNLOAD_DIR):
        dp = os.path.join(DOWNLOAD_DIR, d)
        if os.path.isdir(dp) and d != '.incomplete':
            try:
                if not os.listdir(dp):
                    os.rmdir(dp)
            except:
                pass


def download_ytdlp_direct(search_url, label, query, item_id):
    """Прямое скачивание через yt-dlp: поиск + загрузка за один вызов (без пробинга)"""
    safe_name = _safe_filename(query)
    output = os.path.join(DOWNLOAD_DIR, f"{safe_name}.%(ext)s")

    update_item(item_id, progress=f"⬇ {label}: ищу и качаю...")

    cmd = [
        "yt-dlp", "--no-playlist",
        "--format", "bestaudio[ext=m4a]/bestaudio",
        "--add-metadata",
        "--output", output,
        "--socket-timeout", "15", "--retries", "2",
        "--no-warnings", "--newline",
        search_url
    ]
    # concurrent-fragments и embed-thumbnail только если есть ffmpeg
    if HAS_FFMPEG:
        cmd.insert(-1, "--concurrent-fragments")
        cmd.insert(-1, "8")
        cmd.insert(-1, "--embed-thumbnail")

    dl_real_start = None  # Реальное начало загрузки (не поиска)
    last_error = ""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        while True:
            if stop_event.is_set():
                proc.kill()
                return False, 0
            line = proc.stdout.readline()
            if not line and proc.poll() is not None:
                break
            line_stripped = line.strip()
            if 'ERROR' in line_stripped:
                last_error = line_stripped[:120]
            if '[download]' in line and '%' in line:
                if dl_real_start is None:
                    dl_real_start = time.time()
                match = re.search(r'(\d+\.?\d*)%', line)
                if match:
                    update_item(item_id, progress=f"⬇ {label}: {match.group(1)}%")
                size_match = re.search(r'of\s+~?(\d+\.?\d*)(MiB|KiB|GiB)', line)
                if size_match:
                    sz_val = float(size_match.group(1))
                    unit = size_match.group(2)
                    mult = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}.get(unit, 1024**2)
                    total_b = int(sz_val * mult)
                    pct_val = float(match.group(1)) if match else 0
                    cur_b = int(total_b * pct_val / 100)
                    with speed_lock:
                        speed_data["current_bytes"] = cur_b
                        speed_data["current_total"] = total_b
        dl_duration = time.time() - dl_real_start if dl_real_start else 0
        if proc.returncode != 0 and last_error:
            update_item(item_id, progress=f"❌ {label}: {last_error}")
        return proc.returncode == 0, dl_duration
    except Exception as e:
        update_item(item_id, progress=f"❌ {label}: {str(e)[:80]}")
        return False, 0


# ===== Воркер загрузки =====

active_workers = {"count": 0}
workers_lock = threading.Lock()


def _get_next_item():
    """Извлечь следующий pending трек из очереди (thread-safe)"""
    with queue_lock:
        for q in state["queue"]:
            if q["status"] == "pending":
                q["status"] = "downloading"
                return q.copy()
    return None


def _process_item(item):
    """Обработка одного трека — максимум скорости + максимум источников"""
    item_id = item["id"]
    artist = item["artist"]
    title = item["title"]
    query = item["query"]

    try:
        # Уже скачан?
        if _file_already_exists(query):
            update_item(item_id, status="done", method="уже есть", progress="✅ Файл уже скачан")
            with queue_lock:
                state["stats"]["downloaded"] += 1
            save_state()
            return

        dl_start_time = time.time()
        with speed_lock:
            speed_data["current_start"] = dl_start_time
            speed_data["current_track"] = query
            speed_data["current_bytes"] = 0
            speed_data["current_total"] = 0

        search_q = _clean_query(artist, title)
        downloaded = False

        # Параллельно: запускаем поиск в Soulseek пока качаем с yt-dlp
        slsk_result = []
        slsk_future = None
        if slsk_connected():
            slsk_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
            slsk_future = slsk_pool.submit(probe_soulseek, artist, title, item_id)

        # Быстрый путь: качаем через yt-dlp (поиск + загрузка за один вызов)
        fast_sources = [
            ("YouTube Music", f"ytsearch1:{search_q} official audio"),
            ("YouTube",       f"ytsearch1:{search_q}"),
            ("SoundCloud",    f"scsearch1:{search_q}"),
        ]

        for label, search_url in fast_sources:
            if stop_event.is_set():
                update_item(item_id, status="pending", progress="")
                return

            ok, dl_duration = download_ytdlp_direct(search_url, label, query, item_id)
            if ok:
                dl_size = _find_file_size(query)
                with speed_lock:
                    speed_data["history"].append({
                        "ts": time.time(),
                        "size_bytes": dl_size,
                        "duration_sec": round(dl_duration, 2),
                        "source": label,
                        "track": query
                    })
                    if len(speed_data["history"]) > 200:
                        speed_data["history"] = speed_data["history"][-200:]
                    _recalc_avg_speed()
                    _adapt_workers()
                    speed_data["current_start"] = None
                    speed_data["current_track"] = ""

                update_item(item_id, status="done", method=label, progress=f"✅ {label}")
                with queue_lock:
                    state["stats"]["downloaded"] += 1
                downloaded = True
                break

        # Если yt-dlp не справился — пробуем Soulseek
        if not downloaded and slsk_future:
            update_item(item_id, progress="🔍 Soulseek: ищу...")
            try:
                slsk_result = slsk_future.result(timeout=20)
            except Exception:
                slsk_result = []

            if slsk_result:
                for cand in slsk_result:
                    if stop_event.is_set():
                        update_item(item_id, status="pending", progress="")
                        return
                    slsk_dl_start = time.time()
                    ok = download_soulseek(cand, query, item_id)
                    if ok:
                        dl_duration = time.time() - slsk_dl_start
                        dl_size = _find_file_size(query)
                        with speed_lock:
                            speed_data["history"].append({
                                "ts": time.time(),
                                "size_bytes": dl_size,
                                "duration_sec": round(dl_duration, 1),
                                "source": "soulseek",
                                "track": query
                            })
                            if len(speed_data["history"]) > 200:
                                speed_data["history"] = speed_data["history"][-200:]
                            _recalc_avg_speed()
                            _adapt_workers()
                            speed_data["current_start"] = None
                            speed_data["current_track"] = ""

                        update_item(item_id, status="done", method=cand.get("label", "Soulseek"),
                                    progress=f"✅ Soulseek")
                        with queue_lock:
                            state["stats"]["downloaded"] += 1
                        downloaded = True
                        break

        if stop_event.is_set():
            update_item(item_id, status="pending", progress="")
            return

        if not downloaded:
            update_item(item_id, status="failed", progress="❌ Все источники исчерпаны")
            with queue_lock:
                state["stats"]["failed"] += 1

        save_state()

    except Exception as e:
        update_item(item_id, status="failed", progress=f"❌ Ошибка: {str(e)[:50]}")
        with queue_lock:
            state["stats"]["failed"] += 1
        save_state()


def download_worker(worker_id=0):
    """Фоновый воркер — берёт треки из очереди и обрабатывает"""
    with workers_lock:
        active_workers["count"] += 1
    state["running"] = True

    try:
        while not stop_event.is_set():
            pause_event.wait()
            if stop_event.is_set():
                break

            item = _get_next_item()
            if not item:
                time.sleep(0.5)
                # Если нет задач — выходим
                with queue_lock:
                    has_pending = any(q["status"] == "pending" for q in state["queue"])
                if not has_pending:
                    time.sleep(1)
                    with queue_lock:
                        has_pending = any(q["status"] == "pending" for q in state["queue"])
                    if not has_pending:
                        break
                continue

            _process_item(item)
            time.sleep(0.3)
    finally:
        with workers_lock:
            active_workers["count"] -= 1
            if active_workers["count"] <= 0:
                active_workers["count"] = 0
                state["running"] = False


def _find_file_size(query):
    """Находим размер скачанного файла по query"""
    safe = _safe_filename(query)
    for ext in ['flac', 'mp3', 'opus', 'm4a', 'ogg', 'wav', 'ape', 'aac']:
        fp = os.path.join(DOWNLOAD_DIR, f"{safe}.{ext}")
        if os.path.exists(fp):
            return os.path.getsize(fp)
    return 0


def _recalc_avg_speed():
    """Пересчёт средней скорости по последним загрузкам (вызывать под speed_lock)"""
    recent = [h for h in speed_data["history"] if h["duration_sec"] > 0 and h["size_bytes"] > 0]
    if not recent:
        speed_data["bytes_per_sec_avg"] = 0
        return
    recent = recent[-20:]
    total_bytes = sum(h["size_bytes"] for h in recent)
    total_sec = sum(h["duration_sec"] for h in recent)
    speed_data["bytes_per_sec_avg"] = total_bytes / total_sec if total_sec > 0 else 0


def _adapt_workers():
    """Адаптация количества воркеров под скорость интернета.
    Если средняя скорость на воркер падает ниже порога — уменьшаем.
    Если есть запас — увеличиваем. По умолчанию MAX_WORKERS."""
    global NUM_WORKERS
    with speed_lock:
        avg_speed = speed_data["bytes_per_sec_avg"]

    if avg_speed <= 0:
        return  # Нет данных — оставляем максимум

    current_workers = active_workers.get("count", NUM_WORKERS)
    if current_workers <= 0:
        return

    # Скорость на воркер (байт/с)
    speed_per_worker = avg_speed / max(current_workers, 1)
    # Общая скорость (Мбит/с)
    total_mbps = avg_speed * 8 / 1_000_000

    # Не опускаемся ниже 10 Мбит/с общей скорости
    if total_mbps < 10 and NUM_WORKERS > MIN_WORKERS:
        NUM_WORKERS = max(MIN_WORKERS, NUM_WORKERS - 1)
    # Если на воркер больше 500 КБ/с и ещё есть запас — увеличиваем
    elif speed_per_worker > 500 * 1024 and NUM_WORKERS < MAX_WORKERS:
        NUM_WORKERS = min(MAX_WORKERS, NUM_WORKERS + 1)


worker_threads = []


def ensure_worker():
    """Запуск воркеров (NUM_WORKERS штук) если не запущены"""
    global worker_threads
    stop_event.clear()
    pause_event.set()
    state["paused"] = False

    # Удаляем завершённые потоки
    worker_threads = [t for t in worker_threads if t.is_alive()]

    # Добиваем до нужного количества
    while len(worker_threads) < NUM_WORKERS:
        wid = len(worker_threads)
        t = threading.Thread(target=download_worker, args=(wid,), daemon=True)
        t.start()
        worker_threads.append(t)


# ===== API роуты =====

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/speedtest')
def speedtest_page():
    return send_from_directory('static', 'speedtest.html')


@app.route('/api/speedtest')
def api_speedtest():
    """Расширенная статистика скорости для спидтеста"""
    with speed_lock:
        history = list(speed_data["history"])
        avg_speed = speed_data["bytes_per_sec_avg"]
        cur_start = speed_data["current_start"]
        cur_track = speed_data["current_track"]
        cur_bytes = speed_data["current_bytes"]
        cur_total = speed_data["current_total"]

    with queue_lock:
        pending = sum(1 for q in state["queue"] if q["status"] == "pending")
        downloading = sum(1 for q in state["queue"] if q["status"] == "downloading")
        done = sum(1 for q in state["queue"] if q["status"] == "done")
        failed = sum(1 for q in state["queue"] if q["status"] == "failed")

    # Средний размер файла из истории
    sizes = [h["size_bytes"] for h in history if h["size_bytes"] > 0]
    avg_file_size = sum(sizes) / len(sizes) if sizes else 15 * 1024 * 1024  # ~15 MB по умолчанию

    # Среднее время на трек (включая поиск)
    durations = [h["duration_sec"] for h in history if h["duration_sec"] > 0]
    avg_track_time = sum(durations) / len(durations) if durations else 30

    # ETA
    remaining = pending + downloading
    eta_seconds = remaining * avg_track_time if avg_track_time > 0 else 0

    # Текущая скорость (последние 5 загрузок)
    last5 = [h for h in history[-5:] if h["duration_sec"] > 0 and h["size_bytes"] > 0]
    if last5:
        cur_speed = sum(h["size_bytes"] for h in last5) / sum(h["duration_sec"] for h in last5)
    else:
        cur_speed = 0

    # Мгновенная скорость текущего файла
    instant_speed = 0
    if cur_start and cur_bytes > 0:
        elapsed = time.time() - cur_start
        if elapsed > 0:
            instant_speed = cur_bytes / elapsed

    # Файлы на диске
    file_count = 0
    total_size = 0
    try:
        for root, dirs, files in os.walk(DOWNLOAD_DIR):
            if '.incomplete' in root:
                continue
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in AUDIO_EXTENSIONS:
                    file_count += 1
                    total_size += os.path.getsize(os.path.join(root, f))
    except:
        pass

    # Последние 10 загрузок для графика
    recent = history[-30:]

    return jsonify({
        "running": state["running"],
        "paused": state["paused"],
        "queue": {
            "pending": pending,
            "downloading": downloading,
            "done": done,
            "failed": failed,
            "total": len(state["queue"]),
        },
        "speed": {
            "current_bps": round(cur_speed),
            "instant_bps": round(instant_speed),
            "avg_bps": round(avg_speed),
            "current_mbps": round(cur_speed / 1024 / 1024, 2),
            "avg_mbps": round(avg_speed / 1024 / 1024, 2),
        },
        "timing": {
            "avg_track_sec": round(avg_track_time, 1),
            "avg_file_mb": round(avg_file_size / 1024 / 1024, 1),
            "eta_seconds": round(eta_seconds),
            "remaining_tracks": remaining,
        },
        "disk": {
            "files": file_count,
            "size_mb": round(total_size / 1024 / 1024, 1),
            "size_gb": round(total_size / 1024 / 1024 / 1024, 2),
        },
        "current": {
            "track": cur_track,
            "bytes": cur_bytes,
            "total": cur_total,
            "start": cur_start,
        },
        "recent": [{
            "track": h["track"][:40],
            "size_mb": round(h["size_bytes"] / 1024 / 1024, 1),
            "duration": h["duration_sec"],
            "source": h["source"],
            "speed_mbps": round(h["size_bytes"] / h["duration_sec"] / 1024 / 1024, 2) if h["duration_sec"] > 0 else 0,
        } for h in recent],
    })


@app.route('/api/status')
def api_status():
    """Статус системы"""
    with queue_lock:
        pending = sum(1 for q in state["queue"] if q["status"] == "pending")
        downloading = sum(1 for q in state["queue"] if q["status"] == "downloading")
        done = sum(1 for q in state["queue"] if q["status"] == "done")
        failed = sum(1 for q in state["queue"] if q["status"] == "failed")

    # Количество файлов на диске
    file_count = 0
    total_size = 0
    try:
        for f in os.listdir(DOWNLOAD_DIR):
            fp = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(fp) and not f.startswith('.'):
                ext = os.path.splitext(f)[1].lower()
                if ext in AUDIO_EXTENSIONS:
                    file_count += 1
                    total_size += os.path.getsize(fp)
    except:
        pass

    # Рекурсивно тоже считаем (Soulseek кладёт в подпапки)
    try:
        for root, dirs, files in os.walk(DOWNLOAD_DIR):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in AUDIO_EXTENSIONS:
                    fp = os.path.join(root, f)
                    if fp not in [os.path.join(DOWNLOAD_DIR, x) for x in os.listdir(DOWNLOAD_DIR) if os.path.isfile(os.path.join(DOWNLOAD_DIR, x))]:
                        file_count += 1
                        total_size += os.path.getsize(fp)
    except:
        pass

    return jsonify({
        "running": state["running"],
        "paused": state["paused"],
        "slsk_connected": slsk_connected(),
        "sources": ["Soulseek (FLAC)", "YouTube Music", "SoundCloud", "YouTube"],
        "workers": active_workers["count"],
        "max_workers": MAX_WORKERS,
        "adaptive_workers": NUM_WORKERS,
        "queue_pending": pending,
        "queue_downloading": downloading,
        "queue_done": done,
        "queue_failed": failed,
        "queue_total": len(state["queue"]),
        "files_on_disk": file_count,
        "disk_size_mb": round(total_size / 1024 / 1024, 1),
        "stats": state["stats"]
    })


@app.route('/api/queue')
def api_queue():
    """Возвращает очередь"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    filter_status = request.args.get('status', '')

    with queue_lock:
        items = state["queue"]
        if filter_status:
            items = [q for q in items if q["status"] == filter_status]
        total = len(items)
        start = (page - 1) * per_page
        items = items[start:start + per_page]

    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "per_page": per_page
    })


@app.route('/api/add', methods=['POST'])
def api_add():
    """Добавление треков"""
    data = request.json
    tracks = data.get('tracks', [])

    if isinstance(tracks, str):
        # Одиночный трек
        tracks = [tracks]

    added = 0
    with queue_lock:
        existing = {q["query"] for q in state["queue"]}
        for line in tracks:
            line = line.strip()
            if not line or ' - ' not in line:
                continue
            if line in existing:
                continue

            parts = line.split(' - ', 1)
            item = {
                "id": f"t{int(time.time()*1000)}-{added}",
                "artist": parts[0].strip(),
                "title": parts[1].strip(),
                "query": line,
                "status": "pending",
                "method": "",
                "progress": "",
                "error": "",
                "added_at": datetime.now().isoformat()
            }
            state["queue"].append(item)
            existing.add(line)
            state["stats"]["total_added"] += 1
            added += 1

    save_state()

    # Автостарт воркера
    ensure_worker()

    return jsonify({"added": added, "total": len(state["queue"])})


@app.route('/api/add-file', methods=['POST'])
def api_add_file():
    """Загрузка списка из файла"""
    text = request.data.decode('utf-8')
    lines = [l.strip() for l in text.strip().split('\n') if l.strip() and ' - ' in l.strip()]
    # Используем тот же api_add
    return api_add_impl(lines)


def api_add_impl(lines):
    """Общая логика добавления"""
    added = 0
    with queue_lock:
        existing = {q["query"] for q in state["queue"]}
        for line in lines:
            if line in existing:
                continue
            parts = line.split(' - ', 1)
            if len(parts) != 2:
                continue
            item = {
                "id": f"t{int(time.time()*1000)}-{added}",
                "artist": parts[0].strip(),
                "title": parts[1].strip(),
                "query": line,
                "status": "pending",
                "method": "",
                "progress": "",
                "error": "",
                "added_at": datetime.now().isoformat()
            }
            state["queue"].append(item)
            existing.add(line)
            state["stats"]["total_added"] += 1
            added += 1
    save_state()
    ensure_worker()
    return jsonify({"added": added, "total": len(state["queue"])})


@app.route('/api/start', methods=['POST'])
def api_start():
    """Запуск/возобновление загрузки"""
    stop_event.clear()
    pause_event.set()
    state["paused"] = False
    ensure_worker()
    return jsonify({"ok": True, "running": True})


@app.route('/api/pause', methods=['POST'])
def api_pause():
    """Пауза"""
    pause_event.clear()
    state["paused"] = True
    return jsonify({"ok": True, "paused": True})


@app.route('/api/resume', methods=['POST'])
def api_resume():
    """Снятие с паузы"""
    pause_event.set()
    state["paused"] = False
    ensure_worker()
    return jsonify({"ok": True, "paused": False})


@app.route('/api/stop', methods=['POST'])
def api_stop():
    """Полная остановка"""
    stop_event.set()
    pause_event.set()  # Разблокируем чтобы тред завершился
    state["paused"] = False
    return jsonify({"ok": True, "running": False})


@app.route('/api/retry-failed', methods=['POST'])
def api_retry_failed():
    """Повторить все неудачные"""
    count = 0
    with queue_lock:
        for q in state["queue"]:
            if q["status"] == "failed":
                q["status"] = "pending"
                q["progress"] = ""
                q["error"] = ""
                q["method"] = ""
                count += 1
    save_state()
    ensure_worker()
    return jsonify({"retried": count})


@app.route('/api/clear-done', methods=['POST'])
def api_clear_done():
    """Убрать завершённые из очереди"""
    with queue_lock:
        state["queue"] = [q for q in state["queue"] if q["status"] != "done"]
    save_state()
    return jsonify({"ok": True})


@app.route('/api/remove/<item_id>', methods=['DELETE'])
def api_remove(item_id):
    """Удалить элемент из очереди"""
    with queue_lock:
        state["queue"] = [q for q in state["queue"] if q["id"] != item_id]
    save_state()
    return jsonify({"ok": True})


@app.route('/api/clear-all', methods=['POST'])
def api_clear_all():
    """Удалить всю очередь"""
    # Останавливаем воркер перед очисткой
    stop_event.set()
    pause_event.set()
    state["paused"] = False
    state["running"] = False
    with queue_lock:
        count = len(state["queue"])
        state["queue"] = []
    save_state()
    return jsonify({"ok": True, "removed": count})


@app.route('/api/workers', methods=['GET'])
def api_get_workers():
    return jsonify({"workers": NUM_WORKERS, "min": MIN_WORKERS, "max": MAX_WORKERS})


@app.route('/api/workers', methods=['POST'])
def api_set_workers():
    global NUM_WORKERS
    data = request.json
    n = data.get("count", NUM_WORKERS)
    NUM_WORKERS = max(MIN_WORKERS, min(MAX_WORKERS, int(n)))
    return jsonify({"workers": NUM_WORKERS})


@app.route('/api/download-dir', methods=['GET'])
def api_get_download_dir():
    return jsonify({"path": DOWNLOAD_DIR})


@app.route('/api/download-dir', methods=['POST'])
def api_set_download_dir():
    global DOWNLOAD_DIR, STATE_FILE
    data = request.json
    new_path = data.get("path", "").strip()
    if not new_path:
        return jsonify({"error": "Пустой путь"}), 400
    new_path = os.path.expanduser(new_path)
    os.makedirs(new_path, exist_ok=True)
    DOWNLOAD_DIR = new_path
    STATE_FILE = os.path.join(DOWNLOAD_DIR, ".panel_state.json")
    _cfg = _load_config()
    _cfg["download_dir"] = DOWNLOAD_DIR
    _save_config(_cfg)
    load_state()
    return jsonify({"path": DOWNLOAD_DIR})


@app.route('/api/import-playlist', methods=['POST'])
def api_import_playlist():
    """Импорт из файла Песни.txt"""
    # Ищем Песни.txt: сначала рядом с server.py, потом на рабочем столе
    base_dir = os.path.dirname(os.path.abspath(__file__))
    songs_file = os.path.join(base_dir, "Песни.txt")
    if not os.path.exists(songs_file):
        songs_file = os.path.expanduser("~/Desktop/Песни.txt")
    if not os.path.exists(songs_file):
        return jsonify({"error": "Файл Песни.txt не найден (ни в папке проекта, ни на рабочем столе)"}), 404

    with open(songs_file, 'r', encoding='utf-8') as f:
        lines = [l.strip() for l in f if l.strip() and ' - ' in l.strip()]

    return api_add_impl(lines)


# ===== Запуск =====

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
load_state()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8844, debug=False)
