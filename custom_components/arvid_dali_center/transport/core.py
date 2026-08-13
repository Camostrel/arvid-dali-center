"""odc.core — транспорт: крипто, discovery (UDP), MQTT-сессия со шлюзом.

Крипто выведено аналитически (см. docs/CRYPTO.md): AES-128-CTR, IV=0101000000001101,
ключ Sunricher неизвестен, но IV фиксирован → keystream постоянен (XOR), ключ не нужен.
"""

from __future__ import annotations

import contextlib
import json
import queue
import select
import socket
import threading
import time
import uuid

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# ── Криптопараметры discovery ────────────────────────────────────────────────
IV_DOC = b"0101000000001101"
# keystream AES-CTR слоя 2 (33 байта). См. docs/CRYPTO.md.
KEYSTREAM = bytes.fromhex(
    "6e078bfa86b9e9bf88c8f062edd688d6840be5885b679e30e59bb193084d90fcd6"
)

# ── Параметры сети ───────────────────────────────────────────────────────────
MCAST = "239.255.255.250"
SEND_PORT = 1900
LISTEN_PORT = 50569


def local_ip_for(target_ip: str) -> str | None:
    """Локальный IP интерфейса, через который ОС достучится до target_ip.

    Решает проблему «шлюз в другой подсети»: для multicast-discovery нужно слать с
    того интерфейса, что видит подсеть шлюза. UDP-connect пакеты не шлёт.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((target_ip, 9))
        ip = s.getsockname()[0]
        return None if ip in ("0.0.0.0", "") else ip
    except OSError:
        return None
    finally:
        s.close()


def list_ifaces() -> list[tuple[str, str]]:
    """Список (имя_интерфейса, IPv4) без loopback — для выбора интерфейса пользователем."""
    try:
        import psutil
        out: list[tuple[str, str]] = []
        for name, addrs in psutil.net_if_addrs().items():
            for a in addrs:
                if a.family == socket.AF_INET and not a.address.startswith("127."):
                    out.append((name, a.address))
        return out
    except Exception:  # noqa: BLE001
        ip = local_ip_for("8.8.8.8")
        return [("default", ip)] if ip else []


def _iface_score(ip: str) -> int:
    """Приоритет LAN-диапазонов; Docker/WSL/Tailscale/link-local — в конец."""
    parts = ip.split(".")
    second = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    if ip.startswith("169.254") or ip.startswith("100."):
        return 9  # link-local / CGNAT (Tailscale)
    if ip.startswith("172.") and 16 <= second <= 31:
        return 3  # часто Docker/WSL
    if ip.startswith("192.168."):
        return 0
    if ip.startswith("10."):
        return 1
    return 5


def auto_iface() -> str | None:
    """Подсказка интерфейса: предпочитаем настоящий LAN (192.168/10), не Docker/Tailscale."""
    ifs = list_ifaces()
    if not ifs:
        return local_ip_for("8.8.8.8")
    return sorted(ifs, key=lambda x: _iface_score(x[1]))[0][1]


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def build_discover() -> bytes:
    """Запрос discover в формате приложения: {"cmd":"<64hex>","snList":[]}."""
    priv = uuid.uuid4().hex[:16].encode()
    enc = Cipher(algorithms.AES(priv), modes.CTR(IV_DOC)).encryptor()
    l1 = (enc.update(b"discover") + enc.finalize()).hex().encode()
    cmd = _xor(priv + l1, KEYSTREAM).hex()
    return json.dumps({"cmd": cmd, "snList": []}, separators=(",", ":")).encode()


def decrypt_cred(hex_ct: str) -> str:
    """Расшифровать username/passwd из discoverRes (XOR keystream)."""
    if not hex_ct:
        return ""
    ct = bytes.fromhex(hex_ct)
    if len(ct) > len(KEYSTREAM):
        raise ValueError(f"creds длиннее keystream ({len(ct)}>{len(KEYSTREAM)}): {hex_ct}")
    return _xor(ct, KEYSTREAM).decode("utf-8")


def _parse_gateway(d: dict) -> dict:
    return {
        "gwSn": d.get("gwSn"),
        "gwIp": d.get("gwIp"),
        "port": int(d.get("port", 1883)),
        "isTls": bool(d.get("isMqttTls")),
        "channelTotal": [int(c) for c in (d.get("channelTotal") or [0])
                         if str(c).lstrip("-").isdigit()] or [0],
        "username": decrypt_cred(d.get("username", "")),
        "passwd": decrypt_cred(d.get("passwd", "")),
        # имя/этаж шлюза — нужны для setGatewayName (сохранить привязку этажа) и
        # отображения текущего имени в карточке. Не критичны для транспорта, но дёшево.
        "name": d.get("name"),
        "floorId": d.get("floorId"),
        "floorName": d.get("floorName"),
    }


def _open_discovery_socket(bind_ip: str) -> tuple[socket.socket, bytes]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("0.0.0.0", LISTEN_PORT))
    mreq = socket.inet_aton(MCAST) + socket.inet_aton(bind_ip)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    return sock, mreq


def discover_all(bind_ip: str = "192.168.8.6", timeout: float = 8.0,
                 send_every: float = 2.0) -> list[dict]:
    """Найти ВСЕ шлюзы в LAN за окно timeout (строго через bind_ip). Список dict."""
    message = build_discover()
    sock, mreq = _open_discovery_socket(bind_ip)
    found: dict[str, dict] = {}
    # МОНОТОННЫЕ часы (не time.time): после вырубания света HAOS стартует с неверным RTC,
    # NTP «прыгает» уже во время работы → wall-clock окна поиска схлопываются/подвисают, и
    # реконнект проваливается. monotonic считает секунды и не зависит от даты/скачков.
    deadline = time.monotonic() + timeout
    last_send = 0.0
    try:
        while time.monotonic() < deadline:
            if time.monotonic() - last_send >= send_every:
                sock.sendto(message, (MCAST, SEND_PORT))
                last_send = time.monotonic()
            ready, _, _ = select.select([sock], [], [], 1.0)
            if not ready:
                continue
            data, _ = sock.recvfrom(4096)
            try:
                d = (json.loads(data.decode("utf-8")) or {}).get("data") or {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if d.get("gwSn") and d["gwSn"] not in found:
                found[d["gwSn"]] = _parse_gateway(d)
    finally:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
        sock.close()
    return list(found.values())


def dev_key(d: dict) -> str:
    """Стабильный ключ устройства: предпочтительно devid, иначе ch:addr:type."""
    return d.get("devid") or f"{d.get('channel')}:{d.get('address')}:{d.get('devType')}"


class GatewaySession:
    """MQTT-сессия со шлюзом: подключение, отправка команд, приём через очередь.

    Явный жизненный цикл: `connect()` → `send(...)`/`recv(...)` → `close()` (контекст-менеджер
    `__enter__/__exit__` удалён в v0.49 — сессией владеет хаб, закрывает в реконнекте/unload).
    """

    def __init__(self, gw: dict) -> None:
        import paho.mqtt.client as mqtt

        self.gw = gw
        self.sub_topic = f"/{gw['gwSn']}/client/reciver/"   # ответы (опечатка протокола)
        self.pub_topic = f"/{gw['gwSn']}/server/publish/"   # команды
        self.inbox: "queue.Queue[dict]" = queue.Queue()
        self._connected = threading.Event()
        # msgId-счётчик потокобезопасен: send() зовётся из РАЗНЫХ executor-потоков HA
        # (async_request → executor). Без лока read-modify-write счётчика при потоке команд
        # (автоматизации на большом объекте) давал коллизию msgId → второй _pending затирал
        # первый → «команда не подтвердилась» по таймауту.
        self._msg_seq = 0
        self._seq_lock = threading.Lock()
        # колбэк состояния связи: on_state("online"|"offline"). Ставит владелец
        # (DaliGatewayHub) ДО connect(). paho сам переподключается — на каждый
        # коннект/обрыв брокера дёргаем колбэк, чтобы хаб знал реальное состояние.
        self.on_state = None
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2, client_id=f"odc-{uuid.uuid4().hex[:8]}"
        )
        self._client.username_pw_set(gw["username"], gw["passwd"])
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_message = self._on_message

    def _fire_state(self, state: str) -> None:
        cb = self.on_state
        if cb:
            with contextlib.suppress(Exception):
                cb(state)

    def _on_connect(self, c, userdata, flags, reason_code, properties) -> None:
        if not getattr(reason_code, "is_failure", reason_code != 0):
            c.subscribe(self.sub_topic, qos=0)
            self._connected.set()
            self._fire_state("online")

    def _on_disconnect(self, *args) -> None:
        # paho v2: (client, userdata, disconnect_flags, reason_code, properties).
        # Сигнатуру не фиксируем (совместимость) — важен сам факт обрыва.
        self._connected.clear()
        self._fire_state("offline")

    def _on_message(self, c, userdata, msg) -> None:
        with contextlib.suppress(Exception):
            self.inbox.put(json.loads(msg.payload.decode("utf-8")))

    def connect(self, timeout: float = 10.0) -> "GatewaySession":
        self._client.connect(self.gw["gwIp"], self.gw["port"], keepalive=60)
        self._client.loop_start()
        if not self._connected.wait(timeout):
            raise TimeoutError("MQTT: не дождались подключения/подписки")
        return self

    def next_msg_id(self) -> str:
        """Потокобезопасный уникальный msgId (формат `{ms}-{seq}` сохранён для совместимости
        со шлюзом). Позволяет вызывающему зарегистрировать _pending ДО публикации команды."""
        with self._seq_lock:
            self._msg_seq += 1
            seq = self._msg_seq
        return f"{int(time.time() * 1000)}-{seq}"

    def send(self, cmd: str, msg_id: str | None = None, **fields) -> str:
        """Опубликовать команду; msgId/gwSn подставляются. Вернёт msgId (уникальный)."""
        if msg_id is None:
            msg_id = self.next_msg_id()
        payload = {"cmd": cmd, "msgId": msg_id, "gwSn": self.gw["gwSn"], **fields}
        self._client.publish(self.pub_topic, json.dumps(payload), qos=0)
        return msg_id

    def recv(self, timeout: float = 5.0) -> dict | None:
        try:
            return self.inbox.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._client.loop_stop()
            self._client.disconnect()
