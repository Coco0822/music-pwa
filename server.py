from flask import Flask, request, jsonify, send_from_directory
import math, os, statistics, time, threading, random, socket
from collections import deque
import logging
import requests
from pydrive2.auth import GoogleAuth
from pydrive2.drive import GoogleDrive
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# ====== 1. Google Drive 設定 ======
scope = ["https://www.googleapis.com/auth/drive"]
KEY_FILE = "bpm1231-12d6db371901.json"
FOLDER_ID = "1OkDdBBl2gWOP-aKr6giPPAWzP_9zVG6s"
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycbz3LY60s9sbk1NAxVRHvpy0FhZglydmxU9QTs-6C_IX0f2C9rgDCTgkxev5WhYAGR0N/exec"

try:
    creds = ServiceAccountCredentials.from_json_keyfile_name(KEY_FILE, scope)
    gauth = GoogleAuth()
    gauth.credentials = creds
    drive = GoogleDrive(gauth)
    print("Google Drive 系統已就緒")
except Exception as e:
    print(f"API 失敗: {e}")
    drive = None

TEMP_FOLDER = "temp_music"
os.makedirs(TEMP_FOLDER, exist_ok=True)

# ====== 2. 核心計算法變數 ======
API_KEY = "1234"

SAMPLING_RATE = 30
WINDOW_SIZE = 90      # 3 秒 (30 Hz * 3)
ALPHA = 0.3

# 所有共享狀態加上鎖，防止 Race Condition
_lock = threading.Lock()

data_buffer = deque(maxlen=WINDOW_SIZE)
last_filtered_mag = 9.8
latest_stable_bpm = 0
batch_bpm_list = []
recent_bpms = deque(maxlen=2)
zero_streak = 0  # 連續幾次收到全 0

# [修正3] play_queue 加上長度上限
MAX_QUEUE_SIZE = 50
play_queue = []

# ====== 快取與除錯機制 ======
SONG_CACHE = []
CACHE_LAST_UPDATED = 0
CACHE_TTL = 3600
_cache_lock = threading.Lock()

def get_all_songs_metadata(force_refresh=False):
    global SONG_CACHE, CACHE_LAST_UPDATED
    current_time = time.time()

    # [Bug 7 修正] 整段包在鎖內,避免併發 force_refresh 同時打 Google API
    with _cache_lock:
        if not force_refresh and SONG_CACHE and (current_time - CACHE_LAST_UPDATED < CACHE_TTL):
            return SONG_CACHE

        try:
            print("\n正在向 Google 雲端請求歌曲資料...")

            r = requests.get(APPS_SCRIPT_URL, timeout=10)
            bpm_data = r.json() if r.status_code == 200 else {}
            if not bpm_data:
                print("Apps Script 試算表回傳空的資料,請檢查網址或權限！")

            if drive is None:
                print("Google Drive 未初始化,無法取得歌曲列表")
                return SONG_CACHE

            files = drive.ListFile({'q': f"'{FOLDER_ID}' in parents and mimeType='audio/mpeg'"}).GetList()
            file_dict = {f['id']: f['title'] for f in files}
            if not file_dict:
                print("在 Google Drive 裡找不到任何 MP3 (audio/mpeg) 檔案！")

            matched = []
            for fid, info in bpm_data.items():
                if fid in file_dict:
                    matched.append({'title': file_dict[fid], 'file_id': fid, 'bpm': info['bpm']})

            if matched:
                SONG_CACHE = matched
                CACHE_LAST_UPDATED = current_time
                print(f"成功抓取 共有 {len(SONG_CACHE)} 首歌準備就緒。")
            else:
                print("比對失敗：沒有任何一首歌同時存在於『雲端硬碟』與『BPM試算表』中。")

            return matched if matched else SONG_CACHE

        except Exception as e:
            print(f"快取更新發生嚴重錯誤: {e}")
            return SONG_CACHE

def clean_temp_folder(max_files=10):
    try:
        files = [os.path.join(TEMP_FOLDER, f) for f in os.listdir(TEMP_FOLDER) if f.endswith('.mp3')]
        if len(files) > max_files:
            # [修正8] 改用最後存取時間排序，避免刪到即將播放的歌
            files.sort(key=os.path.getatime)
            for f in files[:-max_files]:
                os.remove(f)
    except Exception as e:
        print(f"清理暫存檔案失敗: {e}")

# ====== 3. 步頻算法邏輯 ======

def process_acceleration(data):
    global last_filtered_mag
    try:
        ax = float(data.get("accelerometerAccelerationX", 0))
        ay = float(data.get("accelerometerAccelerationY", 0))
        az = float(data.get("accelerometerAccelerationZ", 0))
    except:
        ax, ay, az = 0.0, 0.0, 0.0
    raw_mag = math.sqrt(ax**2 + ay**2 + az**2)
    # EMA 低通濾波:扣除重力,保留純動作訊號
    with _lock:
        pure_motion = raw_mag - last_filtered_mag
        last_filtered_mag = (ALPHA * raw_mag) + ((1 - ALPHA) * last_filtered_mag)
    return pure_motion * 10

def calculate_bpm(buffer):
    if len(buffer) < WINDOW_SIZE:
        return 0
    if (max(buffer) - min(buffer)) < 12:
        return 0

    avg = sum(buffer) / len(buffer)
    indices = [i for i in range(1, len(buffer)) if buffer[i-1] <= avg and buffer[i] > avg]
    if len(indices) < 2:
        return 0

    intervals = [indices[i] - indices[i-1] for i in range(1, len(indices))]
    avg_interval = sum(intervals) / len(intervals)

    result = int(60 / (avg_interval / SAMPLING_RATE)) if avg_interval > 0 else 0
    if result < 80 or result > 220:
        return 0
    return result

# ====== 4. 路由與功能 ======

@app.route("/")
def index():
    return send_from_directory('.', 'index.html')

@app.route("/manifest.json")
def manifest():
    return send_from_directory('.', 'manifest.json')

@app.route("/sw.js")
def service_worker():
    return send_from_directory('.', 'sw.js', mimetype='application/javascript')

@app.route('/music/<filename>')
def serve_music(filename):
    # [修正2-補強] 防止路徑穿越攻擊
    if '..' in filename or '/' in filename:
        return "Invalid filename", 400

    file_path = os.path.join(TEMP_FOLDER, filename)

    if not os.path.exists(file_path):
        try:
            file_id = filename.replace("temp_", "").replace(".mp3", "")
            print(f"\n觸發時光機！自動從雲端找回被清理的舊歌...")
            if drive is None:
                return "Drive not available", 503
            f = drive.CreateFile({'id': file_id})
            f.GetContentFile(file_path)
            print("找回成功！")
        except Exception as e:
            print(f"找回舊歌失敗: {e}")
            return "File not found", 404

    return send_from_directory(TEMP_FOLDER, filename)

@app.post("/sensor")
def sensor():
    global latest_stable_bpm, batch_bpm_list, zero_streak
    key = request.headers.get("X-API-Key")
    if key != API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"ok": False})

    pure_signal = process_acceleration(data)

    # 用鎖保護 buffer 和 batch_bpm_list 的讀寫
    with _lock:
        data_buffer.append(pure_signal)
        current_bpm = calculate_bpm(list(data_buffer))
        batch_bpm_list.append(current_bpm)

        if len(batch_bpm_list) >= 60:
            raw_bpm = int(statistics.median(batch_bpm_list))
            batch_bpm_list = []
            
            if raw_bpm > 0:
                # 有偵測到步頻，正常更新
                zero_streak = 0
                recent_bpms.append(raw_bpm)
                latest_stable_bpm = int(sum(recent_bpms) / len(recent_bpms)) + 10
            else:
                # 中位數是 0，可能停了也可能在過渡期
                zero_streak += 1
                if zero_streak >= 3:
                    # 連續 3 次（約 6 秒）都是 0，真的停了
                    recent_bpms.clear()
                    latest_stable_bpm = 0
                # 否則保留上次的值，不動
            
            print(f"\r當前步頻: {latest_stable_bpm:>3} BPM", end="")
    return jsonify({"ok": True, "bpm": current_bpm})

@app.route("/get_data")
def get_data():
    with _lock:
        bpm = latest_stable_bpm
    return jsonify({"bpm": bpm})

@app.route("/api/reset", methods=["POST"])
def reset_system():
    global play_queue, latest_stable_bpm, data_buffer, batch_bpm_list, zero_streak
    with _lock:
        play_queue.clear()
        latest_stable_bpm = 0
        data_buffer.clear()
        batch_bpm_list = []
        recent_bpms.clear()
        zero_streak = 0
    print("\n系統已重置：收到前端重新載入指令")
    return jsonify({"status": "ok"})

# [修正6] 新增手動刷新歌單 API
@app.route("/api/refresh_songs", methods=["POST"])
def refresh_songs():
    songs = get_all_songs_metadata(force_refresh=True)
    return jsonify({"status": "ok", "count": len(songs)})

# --- 音樂搜尋邏輯 ---
def find_songs_by_bpm(target_bpm):
    try:
        all_songs = get_all_songs_metadata()
        matched = []

        for song in all_songs:
            song_bpm = song['bpm']
            in_normal_range = (target_bpm - 10 <= song_bpm <= target_bpm + 10)

            in_half_range = False
            if target_bpm >= 160:
                half_target = target_bpm / 2
                in_half_range = (half_target - 5 <= song_bpm <= half_target + 5)

            if in_normal_range or in_half_range:
                matched.append(song)

        if matched:
            random.shuffle(matched)
        return matched
    except:
        return []

@app.route("/api/songs")
def api_songs():
    bpm = float(request.args.get('bpm', 0))
    songs = find_songs_by_bpm(bpm)
    # 推薦清單依 BPM 由低到高排序，方便使用者瀏覽
    songs_sorted = sorted(songs, key=lambda s: s['bpm'])
    return jsonify(songs_sorted)

@app.route("/api/lock_and_play", methods=["POST"])
def lock_and_play():
    global play_queue
    data = request.get_json()
    target_bpm = data.get("bpm", 120)

    matched_songs = find_songs_by_bpm(target_bpm)
    if matched_songs:
        song = random.choice(matched_songs)
        with _lock:
            play_queue.insert(0, song)
        return jsonify({"status": "ok", "song": song['title'], "bpm": song['bpm']})

    return jsonify({"status": "error", "message": "找不到符合範圍的音樂"}), 404

@app.route("/api/prepare_next", methods=["POST"])
def prepare_next():
    global latest_stable_bpm, play_queue
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "dynamic")

    with _lock:
        try:
            song = play_queue.pop(0)
        except IndexError:
            song = None

        current_bpm = latest_stable_bpm

    if not song and mode == 'dynamic':
        bpm = current_bpm if current_bpm > 0 else 120
        matched = find_songs_by_bpm(bpm)
        if matched:
            song = random.choice(matched)

    if not song:
        return jsonify({"error": "no song"}), 404

    # [修正2] 下載失敗要有完整錯誤回應
    temp_fn = f"temp_{song['file_id']}.mp3"
    temp_path = os.path.join(TEMP_FOLDER, temp_fn)

    if not os.path.exists(temp_path):
        if drive is None:
            return jsonify({"error": "Google Drive 未初始化"}), 503
        try:
            print(f"\n正在下載: {song['title']}")
            f = drive.CreateFile({'id': song['file_id']})
            f.GetContentFile(temp_path)
            print(f"下載完成: {song['title']}")
        except Exception as e:
            print(f"下載失敗: {e}")
            return jsonify({"error": f"下載失敗: {str(e)}"}), 503

    clean_temp_folder(max_files=10)
    return jsonify({"title": song['title'], "url": f"/music/{temp_fn}"})

@app.route("/api/play", methods=["POST"])
def api_play():
    global play_queue
    # [修正3] 加上 queue 長度上限
    with _lock:
        if len(play_queue) >= MAX_QUEUE_SIZE:
            return jsonify({"status": "error", "message": f"播放清單已達上限 ({MAX_QUEUE_SIZE} 首)"}), 429
        play_queue.append(request.json)
    return jsonify({"status": "ok"})

@app.route("/api/queue")
def get_queue():
    with _lock:
        q = list(play_queue)
    return jsonify({"upcoming": q})

@app.route("/api/queue/remove", methods=["POST"])
def remove_from_queue():
    global play_queue
    idx = request.json.get("index")
    with _lock:
        if idx is not None and 0 <= idx < len(play_queue):
            removed_song = play_queue.pop(idx)
            return jsonify({"status": "ok", "removed": removed_song["title"], "file_id": removed_song.get("file_id")})
    return jsonify({"status": "error"}), 400

if __name__ == "__main__":
    def get_local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except:
            return "127.0.0.1"

    local_ip = get_local_ip()
    print(f"\n電腦用網址: http://127.0.0.1:1000")
    print(f"手機用網址: http://{local_ip}:1000\n")

    threading.Thread(target=get_all_songs_metadata, daemon=True).start()
    app.run(host="0.0.0.0", port=1000)

