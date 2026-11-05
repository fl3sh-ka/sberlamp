#!/usr/bin/env python3
"""Управление умной лампой Sber из терминала.

Работает через неофициальный облачный API приложения «Салют!».
Локального протокола у Wi-Fi-ламп Sber нет.
"""

import argparse
import asyncio
import base64
import datetime
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import aiohttp
from aiohttp import ClientSession, ClientTimeout, TCPConnector

CONFIG_DIR = Path(os.environ.get("SBERLAMP_CONFIG_DIR", Path.home() / ".config" / "sberlamp"))
CONFIG_PATH = CONFIG_DIR / "config.json"
TOKEN_PATH = CONFIG_DIR / "refresh_token"

GATEWAY = "https://gateway.iot.sberdevices.ru/gateway/v1/"
UAPI = "https://online.sberbank.ru/CSAFront/uapi/v2/"
TOKEN_URL = "https://online.sberbank.ru:4431/CSAFront/api/service/oidc/v3/token"
SMARTHOME_TOKEN_URL = "https://mp-prom.salutehome.ru/v13/smarthome/token"
SALUTE_UA = "Salute+prod%2F24.08.1.15602+%28Android+34%3B+Google+sdk_gphone64_arm64%29"
CLIENT_ID = "b1f0f0c6-fcb0-4ece-8374-6b614ebe3d42"
REDIRECT_URI = "companionapp://host"

# Отпечаток «браузера» для CSAFront: без него authenticate отвечает ошибкой.
RSA_DATA = (
    "dom_elements=&htmlinjection=&manvsmachinedetection=&js_events=&deviceprint=version=1.7.3"
    "&pm_br=Chrome&pm_brmjv=120&iframed=0&intip=&pm_expt=&pm_fpacn=Mozilla&pm_fpan=Netscape"
    "&pm_fpasw=&pm_fpco=1&pm_fpjv=0&pm_fpln=lang=ru|syslang=|userlang=&pm_fpol=true&pm_fposp="
    "&pm_fpsaw=393&pm_fpsbd=&pm_fpsc=24|393|786|786&pm_fpsdx=&pm_fpsdy=&pm_fpslx=&pm_fpsly="
    "&pm_fpspd=24&pm_fpsui=&pm_fpsw=&pm_fptz=3&pm_fpua=mozilla/5.0 (linux; android 10; k) "
    "applewebkit/537.36 (khtml, like gecko) chrome/120.0.0.0 mobile safari/537.36|5.0 (Linux; "
    "Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
    "|Linux armv81&pm_fpup=&pm_inpt=&pm_os=Android&adsblock=0=false|1=false|2=false|3=false|4=false"
    "&audio=baseLatency=0.003|outputLatency=0|sampleRate=48000|state=suspended|maxChannelCount=2"
    "|numberOfInputs=1|numberOfOutputs=1|channelCount=2|channelCountMode=max"
    "|channelInterpretation=speakers|fftSize=2048|frequencyBinCount=1024|minDecibels=-100"
    "|maxDecibels=-30|smoothingTimeConstant=0.8&pm_fpsfse=true&webgl=ver=webgl2|vendor=Google Inc. "
    "(Qualcomm)|render=ANGLE (Qualcomm, Adreno (TM) 630, OpenGL ES 3.2)"
)

SCENES = ["candle", "arctic", "romantic", "sunset", "dawn", "christmas", "fito"]
MODE_NAMES = {
    "white": "белый",
    "colour": "цветной",
    "color": "цветной",
    "scene": "сцена",
    "music": "музыка",
    "adaptive": "адаптивный",
}
SCENE_NAMES = {
    "candle": "свеча",
    "arctic": "арктика",
    "romantic": "романтика",
    "sunset": "закат",
    "dawn": "рассвет",
    "christmas": "рождество",
    "fito": "фитосвет",
}


# ---------- конфиг ----------

def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}


def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)
    if TOKEN_PATH.exists():
        TOKEN_PATH.chmod(0o600)


def save_config(cfg: dict) -> None:
    ensure_config_dir()
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
    CONFIG_PATH.chmod(0o600)


# ---------- сеть ----------

class LampError(Exception):
    """Общая ошибка работы с облаком Сбера."""


class AuthError(LampError):
    """Токен отсутствует или отвергнут - нужен lamp setup."""


class NetworkError(LampError):
    """Облако не отвечает: таймаут, обрыв, DNS."""


class ApiError(LampError):
    """Облако ответило ошибкой (HTTP-код + текст)."""

    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(message)


def jwt_expired(token) -> bool:
    """Проверяет exp в JWT без проверки подписи (PyJWT не нужен)."""
    if not token:
        return True
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError):
        return True
    expires = data.get("exp")
    return expires is not None and expires < time.time()


class SberLampAPI:
    """Клиент облака Сбера: SMS-вход, токены и запросы к gateway."""

    def __init__(self, refresh_token_path=None, level=logging.CRITICAL, timeout: int = 30):
        self.refresh_token_path = refresh_token_path
        self._refresh_token = None
        self._access_token = None
        self._smarthome_token = None
        self._code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
        digest = hashlib.sha256(self._code_verifier.encode()).digest()
        self._oidc = {
            "scope": "openid",
            "response_type": "code",
            "redirect_uri": REDIRECT_URI,
            "state": str(uuid.uuid4()),
            "nonce": str(uuid.uuid4()),
            "code_challenge_method": "S256",
            "client_id": CLIENT_ID,
            "code_challenge": base64.urlsafe_b64encode(digest).rstrip(b"=").decode(),
            "referer_uri": "",
        }
        self._session = ClientSession(connector=TCPConnector(ssl=False),
                                      timeout=ClientTimeout(total=timeout))

    async def close(self) -> None:
        await self._session.close()

    # --- транспорт ---

    async def _raw(self, url: str, method: str = "GET", json=None, data=None,
                   headers=None) -> dict:
        try:
            async with self._session.request(method, url, json=json, data=data,
                                             headers=headers) as response:
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError):
                    payload = await response.text()
                if response.status != 200:
                    raise ApiError(response.status, _error_text(payload, response))
                return payload
        except (aiohttp.ClientError, TimeoutError) as error:
            raise NetworkError(str(error) or type(error).__name__) from error

    def _uapi_headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "*/*",
            "Referer": UAPI,
            "User-Agent": SALUTE_UA,
        }

    # --- токены ---

    def _save_refresh_token(self, token: str) -> None:
        self._refresh_token = token
        if self.refresh_token_path:
            self.refresh_token_path.write_text(token)
            self.refresh_token_path.chmod(0o600)

    def _load_refresh_token(self) -> None:
        if not self._refresh_token and self.refresh_token_path:
            try:
                self._refresh_token = self.refresh_token_path.read_text().strip()
            except FileNotFoundError:
                pass
        if not self._refresh_token:
            raise AuthError("нет сохранённого токена")

    async def _token_request(self, data: dict) -> dict:
        answer = await self._raw(
            TOKEN_URL, method="POST", data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        self._access_token = answer.get("access_token")
        refresh = answer.get("refresh_token")
        if refresh:
            self._save_refresh_token(refresh)
        return answer

    async def _refresh_access_token(self) -> None:
        self._load_refresh_token()
        await self._token_request({
            "client_id": CLIENT_ID,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        })

    async def _ensure_smarthome_token(self) -> None:
        if not jwt_expired(self._smarthome_token):
            return
        if jwt_expired(self._access_token):
            await self._refresh_access_token()
        answer = await self._raw(
            SMARTHOME_TOKEN_URL,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "User-Agent": SALUTE_UA,
                "Accept": "application/json",
            },
        )
        token = answer.get("token") or answer.get("access_token")
        if not token:
            raise AuthError(f"облако не выдало SmartHome-токен: {answer}")
        self._smarthome_token = token

    # --- публичный запрос ---

    async def _send_request(self, url: str, method: str = "GET", json=None, data=None,
                            headers=None) -> dict:
        if headers is None:
            await self._ensure_smarthome_token()
            headers = {
                "x-auth-jwt": self._smarthome_token,
                "User-Agent": SALUTE_UA,
                "Accept": "application/json",
                "x-trace-id": uuid.uuid4().hex,
            }
        for attempt in (1, 2):
            try:
                return await self._raw(url, method=method, json=json, data=data,
                                       headers=headers)
            except ApiError as error:
                if attempt == 2 or error.status != 401:
                    raise
                # токен протух между проверкой и запросом - обновляем и повторяем
                self._smarthome_token = None
                self._access_token = None
                await self._ensure_smarthome_token()
                headers = dict(headers, **{"x-auth-jwt": self._smarthome_token})

    # --- вход по SMS ---

    async def authenticate(self, phone: str) -> dict:
        body = {
            "identifier": {"type": "phone", "data": {"value": phone}},
            "authenticator": {"type": "sms_otp", "data": {}},
            "channel": {
                "type": "web",
                "user_type": "private",
                "data": {"rsa_data": RSA_DATA, "oidc": self._oidc,
                         "browser": "Chrome", "os": "Android 10"},
            },
        }
        return await self._raw(UAPI + "authenticate", method="POST", json=body,
                               headers=self._uapi_headers())

    async def verify(self, ouid: str, code: str) -> dict:
        body = {
            "identifier": {"type": "ouid", "data": {"value": ouid}},
            "authenticator": {"type": "sms_otp", "data": {"value": str(code)}},
            "channel": {
                "type": "web",
                "data": {"rsa_data": RSA_DATA, "oidc": self._oidc, "browser": "Chrome",
                         "os": "Android 10", "set_cookie": False},
            },
        }
        answer = await self._raw(UAPI + "verify", method="POST", json=body,
                                 headers=self._uapi_headers())
        return answer.get("response_data", answer)

    async def get_access_token(self, authcode: str) -> dict:
        return await self._token_request({
            "code": authcode,
            "code_verifier": self._code_verifier,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        })


def _error_text(payload, response) -> str:
    if isinstance(payload, dict):
        state = payload.get("state") or {}
        if isinstance(state, dict) and (state.get("title") or state.get("message")):
            return f"{state.get('title') or ''}: {state.get('message') or ''}".strip(": ")
        for key in ("message", "error", "error_description"):
            if payload.get(key):
                return f"{response.status} {payload[key]}"
        return f"{response.status} {payload}"
    text = (payload or "").strip().replace("\n", " ")
    return f"{response.status} {text[:200] or response.reason}"


RESULT = (NetworkError,)


async def api_get(api, path: str) -> dict:
    return await api._send_request(GATEWAY + path)


async def api_put(api, path: str, body: dict) -> dict:
    return await api._send_request(GATEWAY + path, method="PUT", json=body)


async def retry(func, *args, attempts: int = 3, delay: float = 1.5):
    """Повторяет сетевой вызов при обрыве/таймауте."""
    for attempt in range(1, attempts + 1):
        try:
            return await func(*args)
        except RESULT:
            if attempt == attempts:
                raise
            print(f"Сеть не ответила, повтор {attempt}/{attempts - 1}…")
            await asyncio.sleep(delay)


# ---------- устройства и состояние ----------

def device_name(device: dict) -> str:
    return (device.get("name") or {}).get("name") or "лампа"


def device_model(device: dict) -> str:
    return (device.get("device_info") or {}).get("model") or "?"


async def collect_lights(api) -> list:
    """Обходит все группы и возвращает список светильников."""
    groups = (await api_get(api, "device_groups")).get("result") or []
    queue = [g["id"] for g in groups]
    visited = set()
    lights = {}

    while queue:
        group_id = queue.pop(0)
        if group_id in visited:
            continue
        visited.add(group_id)

        try:
            tree = await api_get(api, f"device_groups/tree?id={group_id}")
        except LampError:
            continue

        result = tree.get("result", tree)
        for device in result.get("devices") or []:
            info = device.get("device_info") or {}
            categories = " ".join(c.lower() for c in (device.get("categories") or []))
            model = (info.get("model") or "").lower()
            description = (info.get("description") or "").lower()
            if ("light" in categories or "bulb" in model or "lamp" in model
                    or "лампа" in description):
                lights[device["id"]] = device

        for child in result.get("children") or []:
            child_id = child.get("id") if isinstance(child, dict) else None
            if child_id and child_id not in visited:
                queue.append(child_id)

    return list(lights.values())


def item_value(item: dict):
    kind = item.get("type")
    if kind == "BOOL":
        return bool(item.get("bool_value"))
    if kind == "INTEGER":
        return int(item.get("integer_value") or 0)
    if kind == "FLOAT":
        return float(item.get("float_value") or 0)
    if kind == "ENUM":
        return item.get("enum_value") or ""
    if kind == "COLOR":
        return item.get("color_value")
    return item.get("string_value")


async def get_state(api, device_id: str) -> dict:
    data = await api_get(api, f"devices/{device_id}/state")
    return {item["key"]: item_value(item) for item in data.get("reported_state") or []}


def brightness_of(states: dict):
    raw = states.get("light_brightness")
    return None if raw is None else max(5, min(100, round(raw / 10)))


def temp_of(states: dict):
    raw = states.get("light_colour_temp")
    return None if raw is None else max(0, min(100, round(raw / 10)))


def colour_of(states: dict):
    colour = states.get("light_colour")
    if not colour:
        return None
    return colour["h"], round(colour["s"] / 10), round(colour["v"] / 10)


async def put_state(api, device_id: str, states: list) -> None:
    await api_put(api, f"devices/{device_id}/state",
                  {"desired_state": states, "device_id": device_id})


# ---------- вход ----------

def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) != 11 or not digits.startswith("7"):
        raise SystemExit("Нужен номер в формате +7XXXXXXXXXX")
    return digits


def _phones_of(auth: dict) -> str:
    """Достаёт номера из ответа authenticate (структура плавает)."""
    try:
        return ", ".join(auth["authenticator"][0]["data"]["phones"])
    except (KeyError, IndexError, TypeError):
        return ""


async def do_setup() -> None:
    ensure_config_dir()
    phone = normalize_phone(input("Телефон (например +79001234567): ").strip())
    api = SberLampAPI(refresh_token_path=TOKEN_PATH, level=logging.CRITICAL)
    try:
        auth = await retry(api.authenticate, phone)
        phones = _phones_of(auth)
        print(f"SMS отправлена на {phones or phone}")
        code = input("Код из SMS: ").strip()
        verified = await retry(api.verify, auth.get("ouid", ""), code)
        await retry(api.get_access_token, verified.get("authcode", ""))

        print("Вход выполнен, ищу лампы…")
        lights = await retry(collect_lights, api)
        if not lights:
            raise SystemExit("Лампы не найдены. Проверь, что она добавлена в приложение «Салют!».")

        lights.sort(key=lambda d: device_name(d).lower())
        if len(lights) == 1:
            chosen = lights[0]
        else:
            for i, device in enumerate(lights, 1):
                print(f"  {i}. {device_name(device)}  ({device_model(device)})")
            answer = input(f"Какую лампу использовать? [1-{len(lights)}, по умолчанию 1]: ").strip()
            index = int(answer) if answer.isdigit() and 1 <= int(answer) <= len(lights) else 1
            chosen = lights[index - 1]

        save_config({
            "device_id": chosen["id"],
            "device_name": device_name(chosen),
            "model": device_model(chosen),
        })
        ensure_config_dir()
        print(f"Готово: «{device_name(chosen)}». Проверь: lamp status")
    finally:
        await api.close()


async def with_api(action) -> None:
    ensure_config_dir()
    api = SberLampAPI(refresh_token_path=TOKEN_PATH, level=logging.CRITICAL)
    try:
        await action(api)
    finally:
        await api.close()


# ---------- команды ----------

async def cmd_status(api, cfg) -> None:
    states = await get_state(api, cfg["device_id"])
    parts = ["включена" if states.get("on_off") else "выключена"]
    if states.get("online") is False:
        parts.append("нет связи")

    brightness = brightness_of(states)
    if brightness is not None:
        parts.append(f"яркость {brightness}%")

    mode = states.get("light_mode")
    if mode == "scene":
        scene = states.get("light_scene")
        parts.append(f"сцена {SCENE_NAMES.get(scene, scene)}")
    elif mode in ("colour", "color"):
        colour = colour_of(states)
        if colour:
            parts.append(f"цвет H={colour[0]} S={colour[1]} V={colour[2]}")
    else:
        temp = temp_of(states)
        if temp is not None:
            parts.append(f"температура {temp} (0 тёплый … 100 холодный)")
        if mode:
            parts.append(f"режим {MODE_NAMES.get(mode, mode)}")

    print(f"«{cfg.get('device_name', 'лампа')}»: " + ", ".join(parts))


async def cmd_devices(api, cfg) -> None:
    lights = await collect_lights(api)
    current = cfg.get("device_id")
    if not lights:
        print("Лампы не найдены.")
        return
    for device in sorted(lights, key=lambda d: device_name(d).lower()):
        mark = "*" if device["id"] == current else " "
        print(f"{mark} {device_name(device)}  [{device_model(device)}]  {device['id']}")


async def set_brightness(api, device_id: str, value: int) -> None:
    states = await get_state(api, device_id)
    value = max(5, min(100, value))
    colour = colour_of(states)
    if states.get("light_mode") in ("colour", "color") and colour:
        h, s, _ = colour
        await put_state(api, device_id, [
            {"key": "light_colour", "type": "COLOR",
             "colour_value": {"h": h, "s": s * 10, "v": max(100, value * 10)}},
        ])
    else:
        await put_state(api, device_id, [
            {"key": "light_brightness", "type": "INTEGER", "integer_value": value * 10},
        ])


async def set_white(api, device_id: str, temp: int) -> None:
    states = await get_state(api, device_id)
    brightness = brightness_of(states) or 100
    await put_state(api, device_id, [
        {"key": "light_mode", "type": "ENUM", "enum_value": "white"},
        {"key": "light_brightness", "type": "INTEGER", "integer_value": brightness * 10},
        {"key": "light_colour_temp", "type": "INTEGER", "integer_value": max(0, min(100, temp)) * 10},
    ])


async def cmd_fade(api, device_id: str, target: int, minutes: float) -> None:
    """Плавно ведёт яркость к target за minutes минут."""
    states = await get_state(api, device_id)
    start = brightness_of(states) or 100
    target = max(5, min(100, target))
    duration = max(2.0, minutes * 60)
    interval = 10.0 if duration > 120 else 2.0
    steps = max(1, int(duration // interval))
    colour = colour_of(states)
    in_colour = states.get("light_mode") in ("colour", "color") and colour

    for step in range(1, steps + 1):
        value = round(start + (target - start) * step / steps)
        if in_colour:
            h, s, _ = colour
            payload = [{"key": "light_colour", "type": "COLOR",
                        "colour_value": {"h": h, "s": s * 10, "v": max(100, value * 10)}}]
        else:
            payload = [{"key": "light_brightness", "type": "INTEGER",
                        "integer_value": value * 10}]
        await put_state(api, device_id, payload)
        if step < steps:
            await asyncio.sleep(interval)


# ---------- свой «адаптив» по расписанию ----------

SCHEDULE_PATH = CONFIG_DIR / "schedule.json"
DEFAULT_ANCHORS = [
    {"time": "00:00", "brightness": 6, "temp": 20},
    {"time": "06:30", "brightness": 10, "temp": 25},
    {"time": "09:00", "brightness": 85, "temp": 80},
    {"time": "18:00", "brightness": 55, "temp": 45},
    {"time": "22:30", "brightness": 12, "temp": 25},
]


def to_minutes(hhmm: str) -> int:
    hours, minutes = hhmm.split(":")
    return int(hours) * 60 + int(minutes)


def load_anchors() -> list:
    try:
        data = json.loads(SCHEDULE_PATH.read_text())
    except FileNotFoundError:
        return DEFAULT_ANCHORS
    except (json.JSONDecodeError, OSError) as error:
        print(f"schedule.json не читается ({error}), беру стандартный график", file=sys.stderr)
        return DEFAULT_ANCHORS
    anchors = data.get("anchors") if isinstance(data, dict) else data
    if not anchors:
        return DEFAULT_ANCHORS
    try:
        return sorted(anchors, key=lambda a: to_minutes(a["time"]))
    except (KeyError, TypeError, ValueError):
        print("в schedule.json нет поля time - беру стандартный график", file=sys.stderr)
        return DEFAULT_ANCHORS


def curve_at(minutes: int, anchors: list):
    """Линейно интерполирует яркость и температуру между опорными точками."""
    points = [(to_minutes(a["time"]), a["brightness"], a["temp"]) for a in anchors]
    for index, (start_t, start_b, start_temp) in enumerate(points):
        end_t, end_b, end_temp = points[(index + 1) % len(points)]
        span = (end_t - start_t) % 1440 or 1440
        offset = (minutes - start_t) % 1440
        if offset <= span:
            share = offset / span
            return (round(start_b + (end_b - start_b) * share),
                    round(start_temp + (end_temp - start_temp) * share))
    return points[0][1], points[0][2]


def densify(anchors: list, step: int) -> list:
    """Раскладывает график на точки каждые step минут (для облака)."""
    points = []
    for minute in range(0, 1440, step):
        brightness, temp = curve_at(minute, anchors)
        points.append({"time": f"{minute // 60:02d}:{minute % 60:02d}",
                       "brightness": max(5, min(100, brightness)),
                       "temp": max(0, min(100, temp))})
    return points


def cmd_schedule() -> None:
    anchors = load_anchors()
    minutes = now_minutes()
    now_b, now_t = curve_at(minutes, anchors)
    print("график (время → яркость / температура):")
    for anchor in anchors:
        print(f"  {anchor['time']}  {anchor['brightness']:3d}%   {anchor['temp']:3d}")
    print(f"\nсейчас ({minutes // 60:02d}:{minutes % 60:02d}) должно быть: {now_b}% / {now_t}")
    if SCHEDULE_PATH.exists():
        print(f"файл: {SCHEDULE_PATH}")
    else:
        print(f"файл: {SCHEDULE_PATH} - нет, используется стандартный график")


async def apply_curve(api, device_id: str, dry_run: bool = False,
                      force: bool = False) -> str:
    """Возвращает строку-отчёт: что сделано (или почему нет)."""
    anchors = load_anchors()
    minutes = now_minutes()
    target_b, target_t = curve_at(minutes, anchors)
    states = await get_state(api, device_id)
    mode = states.get("light_mode") or ""
    stamp = f"{minutes // 60:02d}:{minutes % 60:02d}"

    if not states.get("on_off") and not force:
        return f"{stamp}: лампа выключена - не трогаю"
    if mode in ("scene", "music", "colour", "color") and not force:
        return (f"{stamp}: режим «{MODE_NAMES.get(mode, mode)}» - не перебиваю "
                f"(нужно --force)")

    current_b = brightness_of(states) or 100
    current_t = temp_of(states) if temp_of(states) is not None else 50
    if abs(current_b - target_b) < 2 and abs(current_t - target_t) < 2 and mode == "white":
        return f"{stamp}: без изменений - {current_b}% / {current_t}"

    if dry_run:
        return (f"{stamp}: стало бы {current_b}% / {current_t} → "
                f"{target_b}% / {target_t}")

    await put_state(api, device_id, [
        {"key": "light_mode", "type": "ENUM", "enum_value": "white"},
        {"key": "light_brightness", "type": "INTEGER", "integer_value": target_b * 10},
        {"key": "light_colour_temp", "type": "INTEGER", "integer_value": target_t * 10},
    ])
    return f"{stamp}: {current_b}% / {current_t} → {target_b}% / {target_t}"


async def cmd_auto(api, device_id: str, dry_run: bool = False,
                   force: bool = False) -> None:
    print(await apply_curve(api, device_id, dry_run, force))


async def cmd_watch(api, device_id: str, interval: int) -> None:
    """Долгоживущий цикл: держит сессию и правит свет по графику."""
    print(f"слежу за графиком, шаг {interval} с (Ctrl+C - остановить)", flush=True)
    last = None
    while True:
        try:
            message = await apply_curve(api, device_id)
            if message != last:
                print(message, flush=True)
                last = message
        except (LampError, OSError) as error:
            text = f"ошибка: {type(error).__name__}: {error}"
            if text != last:
                print(text, file=sys.stderr, flush=True)
                last = text
        await asyncio.sleep(interval)


def now_minutes() -> int:
    import datetime
    now = datetime.datetime.now()
    return now.hour * 60 + now.minute


# ---------- облачные сценарии Сбера ----------

CLOUD_PATH = CONFIG_DIR / "cloud.json"
SCENARIO_IMAGE = ("https://img.iot.sberdevices.ru/p/q100/f1/d1/"
                  "f13cd11ae0a7aec7b651e4d49fb2385a4e98464688c7867abff0bd36f9f55bb1")


def local_timezone() -> str:
    """IANA-зона системы, например Asia/Krasnoyarsk."""
    try:
        done = subprocess.run(["timedatectl", "show", "-p", "Timezone", "--value"],
                              capture_output=True, text=True, timeout=5)
        zone = done.stdout.strip()
        if zone:
            return zone
    except (OSError, subprocess.SubprocessError):
        pass
    return "UTC"


def cloud_load() -> dict:
    try:
        return json.loads(CLOUD_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def cloud_save(data: dict) -> None:
    ensure_config_dir()
    CLOUD_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    CLOUD_PATH.chmod(0o600)


def state_item(key: str, kind: str, **values) -> dict:
    item = {"key": key, "type": kind, "float_value": 0, "integer_value": "0",
            "string_value": "", "bool_value": False, "enum_value": "",
            "color_value": None, "last_sync": None,
            "schedule_value": [], "sequence": []}
    item.update(values)
    return item


def condition(**values) -> dict:
    node = {"type": None, "time_data": None, "phrases_data": None,
            "nested_conditions_data": None, "device_data": None,
            "check_device_conditions_data": None,
            "check_scenario_conditions_data": None,
            "geo_time_conditions_data": None}
    node.update(values)
    return node


def daily_rrule(hhmm: str, zone: str) -> str:
    """RRULE как в сценариях Сбера: ежедневно в hh:mm по зоне zone."""
    hours, minutes = hhmm.split(":")
    now = datetime.datetime.now()
    start = now.replace(hour=int(hours), minute=int(minutes), second=0, microsecond=0)
    if start <= now:
        start += datetime.timedelta(days=1)
    return (f"DTSTART;TZID={zone}:{start:%Y%m%dT%H%M%S}\n"
            f"RRULE:FREQ=DAILY;WKST=MO;BYDAY=MO,TU,WE,TH,FR,SA,SU;"
            f"BYHOUR={int(hours)};BYMINUTE={int(minutes)};BYSECOND=0")


def build_step(device_id: str, home_id: str, hhmm: str, zone: str,
               brightness: int, temp: int) -> dict:
    """Один шаг сценария: в hhmm поставить белый свет нужной яркости/температуры."""
    desired = [
        {"state": state_item("light_mode", "ENUM", enum_value="white"),
         "relative": False, "mode": "RANGE_SET"},
        {"state": state_item("light_brightness", "INTEGER",
                             integer_value=str(brightness * 10)),
         "relative": False, "mode": "RANGE_SET"},
        {"state": state_item("light_colour_temp", "INTEGER",
                             integer_value=str(temp * 10)),
         "relative": False, "mode": "RANGE_SET"},
    ]
    task = {
        "type": "DEVICE_COMMAND",
        "start_delay": "0s",
        "device_command_data": {
            "device_id": device_id,
            "parent_id": home_id,
            "categories_slugs": ["light"],
            "desired_state": desired,
            "alter_state": [],
            "alter_state_ttl": "",
        },
    }
    inner = condition(type="TIME", time_data={"execute_at": None,
                                              "rrule": daily_rrule(hhmm, zone)})
    outer = condition(type="CONDITIONS",
                      nested_conditions_data={"relation": "OR", "conditions": [inner]})
    return {"condition": outer, "tasks": [task]}


async def cloud_home_id(api) -> str:
    groups = (await api_get(api, "device_groups")).get("result") or []
    for group in groups:
        if (group.get("group_type") or "").upper() == "HOME":
            return group["id"]
    if groups:
        return groups[0]["id"]
    raise SystemExit("Не нашёл дом в аккаунте - сценарий создавать некуда")


async def cloud_create(api, home_id: str, name: str, steps: list) -> str:
    body = {
        "name": name,
        "image": SCENARIO_IMAGE,
        "home_id": home_id,
        "timezone": local_timezone(),
        "is_active": True,
        "steps": steps,
    }
    answer = await api._send_request(url=GATEWAY + "scenario/v2/scenario",
                                     method="POST", json=body)
    payload = answer.get("result", answer)
    scenario_id = payload.get("id")
    if not scenario_id:
        raise LampError(f"Облако не вернуло id сценария: {answer}")
    return scenario_id


async def cloud_delete(api, scenario_id: str) -> None:
    await api._send_request(url=GATEWAY + f"scenario/v2/scenario/{scenario_id}",
                            method="DELETE")


async def cloud_set_active(api, scenario_id: str, active: bool) -> None:
    await api._send_request(url=GATEWAY + f"scenario/v2/scenario/{scenario_id}/active",
                            method="PATCH", json={"is_active": active})


def print_cloud(stored: dict) -> None:
    items = stored.get("items") or []
    if not items:
        print("Облачных сценариев нет (lamp cloud push - создать)")
        return
    state = "включены" if stored.get("active", True) else "выключены"
    print(f"Сценарии графика: {state}, точек {len(items)}, зона {stored.get('timezone')}")
    for item in items:
        print(f"  {item['time']}  {item['brightness']:3d}%  t={item['temp']}  {item['id']}")


async def cmd_cloud(api, cfg, action: str, step: int = 0) -> None:
    device_id = cfg["device_id"]
    stored = cloud_load()
    items = stored.get("items") or []

    if action == "list":
        print_cloud(stored)
        if items:
            active = faulty = 0
            for item in items:
                try:
                    live = await api_get(api, f"scenario/v2/scenario/{item['id']}")
                    node = live.get("result", live)
                    active += bool(node.get("is_active"))
                    faulty += bool(node.get("is_faulty"))
                except LampError as error:
                    print(f"  {item['time']}: не читается - {error}")
            print(f"в облаке: активны {active} из {len(items)}, с ошибкой {faulty}")
        return

    if action == "push":
        anchors = load_anchors()
        if step > 0:
            anchors = densify(anchors, step)
        zone = local_timezone()
        home_id = await cloud_home_id(api)
        for item in items:
            try:
                await cloud_delete(api, item["id"])
            except LampError as error:
                print(f"старый {item['time']} не удалился: {error}", file=sys.stderr)

        created = []
        for anchor in anchors:
            step = build_step(device_id, home_id, anchor["time"], zone,
                              anchor["brightness"], anchor["temp"])
            try:
                scenario_id = await cloud_create(api, home_id,
                                                 f"Свет {anchor['time']}", [step])
            except LampError as error:
                cloud_save({"active": True, "timezone": zone, "items": created})
                raise SystemExit(f"на точке {anchor['time']} облако отказало: {error}\n"
                                 f"создано {len(created)} - убрать: lamp cloud delete")
            created.append({"id": scenario_id, "time": anchor["time"],
                            "brightness": anchor["brightness"], "temp": anchor["temp"]})
            print(f"  {anchor['time']}  {anchor['brightness']:3d}%  t={anchor['temp']}"
                  f"  → {scenario_id}")
        cloud_save({"active": True, "timezone": zone, "items": created})
        print(f"готово: {len(created)} сценариев в облаке, ноутбук больше не нужен")
        return

    if action in ("on", "off"):
        if not items:
            raise SystemExit("Сценариев ещё нет - сначала lamp cloud push")
        wanted = action == "on"
        failed = 0
        for item in items:
            try:
                await cloud_set_active(api, item["id"], wanted)
            except LampError as error:
                failed += 1
                print(f"  {item['time']}: {error}", file=sys.stderr)
        stored["active"] = wanted and not failed
        cloud_save(stored)
        word = "включены" if wanted else "выключены"
        print(f"сценарии {word}" + (f" (ошибок: {failed})" if failed else ""))
        return

    if action == "delete":
        if not items:
            raise SystemExit("Сценариев нет - нечего удалять")
        for item in items:
            try:
                await cloud_delete(api, item["id"])
            except LampError as error:
                print(f"  {item['time']}: не удалился - {error}", file=sys.stderr)
        cloud_save({})
        print("сценарии удалены, облако чистое")
        return

    raise SystemExit(f"Неизвестное действие: {action}")


async def run(args) -> None:
    cfg = load_config()

    if args.cmd == "setup":
        await do_setup()
        return

    if args.cmd == "schedule":
        cmd_schedule()
        return

    if not cfg.get("device_id"):
        raise SystemExit("Сначала выполни: lamp setup")

    device_id = cfg["device_id"]

    async def action(api):
        if args.cmd == "status":
            await cmd_status(api, cfg)
        elif args.cmd == "devices":
            await cmd_devices(api, cfg)
        elif args.cmd == "on":
            await put_state(api, device_id, [
                {"key": "on_off", "type": "BOOL", "bool_value": True},
            ])
        elif args.cmd == "off":
            await put_state(api, device_id, [
                {"key": "on_off", "type": "BOOL", "bool_value": False},
            ])
        elif args.cmd == "toggle":
            states = await get_state(api, device_id)
            await put_state(api, device_id, [
                {"key": "on_off", "type": "BOOL", "bool_value": not states.get("on_off")},
            ])
        elif args.cmd == "b":
            await set_brightness(api, device_id, args.value)
        elif args.cmd in ("up", "down"):
            states = await get_state(api, device_id)
            current = brightness_of(states) or 100
            step = args.step
            await set_brightness(api, device_id,
                                 current + (step if args.cmd == "up" else -step))
        elif args.cmd == "temp":
            await set_white(api, device_id, args.value)
        elif args.cmd == "warm":
            await set_white(api, device_id, 10)
        elif args.cmd == "cool":
            await set_white(api, device_id, 90)
        elif args.cmd == "rgb":
            h, s, v = rgb_to_hsv(args.r, args.g, args.b)
            await put_state(api, device_id, [
                {"key": "light_mode", "type": "ENUM", "enum_value": "colour"},
                {"key": "light_colour", "type": "COLOR",
                 "colour_value": {"h": h, "s": s * 10, "v": max(100, v * 10)}},
            ])
        elif args.cmd == "scene":
            await put_state(api, device_id, [
                {"key": "light_mode", "type": "ENUM", "enum_value": "scene"},
                {"key": "light_scene", "type": "ENUM", "enum_value": args.name},
            ])
        elif args.cmd == "adaptive":
            await put_state(api, device_id, [
                {"key": "light_mode", "type": "ENUM", "enum_value": "adaptive"},
            ])
        elif args.cmd == "fade":
            await cmd_fade(api, device_id, args.target, args.minutes)
        elif args.cmd == "auto":
            await cmd_auto(api, device_id, args.dry_run, args.force)
        elif args.cmd == "watch":
            await cmd_watch(api, device_id, args.interval)
        elif args.cmd == "cloud":
            await cmd_cloud(api, cfg, args.action, args.step)
        else:
            raise SystemExit(f"Неизвестная команда: {args.cmd}")

    if args.cmd in ("status", "devices"):
        await retry(with_api, action)
    else:
        await with_api(action)


def rgb_to_hsv(r: int, g: int, b: int):
    r, g, b = r / 255, g / 255, b / 255
    mx, mn = max(r, g, b), min(r, g, b)
    diff = mx - mn
    if diff == 0:
        h = 0
    elif mx == r:
        h = (60 * ((g - b) / diff)) % 360
    elif mx == g:
        h = 60 * ((b - r) / diff) + 120
    else:
        h = 60 * ((r - g) / diff) + 240
    s = 0 if mx == 0 else diff / mx
    return round(h), round(s * 100), round(mx * 100)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lamp",
        description="Управление умной лампой Sber (через облако «Салют!»).",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("setup", help="вход по SMS и выбор лампы")
    sub.add_parser("on", help="включить")
    sub.add_parser("off", help="выключить")
    sub.add_parser("toggle", help="переключить")
    sub.add_parser("status", help="показать состояние")
    sub.add_parser("devices", help="список ламп в аккаунте")
    sub.add_parser("warm", help="тёплый белый")
    sub.add_parser("cool", help="холодный белый")
    sub.add_parser("adaptive", help="адаптивный свет")

    p = sub.add_parser("b", help="яркость 5-100")
    p.add_argument("value", type=int)

    p = sub.add_parser("up", help="яркость +N (по умолчанию 10)")
    p.add_argument("step", nargs="?", type=int, default=10)

    p = sub.add_parser("down", help="яркость -N (по умолчанию 10)")
    p.add_argument("step", nargs="?", type=int, default=10)

    p = sub.add_parser("temp", help="температура 0 (тёплый) - 100 (холодный)")
    p.add_argument("value", type=int)

    p = sub.add_parser("rgb", help="цвет в RGB, например: lamp rgb 255 120 0")
    p.add_argument("r", type=int)
    p.add_argument("g", type=int)
    p.add_argument("b", type=int)

    p = sub.add_parser("scene", help="сцена: " + ", ".join(SCENES))
    p.add_argument("name", choices=SCENES)

    p = sub.add_parser("fade", help="плавно: fade 30 10 - до 30%% за 10 минут")
    p.add_argument("target", type=int)
    p.add_argument("minutes", nargs="?", type=float, default=10)

    sub.add_parser("schedule", help="показать график яркости/температуры по времени")

    p = sub.add_parser("auto", help="выставить яркость и температуру по графику")
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="только показать, что было бы")
    p.add_argument("--force", action="store_true",
                   help="перебить ручной режим (сцена/цвет) и включить лампу")

    p = sub.add_parser("watch", help="постоянно держать свет по графику (служба)")
    p.add_argument("--interval", type=int, default=180,
                   help="шаг проверки в секундах (по умолчанию 180)")

    p = sub.add_parser("cloud", help="облачный сценарий Сбера (работает без ноутбука)")
    p.add_argument("action", choices=["push", "list", "on", "off", "delete"])
    p.add_argument("--step", type=int, default=0, metavar="МИН",
                   help="для push: разложить график на точки каждые МИН минут")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except AuthError:
        sys.exit("Токен не найден или истёк - выполни: lamp setup")
    except NetworkError as error:
        detail = f": {error.__cause__}" if error.__cause__ else ""
        sys.exit(f"Сеть недоступна, облако Сбера не отвечает{detail}\n"
                 "Проверь интернет и VPN (домены Сбера должны идти напрямую).")
    except ApiError as error:
        if "invalid_grant" in str(error):
            sys.exit("Токен недействителен - выполни: lamp setup")
        sys.exit(f"Облако ответило ошибкой: {error}")
    except (LampError, OSError) as error:
        sys.exit(f"Ошибка: {error}")
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()