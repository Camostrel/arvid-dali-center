#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""energy_compare.py — ПАРАЛЛЕЛЬНАЯ СВЕРКА нашего расчётного энергоучёта с реальным реле.

ЗАЧЕМ. Наш учёт — РАСЧЁТНЫЙ: `P = power_w × кривая(яркость)`, отрезки суммируются в `EnergyStore`
(docs/ENERGY_CALC_MODEL.md). Проверить его можно только сравнением с прибором на входе 230 В —
у нас это реле Shelly, отдающее реальные Вт и накопленные Вт·ч.

Скрипт НИЧЕГО не меняет: раз в интервал снимает наши числа (WS `energy_data`/`energy_live`) и
состояния Shelly-сущностей, пишет строку в CSV и печатает нарастающее сравнение ДЕЛЬТ.

⚠⚠ ГЛАВНАЯ ЛОВУШКА (docs/ENERGY_CALC_MODEL.md §5): счётчик Вт·ч у реле КВАНТОВАН (тики
0.2–0.4 Вт·ч). На коротком прогоне невыпавший тик даёт до 6–8 % ложной «ошибки модели» — мы на это
уже попались однажды. Поэтому:
  • ЭНЕРГИЮ сверять ДЛИННЫМ прогоном (час+), по ДЕЛЬТАМ за окно;
  • ФОРМУ КРИВОЙ сверять МГНОВЕННОЙ мощностью (колонки `our_w` / `relay_w`) — там квантования нет.

Запуск на боксе (в фоне — прогон нужен длинный):
  export HA_TOKEN="..."
  nohup python3 energy_compare.py --devsn 251026828F6CAB35 --gw E22435088727 \
      --relay-energy sensor.vrn_shelly_2pm_01_output_1_energy \
      --relay-power  sensor.vrn_shelly_2pm_01_output_1_power \
      --relay-switch switch.vrn_shelly_2pm_01_output_1 \
      --lamp light.l_1_1_3 --interval 60 --out /config/tools/energy_l_1_1_3.csv &
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import socket
import ssl
import struct
import sys
import time
from datetime import datetime

HA_URL = "ws://localhost:8123/api/websocket"


class WS:
    """Минимальный синхронный WebSocket-клиент (stdlib), под HA WS API."""
    def __init__(self, url):
        sec = url.startswith("wss://")
        host_path = url.split("://", 1)[1]
        hostport, _, path = host_path.partition("/")
        host, _, port = hostport.partition(":")
        self.host, self.port = host, int(port or (443 if sec else 80))
        self.path, self.sec, self.buf, self._id = "/" + path, sec, b"", 0

    def connect(self):
        s = socket.create_connection((self.host, self.port), timeout=15)
        if self.sec:
            s = ssl.create_default_context().wrap_socket(s, server_hostname=self.host)
        self.sock = s
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {self.path} HTTP/1.1\r\nHost: {self.host}:{self.port}\r\n"
                   "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += s.recv(4096)
        if b" 101 " not in resp.split(b"\r\n", 1)[0]:
            raise RuntimeError("WS handshake отклонён: " + resp[:80].decode("latin1"))
        self.buf = resp.split(b"\r\n\r\n", 1)[1]

    def _send(self, obj):
        pl = json.dumps(obj).encode()
        b = bytearray([0x81]); m = os.urandom(4); n = len(pl)
        if n < 126: b.append(0x80 | n)
        elif n < 65536: b.append(0x80 | 126); b += struct.pack("!H", n)
        else: b.append(0x80 | 127); b += struct.pack("!Q", n)
        b += m; b += bytes(c ^ m[i % 4] for i, c in enumerate(pl))
        self.sock.sendall(bytes(b))

    def _frame(self):
        while True:
            d = self.buf
            if len(d) >= 2:
                op = d[0] & 0x0f; ln = d[1] & 0x7f; off = 2
                if ln == 126 and len(d) >= 4: ln = struct.unpack("!H", d[2:4])[0]; off = 4
                elif ln == 127 and len(d) >= 10: ln = struct.unpack("!Q", d[2:10])[0]; off = 10
                elif ln >= 126: off = None
                if off is not None and len(d) >= off + ln:
                    pl = d[off:off + ln]; self.buf = d[off + ln:]
                    if op == 0x8: raise RuntimeError("WS закрыт сервером")
                    if op == 0x9: self._pong(pl); continue    # ping → pong
                    if op == 0xA: continue                    # pong → игнор
                    return pl
            ch = self.sock.recv(65536)
            if not ch: raise RuntimeError("WS соединение оборвано")
            self.buf += ch

    def _pong(self, payload):
        b = bytearray([0x8A]); m = os.urandom(4); n = len(payload)
        b.append(0x80 | n); b += m; b += bytes(c ^ m[i % 4] for i, c in enumerate(payload))
        self.sock.sendall(bytes(b))

    def _recv(self):
        return json.loads(self._frame())

    def auth(self, token):
        self._recv()                              # auth_required
        self._send({"type": "auth", "access_token": token})
        if self._recv().get("type") != "auth_ok":
            raise RuntimeError("HA не принял токен")

    def cmd(self, type_, **kw):
        self._id += 1; mid = self._id
        self._send({"id": mid, "type": type_, **kw})
        while True:
            m = self._recv()
            if m.get("id") != mid or m.get("type") != "result":
                continue
            if not m.get("success"):
                raise RuntimeError(f"{type_}: {m.get('error')}")
            return m.get("result", {})



def _num(v):
    """Состояние HA → число (None, если пусто/недоступно)."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def snapshot(ws, args):
    """Один срез: наши числа + числа реле + состояние лампы в HA."""
    # НАШ накопитель по лампе (ключ — devSn) и НАША мгновенная мощность
    our_wh = our_w = power_w = None
    model = None
    for lamp in (ws.cmd("arvid_dali_center/energy_data", gw_sn=args.gw).get("lamps") or []):
        if str(lamp.get("devSn")) == args.devsn:
            our_wh, power_w, model = lamp.get("energy_wh"), lamp.get("power_w"), lamp.get("model")
            break
    # ⚠ ключ ответа — `energy` (карта devSn → {power_w, total_wh, on_time_h}), не `lamps`
    live = ws.cmd("arvid_dali_center/energy_live", gw_sn=args.gw).get("energy") or {}
    rec = live.get(args.devsn)
    if isinstance(rec, dict):
        our_w = rec.get("power_w")
        if our_wh is None:                      # фолбэк, если lamps не отдал накопитель
            our_wh = rec.get("total_wh")

    # состояния HA: реле (энергия/мощность/вкл) и наша лампа (вкл + яркость)
    states = {s["entity_id"]: s for s in ws.cmd("get_states")}

    def st(eid):
        return states.get(eid, {}) if eid else {}

    relay_e = st(args.relay_energy).get("state")
    relay_p = st(args.relay_power).get("state")
    relay_sw = st(args.relay_switch).get("state")
    lamp_st = st(args.lamp)
    bri = (lamp_st.get("attributes") or {}).get("brightness")
    return {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "our_wh": our_wh, "our_w": our_w, "power_w": power_w, "model": model,
        "relay_wh": _num(relay_e), "relay_w": _num(relay_p), "relay_on": relay_sw,
        "lamp_state": lamp_st.get("state"), "lamp_bri": bri,
    }


def main():
    ap = argparse.ArgumentParser(description="Сверка расчётной энергии с реле (Shelly).")
    ap.add_argument("--devsn", required=True, help="devSn лампы (ключ нашего накопителя)")
    ap.add_argument("--gw", required=True, help="серийник шлюза")
    ap.add_argument("--relay-energy", required=True, help="сущность накопленной энергии реле")
    ap.add_argument("--relay-power", required=True, help="сущность мгновенной мощности реле")
    ap.add_argument("--relay-switch", default="", help="сущность самого реле (для диагностики)")
    ap.add_argument("--lamp", default="", help="light.* нашей лампы (состояние/яркость)")
    ap.add_argument("--interval", type=float, default=60.0, help="период съёма, сек")
    ap.add_argument("--out", default="energy_compare.csv")
    ap.add_argument("--ha-url", default=HA_URL)
    ap.add_argument("--ha-token", default=os.environ.get("HA_TOKEN", ""))
    a = ap.parse_args()
    if not a.ha_token:
        sys.exit("нет токена: export HA_TOKEN=...")

    ws = WS(a.ha_url); ws.connect(); ws.auth(a.ha_token)
    first = snapshot(ws, a)
    if first["power_w"] is None:
        print("⚠ у лампы НЕ задана полная мощность (power_w) — наш расчёт будет 0 Вт·ч.\n"
              "  Задайте в карточке: Энергия → Параметры ламп (иначе сверять нечего).")
    print(f"старт: наш накопитель {first['our_wh']} Вт·ч · реле {first['relay_wh']} Вт·ч · "
          f"power_w={first['power_w']} кривая={first['model']}")
    print(f"пишу в {a.out}, интервал {a.interval:.0f}с. Ctrl+C — стоп.\n")

    cols = ["ts", "our_wh", "our_w", "relay_wh", "relay_w", "relay_on", "lamp_state",
            "lamp_bri", "power_w", "model", "d_our_wh", "d_relay_wh", "ratio"]
    new = not os.path.exists(a.out)
    with open(a.out, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if new:
            w.writeheader()
        base = first
        while True:
            try:
                cur = snapshot(ws, a)
            except Exception as err:                       # обрыв WS — переподключаемся, не падаем
                print(f"  ⚠ {err} — переподключение")
                time.sleep(5)
                try:
                    ws = WS(a.ha_url); ws.connect(); ws.auth(a.ha_token)
                except Exception as e2:
                    print(f"  ⚠ не удалось: {e2}")
                time.sleep(a.interval)
                continue
            # ДЕЛЬТЫ от начала прогона — сравнивать надо приросты, не абсолютные значения
            d_our = (cur["our_wh"] - base["our_wh"]) if None not in (cur["our_wh"], base["our_wh"]) else None
            d_rel = (cur["relay_wh"] - base["relay_wh"]) if None not in (cur["relay_wh"], base["relay_wh"]) else None
            ratio = (d_our / d_rel) if (d_our is not None and d_rel) else None
            row = {**cur, "d_our_wh": None if d_our is None else round(d_our, 4),
                   "d_relay_wh": None if d_rel is None else round(d_rel, 4),
                   "ratio": None if ratio is None else round(ratio, 3)}
            w.writerow({k: row.get(k) for k in cols}); f.flush()
            fmt = lambda v: "  —  " if v is None else f"{v:6.1f}"
            print(f"{cur['ts']}  наш {fmt(cur['our_w'])} Вт / реле {fmt(cur['relay_w'])} Вт  ·  "
                  f"Δ наш {0 if d_our is None else d_our:7.3f} Вт·ч / Δ реле "
                  f"{0 if d_rel is None else d_rel:7.3f} Вт·ч  ·  "
                  f"отношение {'—' if ratio is None else f'{ratio:.3f}'}  ·  "
                  f"лампа {cur['lamp_state']}/{cur['lamp_bri']} реле {cur['relay_on']}")
            time.sleep(a.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nостановлено")
