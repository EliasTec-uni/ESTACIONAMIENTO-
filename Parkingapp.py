"""
parking_app.py
==============================================================
 SISTEMA DE GESTIÓN DE ESTACIONAMIENTO
 Hardware : ESP32 + Sensores HC-SR04 (x10)
 Protocolo: WiFi / MQTT
 GUI      : tkinter (incluido en Python estándar)
==============================================================
 Dependencia externa: pip install paho-mqtt
==============================================================
"""

import tkinter as tk
from tkinter import messagebox
import threading
import random
import math
import time
import json
import logging
import os
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from collections import deque

# ── Logging básico ────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
#  CONFIGURACIÓN — edita aquí sin tocar el resto del código
# ══════════════════════════════════════════════════════════════════════════════

# ── MQTT ──────────────────────────────────────────────────────────────────────
MQTT_BROKER    = "broker.hivemq.com"
MQTT_PORT      = 1883
MQTT_CLIENT_ID = "parking_supervisor"
TOPIC_SENSOR   = "parking/esp32/sensor"   # + /N
TOPIC_STATUS   = "parking/esp32/status"
TOPIC_CMD      = "parking/esp32/cmd"

# ── Estacionamiento ───────────────────────────────────────────────────────────
NUM_SPACES             = 10
SPACE_LABELS           = [f"L{i+1:02d}" for i in range(NUM_SPACES)]
OCCUPIED_THRESHOLD_CM  = 15.0   # cm — por debajo = ocupado

# ── Interfaz ──────────────────────────────────────────────────────────────────
APP_TITLE   = "Sistema de Gestión de Estacionamiento"
APP_W, APP_H = 1150, 730
REFRESH_MS  = 500

# ── Colores (tema industrial oscuro) ─────────────────────────────────────────
C_BG       = "#0D1117"
C_SURFACE  = "#161B22"
C_BORDER   = "#30363D"
C_ACCENT   = "#00D9FF"
C_FREE     = "#2EA043"
C_OCC      = "#DA3633"
C_UNKNOWN  = "#6E7681"
C_TEXT     = "#E6EDF3"
C_DIM      = "#8B949E"
C_WARN     = "#F0883E"

# ── Modo simulación ───────────────────────────────────────────────────────────
SIMULATION_MODE = True    # True = sin ESP32 físico, False = MQTT real

# ── Fuentes ───────────────────────────────────────────────────────────────────
F_TITLE   = ("Consolas", 18, "bold")
F_SECTION = ("Consolas", 10, "bold")
F_BODY    = ("Consolas", 10)
F_SMALL   = ("Consolas", 9)
F_BIG     = ("Consolas", 26, "bold")
F_LABEL   = ("Consolas", 8, "bold")

# ══════════════════════════════════════════════════════════════════════════════
#  MODELOS DE DATOS
# ══════════════════════════════════════════════════════════════════════════════

class SpaceStatus(Enum):
    UNKNOWN  = "unknown"
    FREE     = "free"
    OCCUPIED = "occupied"


@dataclass
class ParkingSpace:
    space_id       : int
    label          : str
    status         : SpaceStatus = SpaceStatus.UNKNOWN
    distance_cm    : Optional[float] = None
    last_update    : Optional[datetime] = None
    occupancy_count: int = 0

    def update(self, data: dict):
        self.distance_cm = data.get("distance_cm")
        occ = data.get("occupied")
        prev = self.status
        if occ is not None:
            self.status = SpaceStatus.OCCUPIED if occ else SpaceStatus.FREE
        self.last_update = datetime.now()
        if self.status == SpaceStatus.OCCUPIED and prev != SpaceStatus.OCCUPIED:
            self.occupancy_count += 1
        return prev != self.status   # True si cambió el estado

    @property
    def stale(self):
        if self.last_update is None:
            return True
        return (datetime.now() - self.last_update).total_seconds() > 30


@dataclass
class ESPStatus:
    connected : bool = False
    ip        : str  = "—"
    rssi      : int  = 0
    uptime_s  : int  = 0
    last_seen : Optional[datetime] = None

    def update(self, data: dict):
        self.ip       = data.get("ip", "—")
        self.rssi     = data.get("rssi", 0)
        self.uptime_s = data.get("uptime_s", 0)
        self.last_seen = datetime.now()
        self.connected = True

    @property
    def uptime_str(self):
        h, r = divmod(self.uptime_s, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    @property
    def rssi_label(self):
        if self.rssi >= -60: return "Excelente"
        if self.rssi >= -70: return "Buena"
        if self.rssi >= -80: return "Regular"
        return "Débil"


# ══════════════════════════════════════════════════════════════════════════════
#  SIMULADOR DE ESP32
# ══════════════════════════════════════════════════════════════════════════════

class ESP32Simulator:
    """Genera datos falsos de HC-SR04 para pruebas sin hardware."""

    def __init__(self, on_sensor, on_status):
        self._on_sensor = on_sensor
        self._on_status = on_status
        self._running   = False
        self._occupied  = [random.random() < 0.4 for _ in range(NUM_SPACES)]
        self._uptime    = 0

    def start(self):
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def stop(self):
        self._running = False

    def _loop(self):
        tick = 0
        while self._running:
            self._uptime += 1
            tick         += 1
            for i in range(NUM_SPACES):
                if random.random() < 0.025:
                    self._occupied[i] = not self._occupied[i]
                base = 8.0 if self._occupied[i] else 45.0
                dist = max(2.0, base + random.gauss(0, 2.5))
                self._on_sensor(i, {
                    "space"      : i,
                    "distance_cm": round(dist, 2),
                    "occupied"   : dist < OCCUPIED_THRESHOLD_CM,
                    "timestamp"  : int(time.time()),
                })
            if tick % 5 == 0:
                self._on_status({
                    "ip"      : "192.168.1.50",
                    "rssi"    : -58 + int(6 * math.sin(tick / 8)),
                    "uptime_s": self._uptime,
                })
            time.sleep(1.0)


# ══════════════════════════════════════════════════════════════════════════════
#  CLIENTE MQTT
# ══════════════════════════════════════════════════════════════════════════════

class MQTTManager:
    """Comunicación MQTT con el ESP32. Requiere: pip install paho-mqtt"""

    def __init__(self, on_sensor, on_status, on_conn):
        self._on_sensor = on_sensor
        self._on_status = on_status
        self._on_conn   = on_conn
        self._client    = None
        self.connected  = False

        try:
            import paho.mqtt.client as mqtt
            self._mqtt = mqtt
            self._available = True
        except ImportError:
            self._available = False
            logger.warning("paho-mqtt no instalado. Ejecuta: pip install paho-mqtt")

    def connect(self):
        if not self._available:
            self._on_conn(False, "paho-mqtt no instalado")
            return
        threading.Thread(target=self._connect_thread, daemon=True).start()

    def _connect_thread(self):
        try:
            self._client = self._mqtt.Client(client_id=MQTT_CLIENT_ID)
            self._client.on_connect    = self._cb_connect
            self._client.on_disconnect = self._cb_disconnect
            self._client.on_message    = self._cb_message
            self._client.connect(MQTT_BROKER, MQTT_PORT, 60)
            self._client.loop_forever()
        except Exception as e:
            self._on_conn(False, str(e))

    def _cb_connect(self, client, ud, flags, rc):
        ok = rc == 0
        self.connected = ok
        if ok:
            client.subscribe(f"{TOPIC_SENSOR}/#")
            client.subscribe(TOPIC_STATUS)
        msgs = {0:"Conectado",1:"Protocolo incorrecto",2:"ID rechazado",
                3:"Broker no disponible",4:"Credenciales incorrectas",5:"No autorizado"}
        self._on_conn(ok, msgs.get(rc, f"rc={rc}"))

    def _cb_disconnect(self, client, ud, rc):
        self.connected = False
        self._on_conn(False, "Desconectado del broker")

    def _cb_message(self, client, ud, msg):
        try:
            data = json.loads(msg.payload.decode())
        except Exception:
            return
        if msg.topic.startswith(TOPIC_SENSOR):
            try:
                sid = int(msg.topic.split("/")[-1])
                if 0 <= sid < NUM_SPACES:
                    self._on_sensor(sid, data)
            except ValueError:
                pass
        elif msg.topic == TOPIC_STATUS:
            self._on_status(data)

    def disconnect(self):
        if self._client:
            try: self._client.disconnect()
            except: pass

    def send_command(self, cmd: str):
        if not self.connected or not self._client:
            return False
        try:
            self._client.publish(TOPIC_CMD,
                json.dumps({"cmd": cmd, "ts": datetime.now().isoformat()}), qos=1)
            return True
        except Exception:
            return False


# ══════════════════════════════════════════════════════════════════════════════
#  WIDGETS DE LA GUI
# ══════════════════════════════════════════════════════════════════════════════

def _status_color(status: SpaceStatus) -> str:
    return {SpaceStatus.FREE: C_FREE, SpaceStatus.OCCUPIED: C_OCC,
            SpaceStatus.UNKNOWN: C_UNKNOWN}[status]


class SpaceCard(tk.Frame):
    """Tarjeta visual de un lugar de estacionamiento."""

    _ICON = {SpaceStatus.FREE:"▣", SpaceStatus.OCCUPIED:"■", SpaceStatus.UNKNOWN:"□"}
    _TEXT = {SpaceStatus.FREE:"LIBRE", SpaceStatus.OCCUPIED:"OCUPADO", SpaceStatus.UNKNOWN:"SIN SEÑAL"}

    def __init__(self, parent, space: ParkingSpace):
        super().__init__(parent, bg=C_SURFACE, highlightthickness=2,
                         highlightbackground=C_BORDER, width=170, height=155)
        self.pack_propagate(False)
        self.space = space
        self._build()

    def _build(self):
        tk.Label(self, text=self.space.label, font=F_SECTION,
                 bg=C_SURFACE, fg=C_ACCENT).pack(pady=(10, 2))
        self._icon   = tk.Label(self, text="□", font=("Consolas", 30),
                                bg=C_SURFACE, fg=C_UNKNOWN)
        self._icon.pack()
        self._status = tk.Label(self, text="SIN SEÑAL", font=F_LABEL,
                                bg=C_SURFACE, fg=C_UNKNOWN)
        self._status.pack()
        self._dist   = tk.Label(self, text="— cm", font=F_SMALL,
                                bg=C_SURFACE, fg=C_DIM)
        self._dist.pack(pady=(2, 4))
        self._led    = tk.Label(self, text="●", font=("Consolas", 7),
                                bg=C_SURFACE, fg=C_UNKNOWN)
        self._led.pack(pady=(0, 6))

    def refresh(self):
        s     = self.space
        color = _status_color(s.status)
        self._icon  .config(text=self._ICON[s.status], fg=color)
        self._status.config(text=self._TEXT[s.status], fg=color)
        self.config(highlightbackground=color if not s.stale else C_BORDER)
        if s.distance_cm is not None:
            self._dist.config(text=f"{s.distance_cm:.1f} cm",
                              fg=C_DIM if not s.stale else C_WARN)
        self._led.config(fg=C_FREE if not s.stale else C_WARN)


# ══════════════════════════════════════════════════════════════════════════════
#  APLICACIÓN PRINCIPAL
# ══════════════════════════════════════════════════════════════════════════════

class ParkingApp:
    """Controlador principal — GUI + modelo + comunicación."""

    def __init__(self):
        # Modelo
        self._esp    = ESPStatus()
        self._spaces = [ParkingSpace(i, SPACE_LABELS[i]) for i in range(NUM_SPACES)]
        self._lock   = threading.Lock()
        self._log_q  : deque = deque()

        # Modo activo
        self._sim_mode : bool = SIMULATION_MODE   # True=simulación, False=real

        # Comunicación
        self._sim  : Optional[ESP32Simulator] = None
        self._mqtt : Optional[MQTTManager]    = None

        # GUI
        self._root   = None
        self._cards  = []

    # ── Propiedades del lote ──────────────────────────────────────────────────

    @property
    def _occupied_count(self): return sum(1 for s in self._spaces if s.status == SpaceStatus.OCCUPIED)
    @property
    def _free_count(self):     return sum(1 for s in self._spaces if s.status == SpaceStatus.FREE)
    @property
    def _occ_pct(self):        return (self._occupied_count / NUM_SPACES) * 100

    # ── Construcción de la GUI ────────────────────────────────────────────────

    def _build(self):
        self._root = tk.Tk()
        self._root.title(APP_TITLE)
        self._root.geometry(f"{APP_W}x{APP_H}")
        self._root.configure(bg=C_BG)
        self._root.resizable(True, True)
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_header()
        self._build_body()
        self._build_statusbar()

    # ── Header ────────────────────────────────────────────────────────────────

    def _build_header(self):
        tk.Frame(self._root, height=3, bg=C_ACCENT).pack(fill="x")

        bar = tk.Frame(self._root, bg=C_SURFACE)
        bar.pack(fill="x")

        tk.Label(bar, text=f"⬡  {APP_TITLE}", font=F_TITLE,
                 bg=C_SURFACE, fg=C_TEXT).pack(side="left", padx=16, pady=10)

        right = tk.Frame(bar, bg=C_SURFACE)
        right.pack(side="right", padx=16)

        mode_color = C_WARN if SIMULATION_MODE else C_FREE
        mode_text  = "[ SIMULACIÓN ]" if SIMULATION_MODE else "[ ESP32 REAL ]"
        self._lbl_mode = tk.Label(right, text=mode_text, font=F_SMALL,
                                  bg=C_SURFACE, fg=mode_color)
        self._lbl_mode.grid(row=0, column=0, padx=10)

        tk.Label(right, text="MQTT:", font=F_SMALL,
                 bg=C_SURFACE, fg=C_DIM).grid(row=0, column=1)
        self._lbl_mqtt = tk.Label(right, text="● DESCONECTADO",
                                  font=F_SMALL, bg=C_SURFACE, fg=C_OCC)
        self._lbl_mqtt.grid(row=0, column=2, padx=(4, 14))

        tk.Label(right, text="ESP32:", font=F_SMALL,
                 bg=C_SURFACE, fg=C_DIM).grid(row=0, column=3)
        self._lbl_esp = tk.Label(right, text="● OFFLINE",
                                 font=F_SMALL, bg=C_SURFACE, fg=C_OCC)
        self._lbl_esp.grid(row=0, column=4)

        tk.Frame(self._root, height=1, bg=C_BORDER).pack(fill="x")

    # ── Cuerpo ────────────────────────────────────────────────────────────────

    def _build_body(self):
        body = tk.Frame(self._root, bg=C_BG)
        body.pack(fill="both", expand=True)

        # ── Columna izquierda ─────────────────────────────────────────────────
        left = tk.Frame(body, bg=C_BG)
        left.pack(side="left", fill="both", expand=True)

        # Cuadrícula de lugares
        tk.Label(left, text="MAPA DE ESTACIONAMIENTO", font=F_SECTION,
                 bg=C_BG, fg=C_DIM).pack(anchor="w", padx=16, pady=(12, 6))

        grid_frame = tk.Frame(left, bg=C_BG)
        grid_frame.pack(padx=16)

        COLS = 5
        for idx, space in enumerate(self._spaces):
            row, col = divmod(idx, COLS)
            card = SpaceCard(grid_frame, space)
            card.grid(row=row, column=col, padx=5, pady=5)
            self._cards.append(card)

        # Log de eventos
        tk.Frame(left, height=1, bg=C_BORDER).pack(fill="x", padx=16, pady=(10, 0))

        log_header = tk.Frame(left, bg=C_BG)
        log_header.pack(fill="x", padx=16, pady=(6, 4))
        tk.Label(log_header, text="REGISTRO DE EVENTOS", font=F_SECTION,
                 bg=C_BG, fg=C_DIM).pack(side="left")
        tk.Button(log_header, text="Limpiar", font=F_SMALL, bg=C_BORDER,
                  fg=C_DIM, relief="flat", cursor="hand2",
                  command=self._clear_log).pack(side="right")

        log_frame = tk.Frame(left, bg=C_BG)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 10))

        sb = tk.Scrollbar(log_frame)
        sb.pack(side="right", fill="y")
        self._log_text = tk.Text(log_frame, bg="#0A0E14", fg=C_DIM,
                                 font=F_SMALL, relief="flat", wrap="word",
                                 state="disabled", yscrollcommand=sb.set)
        self._log_text.pack(fill="both", expand=True)
        sb.config(command=self._log_text.yview)

        for tag, color in [("OK", C_FREE), ("WARN", C_WARN),
                           ("ERROR", C_OCC), ("INFO", C_DIM), ("TS", C_ACCENT)]:
            self._log_text.tag_config(tag, foreground=color)

        # ── Separador vertical ────────────────────────────────────────────────
        tk.Frame(body, width=1, bg=C_BORDER).pack(side="left", fill="y")

        # ── Columna derecha ───────────────────────────────────────────────────
        right = tk.Frame(body, bg=C_SURFACE, width=285)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        self._build_stats(right)
        self._build_controls(right)

    # ── Panel de estadísticas ─────────────────────────────────────────────────

    def _build_stats(self, parent):
        tk.Label(parent, text="ESTADÍSTICAS", font=F_SECTION,
                 bg=C_SURFACE, fg=C_DIM).pack(anchor="w", padx=14, pady=(14, 8))

        counters = tk.Frame(parent, bg=C_SURFACE)
        counters.pack(fill="x", padx=14)

        self._stat_vals = {}
        for key, label, color in [("free","LIBRES",C_FREE),
                                   ("occ","OCUPADOS",C_OCC),
                                   ("pct","OCUPACIÓN",C_ACCENT)]:
            box = tk.Frame(counters, bg=C_BG, pady=6)
            box.pack(side="left", expand=True, fill="both", padx=3)
            v = tk.Label(box, text="—", font=F_BIG, bg=C_BG, fg=color)
            v.pack()
            tk.Label(box, text=label, font=F_LABEL, bg=C_BG, fg=C_DIM).pack()
            self._stat_vals[key] = v

        # Barra de ocupación
        tk.Label(parent, text="NIVEL DE OCUPACIÓN", font=F_LABEL,
                 bg=C_SURFACE, fg=C_DIM).pack(anchor="w", padx=14, pady=(14, 4))

        bar_bg = tk.Frame(parent, bg=C_BORDER, height=16)
        bar_bg.pack(fill="x", padx=14)
        bar_bg.pack_propagate(False)
        self._bar = tk.Frame(bar_bg, bg=C_FREE, height=16)
        self._bar.place(x=0, y=0, relheight=1.0, relwidth=0.0)

        self._lbl_pct = tk.Label(parent, text="0%", font=F_SMALL,
                                 bg=C_SURFACE, fg=C_TEXT)
        self._lbl_pct.pack(anchor="e", padx=14)

        # Info ESP32
        tk.Frame(parent, height=1, bg=C_BORDER).pack(fill="x", padx=14, pady=10)
        tk.Label(parent, text="ESP32  /  HC-SR04", font=F_SECTION,
                 bg=C_SURFACE, fg=C_DIM).pack(anchor="w", padx=14)
        self._lbl_esp_info = tk.Label(parent, text="Esperando datos...",
                                      font=F_SMALL, bg=C_SURFACE, fg=C_DIM,
                                      justify="left")
        self._lbl_esp_info.pack(anchor="w", padx=14, pady=4)

        # Reloj
        tk.Frame(parent, height=1, bg=C_BORDER).pack(fill="x", padx=14, pady=6)
        self._lbl_clock = tk.Label(parent, text="", font=("Consolas", 13, "bold"),
                                   bg=C_SURFACE, fg=C_ACCENT)
        self._lbl_clock.pack(anchor="w", padx=14)

    # ── Panel de controles ────────────────────────────────────────────────────

    def _build_controls(self, parent):
        tk.Frame(parent, height=1, bg=C_BORDER).pack(fill="x", padx=14, pady=(10, 0))
        tk.Label(parent, text="CONTROLES", font=F_SECTION,
                 bg=C_SURFACE, fg=C_DIM).pack(anchor="w", padx=14, pady=(8, 6))

        # Botón principal: alternar modo simulación ↔ monitoreo real
        self._btn_toggle = tk.Button(
            parent,
            text="▶  Iniciar Monitoreo Real",
            font=("Consolas", 10, "bold"),
            bg=C_FREE, fg=C_BG,
            activebackground="#1a6b2a", activeforeground=C_BG,
            relief="flat", cursor="hand2", padx=10, pady=8, width=22,
            command=self._cmd_toggle_mode
        )
        self._btn_toggle.pack(pady=(0, 6))

        tk.Frame(parent, height=1, bg=C_BORDER).pack(fill="x", padx=14, pady=(2, 6))

        for text, color, cmd in [
            ("Reconectar MQTT",     C_ACCENT,  self._cmd_reconnect),
            ("Reiniciar ESP32",     C_WARN,    self._cmd_reset_esp),
            ("Leer todos sensores", C_FREE,    self._cmd_read_all),
            ("Exportar reporte",    C_DIM,     self._cmd_export),
        ]:
            tk.Button(parent, text=text, font=F_BODY, bg=C_BG, fg=color,
                      activebackground=C_BORDER, activeforeground=color,
                      relief="flat", cursor="hand2", padx=10, pady=6, width=22,
                      command=cmd).pack(pady=3)

    # ── Barra de estado ───────────────────────────────────────────────────────

    def _build_statusbar(self):
        bar = tk.Frame(self._root, bg=C_BORDER, height=24)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self._statusbar = tk.Label(bar, text="Sistema iniciado", font=F_SMALL,
                                   bg=C_BORDER, fg=C_DIM)
        self._statusbar.pack(side="left", padx=12)
        tk.Label(bar, text="ESP32 + HC-SR04  |  WiFi / MQTT  |  Python",
                 font=F_SMALL, bg=C_BORDER, fg=C_DIM).pack(side="right", padx=12)

    # ── Callbacks de datos (vienen de hilos externos) ─────────────────────────

    def _on_sensor(self, space_id: int, data: dict):
        with self._lock:
            changed = self._spaces[space_id].update(data)
            if changed:
                s   = self._spaces[space_id]
                txt = (f"{s.label}: → {s.status.value.upper()}"
                       f"  ({data.get('distance_cm', 0):.1f} cm)")
                lvl = "WARN" if s.status == SpaceStatus.OCCUPIED else "OK"
                self._log_q.append((txt, lvl))

    def _on_status(self, data: dict):
        with self._lock:
            self._esp.update(data)

    def _on_mqtt_conn(self, connected: bool, msg: str):
        self._log_q.append((f"MQTT: {msg}", "OK" if connected else "ERROR"))
        if self._root:
            color = C_FREE if connected else C_OCC
            text  = f"● {msg[:35]}"
            self._root.after(0, lambda: self._lbl_mqtt.config(text=text, fg=color))

    # ── Comandos del operador ─────────────────────────────────────────────────

    def _cmd_reconnect(self):
        self._log("Reconectando MQTT...", "INFO")
        if self._mqtt:
            self._mqtt.disconnect()
            self._mqtt.connect()

    def _cmd_reset_esp(self):
        if messagebox.askyesno("Reiniciar ESP32",
                               "¿Enviar comando RESET al ESP32?"):
            if self._mqtt and self._mqtt.send_command("reset"):
                self._log("Comando RESET enviado al ESP32", "WARN")
            else:
                self._log("Sin conexión MQTT — comando no enviado", "ERROR")

    def _cmd_read_all(self):
        if self._mqtt:
            self._mqtt.send_command("read_all")
            self._log("Solicitud de lectura enviada al ESP32", "INFO")
        else:
            self._log("Simulación activa — lectura forzada no aplica", "WARN")

    def _cmd_toggle_mode(self):
        """Alterna entre simulación y monitoreo real via MQTT."""
        if self._sim_mode:
            # ── Detener simulación → iniciar MQTT real ──────────────────────
            if messagebox.askyesno(
                "Cambiar a Monitoreo Real",
                "¿Detener la simulación e iniciar monitoreo real con el ESP32?\n\n"
                f"Broker MQTT: {MQTT_BROKER}:{MQTT_PORT}"
            ):
                self._sim_mode = False
                # Detener simulador
                if self._sim:
                    self._sim.stop()
                    self._sim = None
                # Resetear estados de los sensores
                for s in self._spaces:
                    s.status      = SpaceStatus.UNKNOWN
                    s.distance_cm = None
                    s.last_update = None
                self._esp = ESPStatus()
                # Actualizar UI
                self._btn_toggle.config(
                    text="⏹  Volver a Simulación",
                    bg=C_WARN, fg=C_BG,
                    activebackground="#8a4f1a"
                )
                self._lbl_mode.config(text="[ ESP32 REAL ]", fg=C_FREE)
                self._lbl_mqtt.config(text="● CONECTANDO...", fg=C_WARN)
                self._log("Simulación detenida — iniciando conexión MQTT real", "WARN")
                # Conectar MQTT
                self._mqtt = MQTTManager(self._on_sensor, self._on_status, self._on_mqtt_conn)
                self._mqtt.connect()
        else:
            # ── Detener MQTT real → volver a simulación ─────────────────────
            if messagebox.askyesno(
                "Volver a Simulación",
                "¿Desconectar el ESP32 real y volver al modo simulación?"
            ):
                self._sim_mode = True
                # Desconectar MQTT
                if self._mqtt:
                    self._mqtt.disconnect()
                    self._mqtt = None
                # Resetear estados
                for s in self._spaces:
                    s.status      = SpaceStatus.UNKNOWN
                    s.distance_cm = None
                    s.last_update = None
                self._esp = ESPStatus()
                # Actualizar UI
                self._btn_toggle.config(
                    text="▶  Iniciar Monitoreo Real",
                    bg=C_FREE, fg=C_BG,
                    activebackground="#1a6b2a"
                )
                self._lbl_mode.config(text="[ SIMULACIÓN ]", fg=C_WARN)
                self._lbl_mqtt.config(text="● SIMULACIÓN", fg=C_WARN)
                self._lbl_esp.config(text="● OFFLINE", fg=C_OCC)
                self._log("Conexión MQTT cerrada — simulación reiniciada", "WARN")
                # Iniciar simulador
                self._sim = ESP32Simulator(self._on_sensor, self._on_status)
                self._sim.start()
                self._log("Simulador HC-SR04 activo", "OK")

    def _cmd_export(self):
        try:
            fname = f"reporte_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            with open(fname, "w", encoding="utf-8") as f:
                f.write(f"REPORTE DE ESTACIONAMIENTO — {datetime.now()}\n")
                f.write("=" * 55 + "\n\n")
                f.write(f"Lugares totales : {NUM_SPACES}\n")
                f.write(f"Ocupados        : {self._occupied_count}\n")
                f.write(f"Libres          : {self._free_count}\n")
                f.write(f"Ocupación       : {self._occ_pct:.1f}%\n\n")
                f.write("DETALLE POR LUGAR:\n")
                for s in self._spaces:
                    dist = f"{s.distance_cm:.1f} cm" if s.distance_cm else "  —   "
                    f.write(f"  {s.label}  {s.status.value.upper():10s}"
                            f"  {dist:>8}  Occ: {s.occupancy_count}\n")
            self._log(f"Reporte guardado: {fname}", "OK")
            messagebox.showinfo("Exportar", f"Archivo guardado:\n{fname}")
        except Exception as e:
            self._log(f"Error al exportar: {e}", "ERROR")

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _log(self, msg: str, level: str = "INFO"):
        """Log thread-safe desde cualquier hilo."""
        self._log_q.append((msg, level))

    def _flush_log(self):
        """Vacía la cola de log en el hilo de la GUI."""
        while self._log_q:
            msg, level = self._log_q.popleft()
            ts = datetime.now().strftime("%H:%M:%S")
            self._log_text.config(state="normal")
            self._log_text.insert("end", f"[{ts}] ", "TS")
            self._log_text.insert("end", f"{msg}\n", level.upper())
            self._log_text.see("end")
            lines = int(self._log_text.index("end-1c").split(".")[0])
            if lines > 200:
                self._log_text.delete("1.0", f"{lines-200}.0")
            self._log_text.config(state="disabled")

    def _clear_log(self):
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.config(state="disabled")

    # ── Loop de refresco ──────────────────────────────────────────────────────

    def _refresh(self):
        self._flush_log()

        # Header ESP32
        if self._esp.connected:
            self._lbl_esp.config(
                text=f"● {self._esp.ip}  {self._esp.rssi} dBm", fg=C_FREE)
        else:
            self._lbl_esp.config(text="● OFFLINE", fg=C_OCC)

        # Tarjetas
        for card in self._cards:
            card.refresh()

        # Estadísticas
        occ = self._occupied_count
        fre = self._free_count
        pct = self._occ_pct
        self._stat_vals["free"].config(text=str(fre))
        self._stat_vals["occ"] .config(text=str(occ))
        self._stat_vals["pct"] .config(text=f"{pct:.0f}%")

        rel = pct / 100
        bar_color = C_FREE if rel < 0.6 else (C_WARN if rel < 0.9 else C_OCC)
        self._bar.config(bg=bar_color)
        self._bar.place(relwidth=rel)
        self._lbl_pct.config(text=f"{pct:.1f}%")

        # Info ESP32
        if self._esp.connected:
            info = (f"IP: {self._esp.ip}\n"
                    f"RSSI: {self._esp.rssi} dBm ({self._esp.rssi_label})\n"
                    f"Uptime: {self._esp.uptime_str}")
        else:
            info = "Sin conexión con ESP32"
        self._lbl_esp_info.config(text=info)

        # Reloj
        self._lbl_clock.config(
            text=datetime.now().strftime("  %H:%M:%S  —  %d/%m/%Y"))

        # Barra inferior
        self._statusbar.config(
            text=(f"Libres: {fre}  |  Ocupados: {occ}  |  "
                  f"Ocupación: {pct:.0f}%  |  "
                  f"{datetime.now().strftime('%H:%M:%S')}"))

        self._root.after(REFRESH_MS, self._refresh)

    # ── Iniciar comunicación ──────────────────────────────────────────────────

    def _start_comm(self):
        if SIMULATION_MODE:
            self._log("Modo simulación activo — ESP32 simulado", "WARN")
            self._sim = ESP32Simulator(self._on_sensor, self._on_status)
            self._sim.start()
            self._log("Simulador HC-SR04 iniciado correctamente", "OK")
            # Marcar MQTT como "simulado"
            self._lbl_mqtt.config(text="● SIMULACIÓN", fg=C_WARN)
        else:
            self._log(f"Conectando a broker: {MQTT_BROKER}", "INFO")
            self._mqtt = MQTTManager(self._on_sensor, self._on_status, self._on_mqtt_conn)
            self._mqtt.connect()

    # ── Cierre ────────────────────────────────────────────────────────────────

    def _on_close(self):
        if messagebox.askokcancel("Salir", "¿Cerrar el sistema supervisor?"):
            if self._sim:   self._sim.stop()
            if self._mqtt:  self._mqtt.disconnect()
            self._root.destroy()

    # ── Punto de entrada ──────────────────────────────────────────────────────

    def run(self):
        self._build()
        self._log(f"Sistema iniciado — {NUM_SPACES} lugares configurados", "OK")
        self._log("Sensores: HC-SR04 × 10  |  Protocolo: WiFi / MQTT", "INFO")
        self._start_comm()
        self._root.after(REFRESH_MS, self._refresh)
        self._root.mainloop()
