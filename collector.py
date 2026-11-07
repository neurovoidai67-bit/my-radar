import asyncio
import json
import logging
import math
import os
import random
import re
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import websockets
from telethon import TelegramClient, events

# === НАСТРОЙКИ ===
CHANNELS = ["me", "kyiv_airdef", "kyiv_sky_watch", "kyiv_airdefence", "kievdenger", "kievinform_ua1"]
API_ID = 34997598
API_HASH = os.getenv("TG_API_HASH", "2478fc8e7a7f32ed5710b8fd97051c99")
SESSION_NAME = "my_map_session"
HTTP_PORT = 8000
WS_PORT = 8765
BROADCAST_INTERVAL = 1.5
MAX_TARGETS_PER_MSG = 5
DEDUP_WINDOW = 60

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("radar")

client = TelegramClient(str(BASE_DIR / SESSION_NAME), API_ID, API_HASH)

# === СЛОВАРИ ===
CLEAR_WORDS = ['відбій', 'отбой', 'загроза минула', 'чисто']
DESTROY_WORDS = ['знищено', 'збито', 'уничтожено', 'сбито', 'минус', 'знищення',
                 'не фіксується', 'не фиксируется', 'зник', 'пропав']

TYPE_KEYWORDS = {
    'ballistic': ['кинджал', 'искандер', 'баліст', 'баллист', 'балістика', 'балистика', 
                  'с-300', 'с-400', 'ц-400', 'циркон', 'zircon', 'авангард', 'гиперзвук'],
    'jet_drone': ['реактивн', 'герань-3', 'герань 3', 'герань-5', 'герань 5', 'shahed-238', 'шахед-238'],
    'banderol': ['бандерол', 's8000'],
    'cruise_missile': ['ракет', 'калібр', 'калибр', ' кр', 'х-101', 'х-59', 'х-555', 'х-55', 'крилат', 
                       'нептун', 'скальп', 'scalp', 'шторм', 'storm shadow'],
    'drone': ['шахед', 'бпла', 'герань', 'shahed', 'мопед'],
}

SPEEDS = {
    'drone': 180,
    'jet_drone': 550,
    'cruise_missile': 850,
    'ballistic': 12000,
    'banderol': 550
}

DEFAULT_ORIGINS = {
    'ballistic': [('курск', (51.7373, 36.1874)), ('миллерово', (48.9226, 40.4013)), ('крым', (45.3453, 34.4997))],
    'cruise_missile': [('брянск', (53.2434, 34.3639)), ('саратов', (51.5333, 46.0333))],
    'drone': [('курск', (51.7373, 36.1874)), ('брянск', (53.2434, 34.3639)), ('крым', (45.3453, 34.4997)),
              ('приморско-ахтарск', (46.0417, 38.1747)), ('ейск', (46.7110, 38.2730))],
    'jet_drone': [('курск', (51.7373, 36.1874)), ('крым', (45.3453, 34.4997))],
    'banderol': [('курск', (51.7373, 36.1874)), ('крым', (45.3453, 34.4997))],
}

RUSSIAN_CITIES = ['курск', 'брянск', 'саратов', 'миллерово', 'приморско-ахтарск', 'ейск', 'белгород', 'воронеж', 'ростов']

# === ЗАГРУЗКА ГОРОДОВ ===
UKRAINE_CITIES = {}
CITY_STEMS = {}

if (BASE_DIR / "cities.json").exists():
    with open(BASE_DIR / "cities.json", "r", encoding="utf-8") as f:
        rows = json.load(f)
        for row in rows:
            if row.get("place_type") not in ("місто", "селище"):
                continue
            name_lower = row["name"].lower()
            if name_lower not in UKRAINE_CITIES:
                UKRAINE_CITIES[name_lower] = {
                    "lat": row["lat"], "lon": row["lon"], "region": row.get("region", "unknown"),
                    "place_type": row["place_type"], "full_name": row.get("full_name", row["name"]),
                }
                if len(name_lower) >= 5:
                    stem = name_lower[:5]
                    if stem not in CITY_STEMS:
                        CITY_STEMS[stem] = []
                    CITY_STEMS[stem].append(name_lower)
    log.info(f"Загружено {len(UKRAINE_CITIES)} городов из cities.json")
else:
    log.warning("cities.json не найден!")

def find_cities(text_lower: str):
    found = set()
    for name in UKRAINE_CITIES:
        if re.search(rf'\b{re.escape(name)}\b', text_lower):
            found.add(name)
    words = re.findall(r'\b[а-яіїєґa-z]{4,}\b', text_lower)
    for word in words:
        stem = word[:5]
        if stem in CITY_STEMS:
            for full_name in CITY_STEMS[stem]:
                if word.startswith(full_name[:4]):
                    found.add(full_name)
    for r_city in RUSSIAN_CITIES:
        if r_city in found:
            found.remove(r_city)
    return list(found)

# === СОСТОЯНИЕ ===
active_targets = {}
target_counter = 1
clients = set()
history = []
last_context = {'type': 'drone', 'time': 0}
state_lock = asyncio.Lock()
_last_sent = {}
pending_ballistic = {}

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

async def safe_send(ws, payload):
    try:
        await asyncio.wait_for(ws.send(payload), timeout=2.0)
    except Exception:
        clients.discard(ws)

async def broadcast(payload):
    if not clients: return
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    await asyncio.gather(*[safe_send(ws, encoded) for ws in list(clients)], return_exceptions=True)

# === СИМУЛЯТОР ПОЛЕТА ===
async def flight_simulator():
    while True:
        now = datetime.now(timezone.utc)
        async with state_lock:
            removed = []
            for tid in list(active_targets.keys()):
                data = active_targets[tid]

                if data['type'] == 'ballistic':
                    age = (now - datetime.fromisoformat(data['launch_time'])).total_seconds()
                    if age > 120:
                        del active_targets[tid]; removed.append(tid)
                    continue

                if data['eta_mins'] <= 0:
                    if (now - datetime.fromisoformat(data['last_update'])).total_seconds() > 600:
                        del active_targets[tid]; removed.append(tid)
                    continue

                time_since_launch = (now - datetime.fromisoformat(data['launch_time'])).total_seconds()
                total_flight_time = data['total_flight_mins'] * 60
                if time_since_launch >= total_flight_time:
                    data['eta_mins'] = 0; continue

                progress = time_since_launch / total_flight_time
                start_lat, start_lng = data['origin_coords']
                end_lat, end_lng = data['dest_coords']
                data['prev_lat'], data['prev_lng'] = data['lat'], data['lng']
                data['lat'] = start_lat + (end_lat - start_lat) * progress
                data['lng'] = start_lng + (end_lng - start_lng) * progress
                data['eta_mins'] = max(0, round((total_flight_time - time_since_launch) / 60))
                data['last_update'] = now.isoformat()

            changed = []
            for tid, data in active_targets.items():
                key = (round(data['lat'], 5), round(data['lng'], 5), data['eta_mins'], data['type'])
                if _last_sent.get(tid) != key:
                    _last_sent[tid] = key
                    changed.append(data)
            for tid in list(_last_sent.keys()):
                if tid not in active_targets:
                    del _last_sent[tid]

        if changed or removed:
            await broadcast({"type": "delta", "changed": changed, "removed": removed})
        await asyncio.sleep(BROADCAST_INTERVAL)

def detect_type(text_lower: str):
    for t, words in TYPE_KEYWORDS.items():
        if any(w in text_lower for w in words):
            return t
    return None

def parse_flight_direction(text_lower, found_cities):
    origins = []
    targets = []
    for city in found_cities:
        pattern_from = re.compile(rf'\b(с|з|со)\s+{re.escape(city)}')
        if pattern_from.search(text_lower):
            origins.append(city)
        else:
            targets.append(city)
    return origins, targets

# === ОБРАБОТКА СООБЩЕНИЙ ===
async def process_message(text: str, channel_name: str):
    global target_counter, last_context, pending_ballistic
    text_lower = text.lower()
    now = datetime.now(timezone.utc)

    if any(w in text_lower for w in CLEAR_WORDS):
        found = find_cities(text_lower)
        async with state_lock:
            pending_ballistic.clear()
            if not found:
                removed = list(active_targets.keys())
                active_targets.clear()
                _last_sent.clear()
            else:
                removed = []
                for tid, data in list(active_targets.items()):
                    if any(f in data['name'].lower() for f in found):
                        del active_targets[tid]
                        removed.append(tid)
        await broadcast({"type": "delta", "changed": [], "removed": removed})
        return

    if any(w in text_lower for w in DESTROY_WORDS):
        found = find_cities(text_lower)
        removed = []
        async with state_lock:
            for tid, data in list(active_targets.items()):
                if any(f in data['name'].lower() for f in found):
                    del active_targets[tid]; removed.append(tid)
        await broadcast({"type": "delta", "changed": [], "removed": removed})
        return

    found_dests = find_cities(text_lower)
    ttype = detect_type(text_lower)

    if ttype == 'ballistic' and not found_dests:
        origin_cities, _ = parse_flight_direction(text_lower, find_cities(text_lower))
        if origin_cities:
            origin_loc = origin_cities[0]
            pending_ballistic[origin_loc] = time.time()
            log.info(f"Помечена баллистика из {origin_loc}. Ждем конечную точку.")
        return

    if not found_dests: return

    origin_cities, target_cities = parse_flight_direction(text_lower, found_dests)

    if not origin_cities and 'ballistic' in pending_ballistic:
        origin_loc = list(pending_ballistic.keys())[-1]
        origin_cities = [origin_loc]

    if origin_cities and not target_cities:
        target_cities = ['київ']
    if not target_cities:
        return

    if not ttype and not any(w in text_lower for w in ["курс", "летить", "в бік", "направляється", "увага"]):
        return

    if ttype:
        last_context = {'type': ttype, 'time': time.time()}
    else:
        ttype = last_context['type'] if time.time() - last_context['time'] < 900 else 'drone'

    if origin_cities:
        origin_loc = origin_cities[0]
        origin_data = UKRAINE_CITIES.get(origin_loc)
        if origin_data:
            origin_coords = (origin_data["lat"], origin_data["lon"])
        else:
            origin_coords = random.choice(DEFAULT_ORIGINS.get(ttype, DEFAULT_ORIGINS['drone']))[1]
    else:
        origin_loc, origin_coords = random.choice(DEFAULT_ORIGINS.get(ttype, DEFAULT_ORIGINS['drone']))

    created = []
    async with state_lock:
        for tid, data in list(active_targets.items()):
            age = (now - datetime.fromisoformat(data['launch_time'])).total_seconds()
            if age > 7200:
                del active_targets[tid]
                removed.append(tid)

        for dest_loc in target_cities[:MAX_TARGETS_PER_MSG]:
            dest_data = UKRAINE_CITIES.get(dest_loc)
            if not dest_data: continue

            dup = None
            for tid, data in active_targets.items():
                if data['name'].endswith(f"({dest_loc})") and data['type'] == ttype:
                    age = (now - datetime.fromisoformat(data['launch_time'])).total_seconds()
                    if age < DEDUP_WINDOW:
                        dup = tid; break
            if dup:
                active_targets[dup]['last_update'] = now.isoformat()
                active_targets[dup]['text'] = text[:100]
                continue

            dest_coords = (dest_data["lat"], dest_data["lon"])
            region = dest_data["region"]
            base_speed = SPEEDS.get(ttype, 200)
            launch_time = now.isoformat()
            time_in_air = 0

            if ttype == 'ballistic' and origin_loc in pending_ballistic:
                launch_time_dt = datetime.fromtimestamp(pending_ballistic[origin_loc], timezone.utc)
                time_in_air = (now - launch_time_dt).total_seconds() / 60
                
                if time_in_air > 3.0:
                    del pending_ballistic[origin_loc]
                    continue
                
                launch_time = launch_time_dt.isoformat()
                del pending_ballistic[origin_loc]
                
                progress = min(0.8, time_in_air / 2.0)
                origin_coords = (
                    origin_coords[0] + (dest_coords[0] - origin_coords[0]) * progress,
                    origin_coords[1] + (dest_coords[1] - origin_coords[1]) * progress
                )

            if ttype != 'ballistic':
                if origin_loc in ['брянск', 'курск', 'саратов', 'миллерово', 'приморско-ахтарск', 'ейск']:
                    if origin_loc == 'брянск':
                        shift_lat = random.uniform(51.0, 51.8)
                        shift_lng = random.uniform(31.0, 32.5)
                    elif origin_loc == 'курск':
                        shift_lat = random.uniform(50.0, 51.0)
                        shift_lng = random.uniform(34.0, 35.5)
                    else:
                        shift_lat = random.uniform(47.0, 48.0)
                        shift_lng = random.uniform(33.0, 35.0)
                    origin_coords = (shift_lat, shift_lng)
                    origin_loc = "кордон"
            
            jitter_lat = random.uniform(-0.15, 0.15)
            jitter_lng = random.uniform(-0.15, 0.15)
            adj_origin = (origin_coords[0] + jitter_lat, origin_coords[1] + jitter_lng)
            adj_dest = (dest_coords[0] + jitter_lat, dest_coords[1] + jitter_lng)

            if origin_loc == dest_loc:
                eta = 1
            else:
                dist = haversine(adj_origin[0], adj_origin[1], adj_dest[0], adj_dest[1])
                eta = max(1, round((dist / base_speed) * 60))
                if ttype == 'ballistic' and time_in_air > 0:
                    eta = max(1, eta - int(time_in_air))

            new_id = f"t_{now.strftime('%H%M%S')}_{target_counter}"
            active_targets[new_id] = {
                'id': new_id,
                'name': f"Ціль #{target_counter} ({dest_data['full_name']})",
                'lat': adj_origin[0], 'lng': adj_origin[1],
                'prev_lat': None, 'prev_lng': None, 'last_update': now.isoformat(),
                'type': ttype, 'text': text[:100], 'speed_kmh': base_speed, 'eta_mins': eta,
                'origin': origin_loc, 'origin_coords': adj_origin, 'dest_coords': adj_dest,
                'total_flight_mins': eta, 'launch_time': launch_time, 'channel': channel_name,
                'region': region, 'place_type': dest_data['place_type'],
            }
            target_counter += 1
            created.append(new_id)

            history.append({'region': dest_loc, 'type': ttype, 'source': channel_name,
                            'time': now.isoformat(), 'message': text[:100]})
            if len(history) > 300: del history[:-300]

    if created:
        async with state_lock:
            new_objs = [active_targets[tid] for tid in created if tid in active_targets]
        await broadcast({"type": "delta", "changed": new_objs, "removed": []})

@client.on(events.NewMessage(chats=CHANNELS))
async def on_new_message(event):
    text = event.message.message or ""
    chat = await event.get_chat()
    channel_name = getattr(chat, 'username', getattr(chat, 'title', str(chat.id)))
    try:
        await process_message(text, channel_name)
    except Exception as e:
        log.exception("process_message error: %s", e)

async def websocket_handler(websocket, *args):
    clients.add(websocket)
    try:
        async with state_lock:
            snap = list(active_targets.values())
        await websocket.send(json.dumps({"type": "init", "targets": snap, "history": history[-50:]}, ensure_ascii=False, default=str))
        await websocket.wait_closed()
    finally:
        clients.discard(websocket)

class RequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/map.html"):
            path = BASE_DIR / "map.html"
            if not path.exists(): return self.send_error(404)
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
            self.wfile.write(path.read_bytes())
        elif self.path == "/cities.json":
            path = BASE_DIR / "cities.json"
            if not path.exists(): return self.send_error(404, "cities.json not found")
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers()
            self.wfile.write(path.read_bytes())
        elif self.path == "/ukraine_regions.geojson":
            path = BASE_DIR / "ukraine_regions.geojson"
            if not path.exists(): return self.send_error(404, "No geojson file")
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.end_headers()
            self.wfile.write(path.read_bytes())
        else: self.send_error(404)
    def log_message(self, *args): pass

def start_http_server():
    ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), RequestHandler).serve_forever()

async def main():
    Thread(target=start_http_server, daemon=True).start()
    await client.start()
    log.info("Telegram connected. Listening to channels...")
    async with websockets.serve(websocket_handler, "0.0.0.0", WS_PORT):
        log.info(f"WebSocket listening on ws://0.0.0.0:{WS_PORT}")
        asyncio.create_task(flight_simulator())
        await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())
