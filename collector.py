import asyncio
import json
import logging
import os
import math
import random
import time
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web
from telethon import TelegramClient, events

# === НАСТРОЙКИ ===
CHANNELS = ["me", "kyiv_airdef", "kyiv_sky_watch", "kyiv_airdefence", "kievdenger", "kievinform_ua1"]
API_ID = 34997598
API_HASH = os.getenv("TG_API_HASH", "2478fc8e7a7f32ed5710b8fd97051c99")
SESSION_NAME = "my_map_session"
PORT = int(os.getenv("PORT", 8000))
BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("radar")

client = TelegramClient(str(BASE_DIR / SESSION_NAME), API_ID, API_HASH)

KEYWORDS = ['балистика', 'балістика', 'баллистика', 'шахед', 'ракета', 'калібр', 'кинджал', 'искандер',
            'курс', 'движется', 'цілі', 'ціль', 'над', 'кр', 'х-101', 'х-59', 'с-300', 'с-400', 'ц-400',
            'бандерол', 's8000', 'реактивн', 'герань-3', 'герань 3', 'shahed-238', 'герань-5', 'герань 5',
            'крилат', 'бпла', 'перший', 'другий', 'третій', 'увага', 'уважно', 'тривога', 'далі',
            'висілки', 'висилки', 'высылки', 'групова', 'группа', 'на', 'в бік', 'один', 'два', 'три', 'десяток']

CLEAR_WORDS = ['відбій', 'отбой', 'загроза минула', 'чисто']
DESTROY_WORDS = ['знищено', 'збито', 'уничтожено', 'сбито', 'минус', 'знищення', 'не фіксується', 'не фиксируется', 'зник', 'пропав']

TYPE_KEYWORDS = {
    'ballistic': ['кинджал', 'искандер', 'баліст', 'баллист', 'балістика', 'балистика', 'с-300', 'с-400', 'ц-400'],
    'jet_drone': ['реактивн', 'герань-3', 'герань 3', 'герань-5', 'герань 5', 'shahed-238'],
    'banderol': ['бандерол', 's8000'],
    'cruise_missile': ['ракет', 'калібр', 'калибр', ' кр', 'х-101', 'х-59', 'х-555', 'крилат'],
    'drone': ['шахед', 'бпла', 'герань', 'shahed'],
}

SPEEDS = {'drone': 200, 'jet_drone': 550, 'cruise_missile': 850, 'ballistic': 10000, 'banderol': 550}
ORIGIN_POINTS = {
    'ballistic': [('курск', (51.7373, 36.1874)), ('миллерово', (48.9226, 40.4013)), ('крым', (45.3453, 34.4997))],
    'cruise_missile': [('брянск', (53.2434, 34.3639)), ('саратов', (51.5333, 46.0333))],
    'drone': [('курск', (51.7373, 36.1874)), ('брянск', (53.2434, 34.3639)), ('крым', (45.3453, 34.4997))]
}

CITIES = {
    'курск': (51.7373, 36.1874), 'брянск': (53.2434, 34.3639), 'миллерово': (48.9226, 40.4013),
    'крым': (45.3453, 34.4997), 'джанкой': (45.7086, 34.3931), 'беларусь': (53.7098, 27.9534),
    'мозырь': (52.0500, 29.2333), 'гомель': (52.4345, 30.9754), 'суми': (50.9077, 34.7981), 
    'харків': (49.9935, 36.2304), 'чернігів': (51.4982, 31.2893), 'полтава': (49.5883, 34.5514), 
    'дніпро': (48.4647, 35.0462), 'запоріжжя': (47.8388, 35.1396), 'київ': (50.4501, 30.5234), 
    'черкаси': (49.4444, 32.0598), 'вінниця': (49.2331, 28.4682), 'житомир': (50.2547, 28.6587),
    'одеса': (46.4825, 30.7233), 'миколаїв': (46.9750, 31.9946), 'херсон': (46.6354, 32.6169),
    'львів': (49.8397, 24.0297), 'луцьк': (50.7472, 25.3254), 'рівне': (50.6199, 26.2516),
    'тернопіль': (49.5535, 25.5948), 'івано-франківськ': (48.9226, 24.7103), 'ужгород': (48.6208, 22.2879),
    'чернівці': (48.2917, 25.9352), 'хмельницький': (49.4229, 26.9871),
    'дарницький': (50.4000, 30.6300), 'позняки': (50.4000, 30.6300), 'харківський масив': (50.3930, 30.6320),
    'осокорки': (50.3900, 30.6100), 'троєщина': (50.5100, 30.6100), 'оболонь': (50.5100, 30.5000),
    'поділ': (50.4700, 30.5200), 'солом\'янка': (50.4300, 30.4500), 'святошин': (50.4500, 30.3800),
    'печерськ': (50.4400, 30.5400), 'голосіїв': (50.3900, 30.5000), 'голосієво': (50.3900, 30.5000),
    'виноградарь': (50.5100, 30.4200), 'русанівка': (50.4400, 30.6000), 'лісовий': (50.4600, 30.6500),
    'вигурівщина': (50.5100, 30.6100),
    'бориспіль': (50.3500, 30.9500), 'бровари': (50.5100, 30.7900), 'біла церква': (49.7950, 30.1160),
    'ірпінь': (50.5200, 30.2500), 'буча': (50.5400, 30.2100), 'вишгород': (50.5800, 30.4800),
    'вишневе': (50.3800, 30.3700), 'фастів': (50.0700, 29.9100), 'обухів': (50.1100, 30.6300),
    'українка': (50.1500, 30.7400), 'переяслав': (50.0700, 31.4400), 'васильків': (50.1850, 30.3200),
    'сквира': (49.7300, 29.6600), 'узин': (49.8200, 30.4400), 'богуслав': (49.5400, 30.8700),
    'козин': (50.1800, 30.6500), 'ржищів': (49.9700, 30.9500)
}

if (BASE_DIR / "cities.json").exists():
    with open(BASE_DIR / "cities.json", "r", encoding="utf-8") as f:
        for row in json.load(f):
            name_lower = row["name"].lower()
            if name_lower not in CITIES:
                CITIES[name_lower] = (row["lat"], row["lon"])

active_targets = {}
target_counter = 1
clients = set()
history = []
last_context = {'type': 'drone', 'time': 0}

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat, dlon = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

async def broadcast(payload):
    if not clients: return
    encoded = json.dumps(payload, ensure_ascii=False)
    stale = set()
    for ws in list(clients):
        try: await ws.send_str(encoded)
        except Exception: stale.add(ws)
    clients.difference_update(stale)

async def flight_simulator():
    while True:
        now = datetime.now(timezone.utc)
        updated = False
        for tid in list(active_targets.keys()):
            data = active_targets[tid]
            
            if data['eta_mins'] <= 0:
                if data['type'] == 'ballistic' or (now - datetime.fromisoformat(data['last_update'])).total_seconds() > 600:
                    del active_targets[tid]; updated = True
                continue

            time_since_launch = (now - datetime.fromisoformat(data['launch_time'])).total_seconds()
            total_flight_time = data['total_flight_mins'] * 60
            if time_since_launch >= total_flight_time:
                data['eta_mins'] = 0; updated = True; continue

            progress = time_since_launch / total_flight_time
            start_lat, start_lng = data['origin_coords']
            end_lat, end_lng = data['dest_coords']
            
            data['prev_lat'], data['prev_lng'] = data['lat'], data['lng']
            data['lat'] = start_lat + (end_lat - start_lat) * progress
            data['lng'] = start_lng + (end_lng - start_lng) * progress
            data['eta_mins'] = max(0, round((total_flight_time - time_since_launch) / 60))
            data['last_update'] = now.isoformat()
            updated = True

        if updated: await broadcast({"type": "targets_update", "targets": list(active_targets.values())})
        await asyncio.sleep(1)

@client.on(events.NewMessage(chats=CHANNELS))
async def on_new_message(event):
    global target_counter, last_context
    text = event.message.message or ""
    text_lower = text.lower()
    now = datetime.now(timezone.utc)
    chat = await event.get_chat()
    channel_name = getattr(chat, 'username', getattr(chat, 'title', str(chat.id)))

    if any(w in text_lower for w in CLEAR_WORDS):
        active_targets.clear()
        last_context = {'type': 'drone', 'time': 0}
        await broadcast({"type": "targets_update", "targets": []})
        return

    if any(w in text_lower for w in DESTROY_WORDS):
        found_dests = [name for name in CITIES.keys() if name in text_lower]
        for dest in found_dests:
            for tid, data in list(active_targets.items()):
                if dest in data['name']:
                    del active_targets[tid]
        await broadcast({"type": "targets_update", "targets": list(active_targets.values())})
        return

    found_dests = [name for name in CITIES.keys() if name in text_lower]
    if not found_dests: return

    target_type = None
    for t, words in TYPE_KEYWORDS.items():
        if any(w in text_lower for w in words):
            target_type = t
            break
            
    if target_type:
        last_context = {'type': target_type, 'time': time.time()}
    else:
        if time.time() - last_context['time'] < 900:
            target_type = last_context['type']
        else:
            target_type = 'drone'

    origins = ORIGIN_POINTS.get(target_type, ORIGIN_POINTS['drone'])
    origin_loc, origin_coords = random.choice(origins)

    target_count = 1
    if any(w in text_lower for w in ['десяток', '10']): target_count = 10
    elif any(w in text_lower for w in ['групова', 'группа', 'багато']): target_count = 4
    elif any(w in text_lower for w in ['дві', 'два', '2']): target_count = 2
    elif any(w in text_lower for w in ['три', '3']): target_count = 3
    elif any(w in text_lower for w in ['чотири', '4']): target_count = 4
    elif any(w in text_lower for w in ['п\'ять', '5']): target_count = 5

    for dest_loc in found_dests:
        base_dest_coords = CITIES[dest_loc]
        
        for _ in range(target_count):
            off_lat = random.uniform(-0.05, 0.05) if target_count > 1 else 0
            off_lng = random.uniform(-0.05, 0.05) if target_count > 1 else 0
            dest_coords = (base_dest_coords[0] + off_lat, base_dest_coords[1] + off_lng)

            base_speed = SPEEDS.get(target_type, 200)
            dist = haversine(origin_coords[0], origin_coords[1], dest_coords[0], dest_coords[1])
            eta = max(1, round((dist / base_speed) * 60))

            new_id = f"t_{now.strftime('%H%M%S')}_{target_counter}"
            active_targets[new_id] = {
                'id': new_id, 'name': f"Ціль #{target_counter} ({dest_loc})", 
                'lat': origin_coords[0], 'lng': origin_coords[1],
                'prev_lat': None, 'prev_lng': None, 'last_update': now.isoformat(), 
                'type': target_type, 'text': text[:100], 'speed_kmh': base_speed, 'eta_mins': eta, 
                'origin': origin_loc, 'origin_coords': origin_coords, 'dest_coords': dest_coords,
                'total_flight_mins': eta, 'launch_time': now.isoformat(), 'channel': channel_name
            }
            target_counter += 1
            
            history.append({'region': dest_loc, 'type': target_type, 'source': channel_name, 'time': now.isoformat(), 'message': text[:100]})
            del history[:-300]

    await broadcast({"type": "targets_update", "targets": list(active_targets.values())})

# === AIOHTTP СЕРВЕР ===
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    clients.add(ws)
    try:
        await ws.send_str(json.dumps({"type": "init", "targets": list(active_targets.values()), "history": history}, ensure_ascii=False))
        async for msg in ws:
            pass
    finally:
        clients.discard(ws)
    return ws

async def index_handler(request):
    path = BASE_DIR / "map.html"
    if not path.exists():
        return web.Response(text="map.html not found", status=404)
    return web.FileResponse(path)

async def cities_handler(request):
    path = BASE_DIR / "cities.json"
    if not path.exists():
        return web.Response(text="cities.json not found. Run build_cities.py first.", status=404)
    return web.FileResponse(path)

def create_app():
    app = web.Application()
    app.router.add_get('/', index_handler)
    app.router.add_get('/map.html', index_handler)
    app.router.add_get('/cities.json', cities_handler)
    app.router.add_get('/ws', websocket_handler)
    return app

async def main():
    await client.start()
    log.info("Telegram connected. Listening to channels...")
    asyncio.create_task(flight_simulator())
    
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    log.info(f"Server started on port {PORT}")
    
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())