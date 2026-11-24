# -*- coding: utf-8 -*-
"""
victory_monitor.py - GUI que da um hook no processo do jogo (victory.exe):

- Modo LAUNCH: inicia o victory.exe com os switches escolhidos e le o stdout
  ao vivo (o mesmo texto que iria para o log), parser cada burst de
  estatisticas por frame e mostra em tempo real.
- Modo ATTACH: acompanha o arquivo de log mais recente (ou um escolhido),
  util quando o jogo ja foi iniciado por fora.
- Ao fechar o jogo, salva um resumo JSON em summaries/ para comparacao.
- Botao COMPARE: compara os dois ultimos resumos (ou dois escolhidos).

Somente stdlib. Uso:
    python victory_monitor.py            # GUI
    python victory_monitor.py --check    # testa o parser contra os logs existentes
"""
import os
import re
import sys
import json
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog
from datetime import datetime
from collections import Counter, deque

STALL_S = 15.0          # segundos sem burst de fps antes de alertar
RECENT_N = 400          # linhas guardadas para o diagnostico do stall

def proc_commit_mb(pid):
    """(commit MB, peak commit MB) de um pid, via PowerShell (wmic saiu do Win11)."""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {pid} -ErrorAction Stop | "
             "Select-Object PagedMemorySize64,PeakPagedMemorySize64 | ConvertTo-Json)"],
            capture_output=True, text=True, timeout=8)
        d = json.loads(r.stdout)
        return (d["PagedMemorySize64"] / 1e6, d["PeakPagedMemorySize64"] / 1e6)
    except Exception:
        return None

# ---------------------------------------------------------------- constantes

APP_DIR = os.path.dirname(os.path.abspath(__file__))
FORK_DIR = os.path.join(APP_DIR, "victory-fork")
EXE = os.path.join(FORK_DIR, "target", "release", "victory.exe")
GAME_EXE = r"D:\Leonida\GTA6.Xbox.v1.0.2.1-BuildToRun\game_scarlett_master.exe"
LOG_DIR = os.path.expandvars(r"%LOCALAPPDATA%\victory\data\logs")
SUMMARY_DIR = os.path.join(APP_DIR, "summaries")

DEFAULT_SWITCHES = {
    "VICTORY_RELAY": "2",
    "VICTORY_LATE_WAIT": "3",
    "VICTORY_WIDE_BACK": "1",
    "VICTORY_CARD_BUFFERS_MIN_MB": "1",
    "VICTORY_CARD_BUFFERS_MB": "1024",
    "VICTORY_TEXTURE_NO_WAIT": "1",
}

ANSI = re.compile(r"\x1b\[[0-9;]*m")

# linha de fps: "... 31.0 fps, 32.3 ms/frame (0.4 preparing: copies 0.2 ms 0.1 MB, textures 0.0 ms 0.0 MB; 0.0 on the GPU; 0.0 copying back 0.0 MB), 3 draws/frame, dropped 0 without a host pipeline (0 being made) and 0 indirect"
FPS_RE = re.compile(
    r"([\d.]+) fps, ([\d.]+) ms/frame "
    r"\(([\d.]+) preparing: copies ([\d.]+) ms ([\d.]+) MB, "
    r"textures ([\d.]+) ms ([\d.]+) MB; "
    r"([\d.]+) on the GPU; "
    r"([\d.]+) copying back ([\d.]+) MB\), "
    r"([\d.]+) draws/frame, dropped ([\d.]+) without a host pipeline "
    r"\(([\d.]+) being made\) and ([\d.]+) indirect"
)

AUX_PATS = {
    "card_busy":    (re.compile(r"the card about ([\d.]+)% busy"), 0),
    "card_idle":    (re.compile(r"nothing of ours queued ([\d.]+) ms"), 0),
    "submit_wait":  (re.compile(r"stood waiting for the card ([\d.]+) ms"), 0),
    "tex_refresh":  (re.compile(r"texture refreshes per frame, all threads: ([\d.]+) ms"), 0),
    "hold_still":   (re.compile(r"([\d.]+) ms waiting for title memory to hold still"), 0),
    "shadow_snap":  (re.compile(r"CPU per frame: .*?([\d.]+) MB in \d+ ranges of title buffers"), 0),
    "heap_ms":      (re.compile(r"heap buffers ([\d.]+) passes ([\d.]+) ms"), 1),
    "nowait_kernel":(re.compile(r"([\d.]+) read through the kernel"), 0),
    "nowait_defer": (re.compile(r"([\d.]+) left for the next refresh"), 0),
    "tex_uploaded": (re.compile(r"(\d+) uploaded so far \((\d+) MB\)"), 0),
    "tex_waiting":  (re.compile(r"uploaded so far \(\d+ MB\), (\d+) waiting"), 0),
}

TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)\.(\d+)Z")

# ------------------------------------------------------------------- parser

def strip_ansi(line):
    return ANSI.sub("", line.rstrip("\n"))

def line_ts(line):
    m = TS_RE.match(line)
    if not m:
        return None
    h, mn, s = m.group(2).split(":")
    return int(h) * 3600 + int(mn) * 60 + int(s) + int(m.group(3)) / 1e6

def parse_line(line, aux):
    """Retorna um frame completo se a linha for a de fps, senao atualiza aux.

    Os patterns auxiliares rodam ANTES do retorno: a propria linha de fps
    carrega contadores acumulativos ("uploaded so far") que devem entrar
    nela mesma. O dict aux segue acumulado para as linhas seguintes.
    """
    for key, (pat, gi) in AUX_PATS.items():
        am = pat.search(line)
        if am:
            aux[key] = float(am.group(1 + gi))
    m = FPS_RE.search(line)
    if m:
        g = [float(x) for x in m.groups()]
        return {
            "t": line_ts(line),
            "fps": g[0], "ms": g[1],
            "prep": g[2], "copies": g[3], "copies_mb": g[4],
            "tex": g[5], "tex_mb": g[6],
            "gpu": g[7], "copyb": g[8], "copyb_mb": g[9],
            "draws": g[10], "drop": g[11], "making": g[12], "indirect": g[13],
            "aux": dict(aux),
        }, True
    return None, False

def verdict(frame):
    """Onde esta o topo do gargalo deste frame."""
    parts = {
        "GPU (draws)": frame["gpu"],
        "copy-back": frame["copyb"],
        "textures": frame["tex"],
        "copies up": frame["copies"],
        "drops x pipeline": frame["drop"] * 0.5,  # estimativa: cada drop ~0.5 ms de montagem
    }
    top = max(parts, key=parts.get)
    if frame["ms"] <= 100:
        return "ok"
    return f"{top} ({parts[top]:.0f} ms)"

# ------------------------------------------------------------ fonte de linhas

class LogSource(threading.Thread):
    """Le linhas de um arquivo em crescimento (tail -f) e empurra para a fila."""

    def __init__(self, path, out_queue, stop_event):
        super().__init__(daemon=True)
        self.path = path
        self.q = out_queue
        self.stop_event = stop_event

    def run(self):
        with open(self.path, encoding="utf-8", errors="replace") as f:
            f.seek(0, 2)  # comeca do fim
            while not self.stop_event.is_set():
                line = f.readline()
                if line:
                    self.q.put(strip_ansi(line))
                else:
                    time.sleep(0.15)

class ProcSource(threading.Thread):
    """Inicia o jogo e le o stdout dele ao vivo."""

    def __init__(self, exe, game_exe, switches, out_queue, stop_event, on_exit):
        super().__init__(daemon=True)
        self.switches = switches
        self.exe = exe
        self.game_exe = game_exe
        self.q = out_queue
        self.stop_event = stop_event
        self.on_exit = on_exit
        self.proc = None

    def run(self):
        env = dict(os.environ)
        env.update(self.switches)
        try:
            self.proc = subprocess.Popen(
                [self.exe, "run", self.game_exe, "--execute"],
                cwd=FORK_DIR, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
        except Exception as e:
            self.q.put(f"ERRO ao iniciar: {e}\n")
            self.on_exit()
            return
        self.q.put(f"__PID__ {self.proc.pid}\n")
        for line in self.proc.stdout:
            if self.stop_event.is_set():
                break
            self.q.put(strip_ansi(line))
        if self.proc.poll() is None and not self.stop_event.is_set():
            self.proc.terminate()
        self.q.put("__SAIU__\n")
        self.on_exit()

    def stop(self):
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

# ----------------------------------------------------------------- resumos

def summarize(frames, meta):
    slow = [f for f in frames if f["ms"] > 1000]
    mid = [f for f in frames if 100 < f["ms"] <= 1000]
    fast = [f for f in frames if f["ms"] <= 100]
    def med(fs, k):
        vals = [f[k] for f in fs]
        return round(sorted(vals)[len(vals) // 2], 1) if vals else 0.0
    aux_last = frames[-1]["aux"] if frames else {}
    return {
        "quando": meta.get("quando", datetime.now().isoformat(timespec="seconds")),
        "fonte": meta.get("fonte", "?"),
        "switches": meta.get("switches", {}),
        "bursts": len(frames),
        "rapidos": len(fast), "medios": len(mid), "lentos": len(slow),
        "mediana_ms": med(frames, "ms"),
        "lentos_mediana_ms": med(slow, "ms"),
        "lentos_max_ms": round(max((f["ms"] for f in slow), default=0)),
        "lentos": {k: med(slow, k) for k in ("prep", "tex", "gpu", "copyb", "copies", "drop")},
        "card_busy": aux_last.get("card_busy", 0),
        "hold_still": aux_last.get("hold_still", 0),
        "tex_refresh": aux_last.get("tex_refresh", 0),
        "tex_uploaded": aux_last.get("tex_uploaded", 0),
        "tex_waiting": aux_last.get("tex_waiting", 0),
        "nowait_kernel": aux_last.get("nowait_kernel", 0),
        "nowait_defer": aux_last.get("nowait_defer", 0),
    }

def compare_summaries(a, b):
    """Tabela de comparacao (a = antes, b = depois)."""
    keys = [
        ("lentos_mediana_ms", "mundo mediana (ms)", 0),
        ("lentos_max_ms", "mundo pior (ms)", 0),
        ("bursts", "bursts", 0),
        ("card_busy", "card busy (%)", 0),
        ("hold_still", "lock remap (ms)", 1),
        ("tex_refresh", "tex refresh (ms)", 1),
        ("tex_uploaded", "texturas upadas", 0),
        ("tex_waiting", "texturas na fila", 0),
        ("nowait_kernel", "NO_WAIT via kernel/frame", 1),
    ]
    out = [f"{'metrica':28s} {'antes':>12s} {'depois':>12s} {'delta':>10s}"]
    for k, label, _ in keys:
        va, vb = a.get(k, 0), b.get(k, 0)
        d = vb - va
        out.append(f"{label:28s} {va:>12} {vb:>12} {d:>+10}")
    sl_a, sl_b = a.get("lentos", {}), b.get("lentos", {})
    for k in ("prep", "tex", "gpu", "copyb", "copies", "drop"):
        va, vb = sl_a.get(k, 0), sl_b.get(k, 0)
        out.append(f"  lento.{k:22s} {va:>12} {vb:>12} {vb - va:>+10}")
    return "\n".join(out)

# --------------------------------------------------------------------- GUI

DARK_BG = "#12141a"
DARK_FG = "#d8dae0"
ACCENT = "#5ab4f0"

class MonitorGUI:
    def __init__(self, root):
        self.root = root
        root.title("Victory monitor - hook no processo")
        root.configure(bg=DARK_BG)
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.source = None
        self.frames = []
        self.aux = {}
        self.pending = None      # frame de fps esperando aux da mesma impressao
        self.pending_aux = {}    # aux acumulado apos o fps (mesma impressao)
        self.pid = None
        self.started_at = None
        self.log_path = None
        self.stall_on = False
        self.last_frame_wall = time.time()
        self.last_mem_poll = 0.0
        self.recent_lines = deque(maxlen=RECENT_N)
        self.var_status = tk.StringVar(value="parado")
        self.var_fps = tk.StringVar(value="-")
        self.var_ms = tk.StringVar(value="-")
        self.var_top = tk.StringVar(value="-")
        self.var_aux = tk.StringVar(value="-")
        self.var_queue = tk.StringVar(value="-")
        self.var_mem = tk.StringVar(value="-")
        self.build()
        self.root.after(100, self.poll)

    # ---------------------------------------------------------- interface
    def build(self):
        pad = dict(padx=6, pady=4)

        top = tk.Frame(self.root, bg=DARK_BG)
        top.pack(fill=tk.X, **pad)

        tk.Label(top, text="victory.exe:", bg=DARK_BG, fg=DARK_FG).pack(side=tk.LEFT)
        self.var_exe = tk.StringVar(value=EXE if os.path.exists(EXE) else FORK_DIR)
        tk.Entry(top, textvariable=self.var_exe, width=48, bg="#1c1f26", fg=DARK_FG,
                 insertbackground=DARK_FG, relief=tk.FLAT).pack(side=tk.LEFT, **pad)

        tk.Label(top, text="game exe:", bg=DARK_BG, fg=DARK_FG).pack(side=tk.LEFT)
        self.var_game = tk.StringVar(value=GAME_EXE)
        tk.Entry(top, textvariable=self.var_game, width=46, bg="#1c1f26", fg=DARK_FG,
                 insertbackground=DARK_FG, relief=tk.FLAT).pack(side=tk.LEFT, **pad)

        # switches
        swf = tk.LabelFrame(self.root, text=" switches de ambiente ", bg=DARK_BG, fg=DARK_FG)
        swf.pack(fill=tk.X, **pad)
        self.sw_vars = {}
        col = 0
        for k, v in DEFAULT_SWITCHES.items():
            var = tk.StringVar(value=v)
            self.sw_vars[k] = var
            cell = tk.Frame(swf, bg=DARK_BG)
            cell.grid(row=col // 3, column=col % 3, sticky="w", padx=8, pady=2)
            tk.Checkbutton(cell, text=k, variable=var, onvalue=v, offvalue="",
                           bg=DARK_BG, fg=DARK_FG, selectcolor="#1c1f26",
                           activebackground=DARK_BG, activeforeground=DARK_FG
                           ).pack(side=tk.LEFT)
            tk.Entry(cell, textvariable=var, bg="#1c1f26", fg=DARK_FG,
                     insertbackground=DARK_FG, relief=tk.FLAT, width=7).pack(side=tk.LEFT)
            col += 1

        # botoes
        btns = tk.Frame(self.root, bg=DARK_BG)
        btns.pack(fill=tk.X, **pad)
        self.btn_launch = tk.Button(btns, text="LAUNCH (hook no processo)",
                                    command=self.launch, bg="#1d3a55", fg=DARK_FG,
                                    activebackground="#27506f", relief=tk.FLAT, padx=10)
        self.btn_launch.pack(side=tk.LEFT, **pad)
        tk.Button(btns, text="ATTACH ao log mais recente", command=self.attach_latest,
                  bg="#26304a", fg=DARK_FG, activebackground="#33405e",
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, **pad)
        tk.Button(btns, text="ATTACH a outro log...", command=self.attach_pick,
                  bg="#26304a", fg=DARK_FG, activebackground="#33405e",
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, **pad)
        self.btn_stop = tk.Button(btns, text="STOP", command=self.stop, bg="#4a2626",
                                  fg=DARK_FG, activebackground="#633434",
                                  relief=tk.FLAT, padx=10, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, **pad)
        self.btn_compare = tk.Button(btns, text="COMPARE ultimos 2", command=self.compare,
                                     bg="#26452e", fg=DARK_FG, activebackground="#33593c",
                                     relief=tk.FLAT, padx=10)
        self.btn_compare.pack(side=tk.LEFT, **pad)
        tk.Label(btns, textvariable=self.var_status, bg=DARK_BG, fg=ACCENT).pack(side=tk.LEFT, **pad)

        # numeros ao vivo
        live = tk.Frame(self.root, bg=DARK_BG)
        live.pack(fill=tk.X, **pad)
        self.live_labels = {}
        for key, label in [("fps", "fps"), ("ms", "ms/frame"), ("top", "gargalo"),
                           ("queue", "fila de texturas"), ("aux", "auxiliares"),
                           ("mem", "memoria do jogo")]:
            cell = tk.Frame(live, bg=DARK_BG)
            cell.pack(side=tk.LEFT, expand=True, fill=tk.X)
            tk.Label(cell, text=label, bg=DARK_BG, fg="#7a7f8a",
                     font=("Consolas", 9)).pack()
            var = {"fps": self.var_fps, "ms": self.var_ms, "top": self.var_top,
                   "queue": self.var_queue, "aux": self.var_aux,
                   "mem": self.var_mem}[key]
            tk.Label(cell, textvariable=var, bg=DARK_BG, fg=DARK_FG,
                     font=("Consolas", 13, "bold")).pack()
        self.var_queue, self.var_aux = self.var_queue, self.var_aux

        # grafico
        self.canvas = tk.Canvas(self.root, height=240, bg="#0d0f14", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True, **pad)
        self.canvas.bind("<Configure>", lambda e: self.redraw())

        # legenda
        leg = tk.Frame(self.root, bg=DARK_BG)
        leg.pack(fill=tk.X, **pad)
        for color, name in [("#e05a4e", "GPU"), ("#e0a34e", "copy-back"),
                            ("#b04ec9", "textures"), ("#4ea3e0", "copies"),
                            ("#58b46a", "prep resto")]:
            tk.Label(leg, text="■", fg=color, bg=DARK_BG).pack(side=tk.LEFT)
            tk.Label(leg, text=name + "   ", fg=DARK_FG, bg=DARK_BG).pack(side=tk.LEFT)

        # texto (log bruto filtrado + compare)
        self.text = tk.Text(self.root, height=10, bg="#0d0f14", fg="#9aa0aa",
                            insertbackground=DARK_FG, relief=tk.FLAT,
                            font=("Consolas", 9))
        self.text.pack(fill=tk.BOTH, expand=True, **pad)

    # ------------------------------------------------------------ acoes
    def current_switches(self):
        return {k: v.get() for k, v in self.sw_vars.items() if v.get()}

    def launch(self):
        exe = self.var_exe.get().strip()
        game = self.var_game.get().strip()
        if not (os.path.exists(exe) and os.path.exists(game)):
            self.var_status.set("caminhos invalidos")
            return
        self.stop()
        self.stop_event = threading.Event()
        self.frames.clear()
        self.aux.clear()
        self.pending = None
        self.pending_aux = {}
        self.started_at = time.time()
        sw = self.current_switches()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = os.path.join(LOG_DIR, f"run_gui_{stamp}.log")
        os.makedirs(LOG_DIR, exist_ok=True)
        self.meta = {"quando": stamp, "fonte": "launch", "switches": sw, "log": self.log_path}
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        self.source = ProcSource(exe, game, sw, self.q, self.stop_event, self.on_source_exit)
        self.source.start()
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_launch.config(state=tk.DISABLED)
        self.var_status.set(f"hook no processo (switches: {len(sw)})")

    def attach_latest(self):
        logs = sorted(p for p in os.listdir(LOG_DIR) if p.endswith(".log")) if os.path.isdir(LOG_DIR) else []
        if not logs:
            self.var_status.set("nenhum log em " + LOG_DIR)
            return
        self.attach(os.path.join(LOG_DIR, logs[-1]))

    def attach_pick(self):
        p = filedialog.askopenfilename(initialdir=LOG_DIR, filetypes=[("logs", "*.log")])
        if p:
            self.attach(p)

    def attach(self, path):
        self.stop()
        self.stop_event = threading.Event()
        self.frames.clear()
        self.aux.clear()
        self.pending = None
        self.pending_aux = {}
        self.started_at = time.time()
        self.log_path = path
        self.meta = {"quando": datetime.now().isoformat(timespec="seconds"),
                     "fonte": os.path.basename(path), "switches": {}, "log": path}
        self.log_file = None
        self.source = LogSource(path, self.q, self.stop_event)
        self.source.start()
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_launch.config(state=tk.DISABLED)
        self.var_status.set("attach: " + os.path.basename(path))

    def stop(self):
        if self.source:
            self.source.stop()
            self.source = None
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_launch.config(state=tk.NORMAL)
        self.var_status.set("parado")
        if getattr(self, "log_file", None):
            try:
                self.log_file.close()
            except OSError:
                pass
            self.log_file = None

    def on_source_exit(self):
        self.root.after(0, lambda: (self.btn_stop.config(state=tk.DISABLED),
                                    self.btn_launch.config(state=tk.NORMAL),
                                    self.finish_run()))

    def finish_run(self):
        if not self.frames:
            return
        os.makedirs(SUMMARY_DIR, exist_ok=True)
        s = summarize(self.frames, self.meta)
        p = os.path.join(SUMMARY_DIR, f"summary_{self.meta.get('quando', 'run').replace(':', '')}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"resumo": s, "frames": self.frames[-500:]}, f)
        self.text.insert(tk.END, f"\nresumo salvo: {p}\n")
        self.text.see(tk.END)

    def compare(self):
        sums = sorted(
            (os.path.getmtime(os.path.join(SUMMARY_DIR, p)), os.path.join(SUMMARY_DIR, p))
            for p in os.listdir(SUMMARY_DIR)
        ) if os.path.isdir(SUMMARY_DIR) else []
        if len(sums) < 2:
            self.text.insert(tk.END, "\ncompare: precisa de 2 resumos (rode 2 sessoes)\n")
            self.text.see(tk.END)
            return
        with open(sums[-2][1], encoding="utf-8") as f:
            a = json.load(f)["resumo"]
        with open(sums[-1][1], encoding="utf-8") as f:
            b = json.load(f)["resumo"]
        self.text.insert(tk.END, f"\n=== comparacao ===\nantes : {os.path.basename(sums[-2][1])}\ndepois: {os.path.basename(sums[-1][1])}\n" + compare_summaries(a, b) + "\n")
        self.text.see(tk.END)

    # ------------------------------------------------------------ live loop
    def poll(self):
        try:
            while True:
                line = self.q.get_nowait()
                self.on_line(line)
        except queue.Empty:
            pass
        self.watchdog()
        self.root.after(120, self.poll)

    def watchdog(self):
        """Alerta se nenhum burst de fps chega ha > STALL_S e mostra o que
        o log estava fazendo (contagem de tipos de linha na janela recente).
        Tambem acompanha commit de memoria do processo via WMI (sem psutil)."""
        now = time.time()
        if self.pending is not None:
            since = now - self.last_frame_wall
            if since > STALL_S and not self.stall_on:
                self.stall_on = True
                kinds = Counter(k for k, _ in self.recent_lines)
                top = ", ".join(f"{k} x{n}" for k, n in kinds.most_common(3)) or "nada"
                msg = (f"\n!!! STALL {since:.0f}s sem burst de fps "
                       f"(frame {self.frames[-1]['draws'] if self.frames else '?'} draws) "
                       f"| log fazendo: {top}\n")
                self.text.insert(tk.END, msg)
                self.text.see(tk.END)
                self.var_status.set(f"STALL {since:.0f}s!")
            if self.pid and now - self.last_mem_poll > 5.0:
                self.last_mem_poll = now
                mem = proc_commit_mb(self.pid)
                if mem:
                    self.var_mem.set(f"commit {mem[0]:.0f} MB (peak {mem[1]:.0f})")
        else:
            if self.stall_on:
                self.stall_on = False
                self.text.insert(tk.END, "!!! stall acabou - frames voltaram\n")
                self.text.see(tk.END)
                self.var_status.set("hook ativo")

    def on_line(self, line):
        if line.startswith("__PID__"):
            self.pid = int(line.split()[1])
            self.var_status.set(f"hook no processo pid={self.pid}")
            return
        if line == "__SAIU__":
            self.var_status.set("o jogo saiu")
            return
        if getattr(self, "log_file", None):
            try:
                self.log_file.write(line + "\n")
            except OSError:
                pass
        frame, is_fps = parse_line(line, self.aux)
        # diagnostico de stall: guarda o tipo da linha recente
        if "first bind pipeline" in line:
            kind = "first-bind"
        elif "shader translated" in line:
            kind = "translate"
        elif "host pipeline" in line:
            kind = "host-pipeline"
        elif is_fps:
            kind = "fps-burst"
        else:
            kind = "outro"
        self.recent_lines.append((kind, line[:80]))
        if is_fps:
            self.last_frame_wall = time.time()
            # fecha o frame anterior com o aux coletado na janela dele
            if self.pending:
                self.pending["aux"].update(self.pending_aux)
                self.frames.append(self.pending)
                if len(self.frames) > 2000:
                    self.frames = self.frames[-1500:]
            self.pending = frame
            self.pending_aux = {}
            self.var_fps.set(f"{frame['fps']:.0f}")
            self.var_ms.set(f"{frame['ms']:.0f}")
            self.var_top.set(verdict(frame))
            self.stall_on = False
        elif self.pending:
            self.pending_aux.update(self.aux)
        aux = self.pending["aux"] if self.pending else {}
        if aux:
            q = f"{int(aux.get('tex_uploaded', 0))} upadas / {int(aux.get('tex_waiting', 0))} na fila"
            self.var_queue.set(q)
            self.var_aux.set(
                f"busy {aux.get('card_busy', 0):.0f}% | hold {aux.get('hold_still', 0):.0f} ms | "
                f"refresh {aux.get('tex_refresh', 0):.0f} ms | drop {self.pending['drop']:.0f}"
            )
        if is_fps or any(p in line for p in (" ERROR ", "panic", "device removed", "ERRO ao iniciar")):
            self.text.insert(tk.END, line[:240] + "\n")
            self.text.see(tk.END)
            if self.text.index("end-1c").split(".")[0] > "3000":
                self.text.delete("1.0", "200.0")

    # ------------------------------------------------------------- grafico
    def redraw(self):
        c = self.canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w < 50 or not self.frames:
            return
        fs = self.frames[-240:]
        maxms = max(max(f["ms"] for f in fs), 200) * 1.15
        n = len(fs)
        bw = max(3, w / n - 1)
        x0 = 6
        for i, f in enumerate(fs):
            x = x0 + i * (bw + 1)
            scale = h / maxms
            # empilhado: resto do frame primeiro (fundo cinza)
            segs = [
                (f["ms"] - f["prep"] - f["copyb"], "#33363e"),   # resto/submit
                (f["prep"] - f["tex"] - f["copies"], "#58b46a"),  # prep resto
                (f["copies"], "#4ea3e0"),
                (f["tex"], "#b04ec9"),
                (f["copyb"], "#e0a34e"),
                (f["gpu"], "#e05a4e"),
            ]
            y = h
            for v, color in segs:
                if v <= 0:
                    continue
                hh = min(v * scale, y)
                y -= hh
                c.create_rectangle(x, y, x + bw, y + hh, fill=color, width=0)
            # linha de 100 ms e de 33 ms
        for ms_, color in [(100, "#3d5a3d"), (33.3, "#2a4a5a")]:
            y = h - ms_ * (h / maxms)
            c.create_line(x0, y, w, y, fill=color, dash=(2, 4))
            c.create_text(w - 30, y - 8, text=f"{ms_:.0f} ms", fill=color,
                          font=("Consolas", 8), anchor="e")
        c.create_text(10, 10, anchor="nw", fill="#7a7f8a",
                      text=f"ultimos {n} frames | escala ate {maxms:.0f} ms",
                      font=("Consolas", 9))

# -------------------------------------------------------------------- main

def check_mode():
    """--check: roda o parser nos logs existentes e imprime resumos."""
    logs = sorted(p for p in os.listdir(LOG_DIR) if p.endswith(".log")) if os.path.isdir(LOG_DIR) else []
    if not logs:
        print("nenhum log em", LOG_DIR)
        return
    sums = []
    for name in logs:
        path = os.path.join(LOG_DIR, name)
        frames = []
        aux = {}
        pending = None
        pending_aux = {}
        for raw in open(path, encoding="utf-8", errors="replace"):
            line = strip_ansi(raw)
            f, is_fps = parse_line(line, aux)
            if is_fps:
                if pending:
                    pending["aux"].update(pending_aux)
                    frames.append(pending)
                pending = f
                pending_aux = {}
            elif pending:
                pending_aux.update(aux)
        if pending:
            pending["aux"].update(pending_aux)
            frames.append(pending)
        sw = {}
        if "nowait" in name: sw["TEXTURE_NO_WAIT"] = "1"
        if "switches" in name: sw["RELAY/LATE_WAIT/..."] = "on"
        s = summarize(frames, {"quando": name, "fonte": name, "switches": sw})
        sums.append(s)
        print(f"\n== {name} ==")
        for k, v in s.items():
            if k != "lentos":
                print(f"  {k}: {v}")
        print("  lentos:", s["lentos"])
    if len(sums) >= 2:
        print("\n=== comparacao (penultimo -> ultimo) ===")
        print(compare_summaries(sums[-2], sums[-1]))

def main():
    if "--check" in sys.argv:
        check_mode()
        return
    root = tk.Tk()
    root.geometry("1100x760")
    MonitorGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
