# todo
# + boot logo
# + boot test led
# + send telegram info
# + send telegram warning and error
# + LED logic
# + calculate RMS current



import board
import busio
import json
import math
import requests
import socket
import sqlite3
import struct
import threading
import time
from datetime import datetime, timedelta
from PIL import Image, ImageDraw, ImageFont
from enum import Enum
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from requests.exceptions import Timeout, ConnectionError, HTTPError, RequestException
from requests_auth_aws_sigv4 import AWSSigV4
from urllib.parse import quote

import adafruit_ssd1306
from adafruit_bus_device.i2c_device import I2CDevice
from gpiozero import LEDBoard

# Credentials live in cmon_secrets.py next to this file (git-ignored).
# Copy cmon_secrets.example.py to cmon_secrets.py and fill it in.
from cmon_secrets import AWS_ACCESS_KEY, AWS_SECRET_KEY, LAMBDA_URL, TELEGRAM_TOKEN

#### Start AWS Lambda related code ####
ACCESS_KEY = AWS_ACCESS_KEY
SECRET_KEY = AWS_SECRET_KEY
URL = LAMBDA_URL
REGION = "eu-central-1"

auth = AWSSigV4('lambda',
    region=REGION,
    aws_access_key_id=ACCESS_KEY,
    aws_secret_access_key=SECRET_KEY
)

def log_data():
    print("log_data worker started...")
    while True:
        try:
            time.sleep(300)
            payload = {
                "device_id": "VRP1",
                "L1": int(values[0]),
                "L2": int(values[1]),
                "L3": int(values[2])
            }
            response = requests.post(URL, json=payload, auth=auth)
            print(f"Status: {response.status_code}, Body: {response.text}")
        except (RequestException, ValueError, HTTPError, ConnectionError) as e:
            print(e)
            time.sleep(600)


#### End AWS lambda related code ####

#### Start Telegram related code ####
# Debug TG: https://api.telegram.org/bot<TELEGRAM_TOKEN>/getUpdates
class Level(Enum):
    INFO = "ℹ️"
    WARNING = "⚠️"
    CRITICAL = "❗"

TOKEN = TELEGRAM_TOKEN
BASE_URL = f"https://api.telegram.org/bot{TOKEN}/"
NAME = "BristolInformBot"
DB_NAME = "cmon.db"
COOLDOWN = {Level.INFO: 24*60*60, Level.WARNING: 30*60, Level.CRITICAL: 15*60}
last_msg = {Level.INFO: datetime.now() - timedelta(hours=1), Level.WARNING: datetime.now()-timedelta(hours=1), Level.CRITICAL: datetime.now()-timedelta(hours=1)}

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS chats (chat_id INTEGER PRIMARY KEY)")
        conn.commit()


def update_chats():
    """Check for new messages and save chats to DB"""
    try:
        results = requests.get(BASE_URL + "getUpdates").json().get("result", [])
        with sqlite3.connect(DB_NAME) as conn:
            for res in results:
                chat_id = None
                if "message" in res:
                    chat_id = res["message"]["chat"]["id"]
                elif "my_chat_member" in res:
                    chat_id = res["my_chat_member"]["chat"]["id"]
                
                if chat_id:
                    conn.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
                    conn.commit()
    except Exception as e:
        print(f"Error during DB update {e}")

def format_value(values, esp_values):
    labels = ["L1", "L2", "L3"]
    lines = [f"{l}: {d:.0f} A" for l, d in zip(labels, values)]
    esp_lines = [f"{l}: {d:.0f} A" for l, d in zip(labels, esp_values)]
    return "Південний ввід \n" + "\n".join(lines) + "\n\nПівнічний ввід \n" + "\n".join(esp_lines)

def notify(values, esp_values):
    level = Level.INFO
    for value in values + esp_values:
        if value > CRITICAL_THRESHOLD:
            level = Level.CRITICAL
            break
        elif value > WARNING_THRESHOLD:
            level = Level.WARNING
            break

    msg = level.value + level.name + "\n" + format_value(values, esp_values)

    now = datetime.now()
    if (now - last_msg[level]).total_seconds() > COOLDOWN[level]:
        broadcast(msg)
        last_msg[level] = now


def broadcast(message):
    """Send message to all known chats"""
    with sqlite3.connect(DB_NAME) as conn:
        chats = conn.execute("SELECT chat_id FROM chats").fetchall()
    
    for (chat_id,) in chats:
        url = BASE_URL + "sendMessage"
        requests.post(url, data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"})

def set_commands():
    """Register the bot menu. Replaces the whole list, so every command goes here."""
    commands = [
        {"command": "load", "description": "Струм по вводах"},
        {"command": "genset", "description": "Рівень палива генератора"},
    ]
    try:
        requests.post(BASE_URL + "setMyCommands", json={"commands": commands}, timeout=10)
    except RequestException as e:
        print(f"Failed to set bot commands: {e}")

def tg_worker():
    """backgroud thread for Telegram Long Polling"""
    offset = None
    print("Telegram worker started...")
    while True:
        try:
            # Long Polling не заважає основному потоку
            response = requests.get(BASE_URL + "getUpdates", 
                                    params={"offset": offset, "timeout": 10}, 
                                    timeout=15)
            updates = response.json()

            if "result" in updates:
                for update in updates["result"]:
                    offset = update["update_id"] + 1
                    chat_id = None

                    if "message" in update:
                        chat_id = update["message"]["chat"]["id"]
                        text = update["message"].get("text")
                        with sqlite3.connect(DB_NAME) as conn:
                            conn.execute("INSERT OR IGNORE INTO chats (chat_id) VALUES (?)", (chat_id,))
                            conn.commit()


                    if "message" in update and "text" in update["message"]:
                        chat_id = update["message"]["chat"]["id"]
                        text = update["message"]["text"]
                        
                        if text == "/load":
                            requests.post(BASE_URL + "sendMessage",
                                          json={"chat_id": chat_id, "text": format_value(values, esp_values)})

                        elif text.strip().lower().split("@")[0] in ("genset", "/genset"):
                            requests.post(BASE_URL + "sendMessage",
                                          json={"chat_id": chat_id, "text": format_genset()})


        except Timeout:
            # expected exception during Long Polling. just proceed
            continue
        except (RequestException, ValueError, HTTPError, ConnectionError) as e:
            print(e)
            time.sleep(30)

#### End Telegram related code ####

#### Start HTTP API code ####
# Read-only public JSON endpoint (for a Home Assistant rest sensor and anyone else):
#   GET /values -> {"south": {"L1": 12.3, "L2": 4.5, "L3": null},
#                   "north": {"L1": 10.1, "L2": 9.8, "L3": 11.0},
#                   "updated": "2026-11-03T12:00:00+02:00"}
# south = Pi (local ADS1115), north = ESP32. Channels that are not read yet or
# failed (negative sentinels) are null.

API_PORT = 8080
values_updated = None

class ValuesHandler(BaseHTTPRequestHandler):
    timeout = 10  # drop idle connections so slow clients can't pile up threads

    def do_GET(self):
        if self.path.split("?")[0] != "/values":
            self.send_error(404)
            return
        def channels(vals):
            return {label: round(v, 1) if v >= 0 else None for label, v in zip(("L1", "L2", "L3"), vals)}

        body = json.dumps({
            "south": channels(values),
            "north": channels(esp_values),
            "updated": values_updated,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # don't print every request that reaches a public port

def api_worker():
    server = ThreadingHTTPServer(("0.0.0.0", API_PORT), ValuesHandler)
    server.daemon_threads = True
    print(f"HTTP API listening on :{API_PORT}")
    server.serve_forever()

#### End HTTP API code ####


# ---------- I2C ----------
i2c = busio.I2C(board.SCL, board.SDA)


#### Start OLED related code ####

WIDTH = 128
HEIGHT = 64
ROTATION = 270
line_height = 42 # HEIGHT / 3
font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 24)

def init_oled(i2c, width, height):
    """Ініціалізація дисплея через стандартну бібліотеку"""
    try:
        # Спроба створити об'єкт OLED. Якщо пристрій не на шині, виникне ValueError
        disp = adafruit_ssd1306.SSD1306_I2C(width, height, i2c, addr=0x3C)
        disp.fill(0)
        disp.show()
        return disp
    except (ValueError, OSError) as e:
        print(f"OLED не знайдено на шині: {e}")
        return None
oled = init_oled(i2c, WIDTH, HEIGHT)


def render(image):
    if oled:
        try:
            oled.image(image)
            oled.show()
        except OSError:
            print("Connection with OLED lost")


def display(values):
    image = Image.new("1", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image) 
    for i, value in enumerate(values):
        text = f"{round(value,2 ):.3g}"
        #print(text)
        txt_img = Image.new("1", (HEIGHT, line_height))
        txt_draw = ImageDraw.Draw(txt_img)
        txt_draw.text((0, 0), text, font=font, fill=255)
        rotated_txt = txt_img.rotate(ROTATION, expand=True)
        bbox = font.getbbox(text)
        image.paste(rotated_txt, (1 + i * line_height,  0))
    render(image)


def logo():
    image = Image.new("1", (WIDTH, HEIGHT))

    linewidth = 4;
    logo_img = Image.new("1", (HEIGHT, HEIGHT))
    logo = ImageDraw.Draw(logo_img)
    logo.line(( 1,  1,  1, 63), fill=255, width=linewidth )
    logo.line((62,  1, 62, 63), fill=255, width=linewidth )
    logo.line(( 1, 32, 63, 32), fill=255, width=linewidth )
    logo.line((12, 48, 52, 48), fill=255, width=linewidth )
    logo.line((18, 60, 45, 60), fill=255, width=linewidth )
    logo.line((32,  1, 32, 28), fill=255, width=linewidth )
    image.paste(logo_img.rotate(ROTATION, expand=True), (60, 0))

    txt_img = Image.new("1", (HEIGHT + 4, 10))
    txt_draw = ImageDraw.Draw(txt_img)
    txt_draw.text((0, 0), "NSH.COM.UA",  fill=255)
    rotated_txt = txt_img.rotate(ROTATION, expand=True)
    image.paste(rotated_txt, (0, -1))
    render(image)

#### End OLED related code ####

#### Start ADS1115 related code ####

VOLTS_PER_LSB = 0.000125  # GAIN 1
SENSITIVITY = 0.0015625   # 0.625V / 400A
I2C_ADDR = 0x48
CHANNELS = {"A1": 0x52E3, "A2": 0x62E3, "A3": 0x72E3}

ads_device = I2CDevice(i2c, I2C_ADDR)
reg_pointer = bytearray([0x00])
result_buffer = bytearray(2)

def set_channel(config_val):
    with ads_device as device:
        device.write(struct.pack(">BH", 0x01, config_val))
    time.sleep(0.1)

def get_rms(duration=0.2):
    samples = []
    with ads_device as device:
        start_time = time.monotonic()
        while (time.monotonic() - start_time) < duration:
            device.write_then_readinto(reg_pointer, result_buffer)
            val = struct.unpack(">h", result_buffer)[0]
            samples.append(val)
            time.sleep(0.0005)
            
    if len(samples) < 50: return None

    # 1. Напруга зміщення (Offset)
    avg_raw = sum(samples) / len(samples)
    offset_v = avg_raw * VOLTS_PER_LSB

    # 2. Розрахунок реального RMS
    sum_sq = 0
    for s in samples:
        i = ((s - avg_raw) * VOLTS_PER_LSB) / SENSITIVITY
        sum_sq += i**2
    rms_calc = math.sqrt(sum_sq / len(samples))

    # 3. Математичний RMS (ідеальна синусоїда)
    # Peak = (Max - Min) / 2
    peak_raw = (max(samples) - min(samples)) / 2
    peak_amps = (peak_raw * VOLTS_PER_LSB) / SENSITIVITY
    rms_ideal = peak_amps / math.sqrt(2)

    # 4. Дельта (різниця між реальним та ідеальним синусом)
    delta = rms_calc - rms_ideal

    return {
        "rms": rms_calc,
        "offset": offset_v,
        "delta": delta,
        "pts": len(samples)
    }

def read_voltages():
    rms_results = []
    for name, config in CHANNELS.items():
        set_channel(config)
        res = get_rms()
        if res:
            rms_results.append(res['rms'])
            print(f"[{time.strftime('%H:%M:%S')}] {name}: {res['rms']:6.2f}A | "
                  f"Offset: {res['offset']:.3f}V | "
                  f"Delta_Ideal: {res['delta']:+5.2f}A | Pts: {res['pts']}")
        else:
            rms_results.append(-1)
    return rms_results

#### End ADS related code ####

#### Start ESP32 remote sensor code ####
# Reads the ct_clamp sensors exposed by esp_cmon.yaml over ESPHome's
# web_server REST API (http://<host>/sensor/<entity>). ESPHome matches the
# URL segment against the sensor's exact "name:" from the yaml, so it must
# be URL-quoted rather than turned into a lowercase/underscore slug.

ESP_CMON_HOST = "192.168.10.37"
ESP_CMON_CHANNELS = ["Current A1", "Current A2", "Current A3"]

def read_esp_cmon_values(timeout=5):
    """Fetch A1/A2/A3 current readings (amps) from the ESP32 ct_clamp sensors."""
    results = []
    for entity in ESP_CMON_CHANNELS:
        try:
            url = f"http://{ESP_CMON_HOST}/sensor/{quote(entity)}"
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
            results.append(resp.json()["value"])
        except (RequestException, ValueError, HTTPError, ConnectionError, KeyError) as e:
            print(f"ESP cmon read failed for {entity}: {e}")
            results.append(-1)
    return results

#### End ESP32 remote sensor code ####

#### Start Datakom genset code ####
# Reads the fuel level from the Datakom D-300 over Modbus TCP (function 3,
# holding registers). The register was found by scanning the live-data area
# (2048+); the raw value is tenths of a percent.

DATAKOM_HOST = "192.168.10.68"
DATAKOM_PORT = 502
DATAKOM_UNIT_ID = 1
DATAKOM_FUEL_REGISTER = 2163
DATAKOM_FUEL_SCALE = 0.1

def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by device")
        buf += chunk
    return buf

def read_datakom_fuel(timeout=5):
    """Fetch the genset fuel level in percent, or None if the Datakom is unreachable."""
    try:
        with socket.create_connection((DATAKOM_HOST, DATAKOM_PORT), timeout=timeout) as sock:
            sock.sendall(struct.pack(">HHHBBHH", 1, 0, 6, DATAKOM_UNIT_ID, 3, DATAKOM_FUEL_REGISTER, 1))
            _tid, _proto, length, _unit = struct.unpack(">HHHB", recv_exact(sock, 7))
            body = recv_exact(sock, length - 1)
        if body[0] != 3 or body[1] != 2:
            raise ValueError(f"modbus error response {body.hex()}")
        return struct.unpack(">H", body[2:4])[0] * DATAKOM_FUEL_SCALE
    except (OSError, ValueError, struct.error) as e:
        print(f"Datakom read failed: {e}")
        return None

def format_genset():
    fuel = read_datakom_fuel()
    if fuel is None:
        return "Генератор\nDatakom недоступний"
    return f"Генератор\nПаливо: {fuel:.0f} %"

#### End Datakom genset code ####

#### Start Current Monitor code ####

WARNING_THRESHOLD = 200
CRITICAL_THRESHOLD = 300

pins = LEDBoard(26, 6, 13) #L1:RED L2:BLUE L3:GREEN
values = [-3, -3, -3]
esp_values = [-3, -3, -3]

def cleanup():
    print("Очищення ресурсів...")
    if oled:
        oled.fill(0)
        oled.show()

def main():
    global values_updated
    try:
        logo()
        for pin in pins:
            pin.on()
            time.sleep(0.5)
        for pin in pins:
            pin.off()
            time.sleep(0.5)

        init_db()
        set_commands()
        threading.Thread(target=tg_worker, daemon=True).start()
        threading.Thread(target=log_data, daemon=True).start()
        threading.Thread(target=api_worker, daemon=True).start()

        while True:
            values[:] = read_voltages()
            esp_values[:] = read_esp_cmon_values()
            values_updated = datetime.now().astimezone().isoformat(timespec="seconds")
            print(f"ESP cmon: A1:{esp_values[0]:6.2f} A2:{esp_values[1]:6.2f} A3:{esp_values[2]:6.2f}")
            notify(values, esp_values)
            display(values)
            for i, value in enumerate(values):
                if (value > CRITICAL_THRESHOLD):
                    pins[i].on()
                elif (value > WARNING_THRESHOLD):
                    pins[i].blink()
                else:
                    pins[i].off()
            time.sleep(30)

            print("")
        

    except KeyboardInterrupt:
        print("\n[!] Зупинка користувачем (Ctrl+C)")
    except Exception as e:
        print(f"\n[!] Критична помилка: {e}")
    finally:
        # Цей блок виконається ЗАВЖДИ при виході
        cleanup()


if __name__ == "__main__":
    main()


