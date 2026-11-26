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
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace",
            env={**os.environ, "POWERSHELL_TELEMETRY_OPTOUT": "1",
                 "PYTHONIOENCODING": "utf-8"})
        if not r.stdout:
            return None
        d = json.loads(r.stdout)
        return (d["PagedMemorySize64"] / 1e6, d["PeakPagedMemorySize64"] / 1e6)
    except Exception:
        return None

# ---------------------------------------------------------------- constantes

APP_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "victory", "data", "logs")
SUMMARY_DIR = os.path.join(APP_DIR, "summaries")

# Sem caminhos fixos de maquina: os defaults saem do victory.toml ao lado do
# exe (game_dir) e do nome do exe dentro dele. Editaveis na GUI, com seletor.
def guess_paths():
    """Procura victory.exe e victory.toml em vizinhancas plausiveis."""
    exe = game = ""
    base = os.path.dirname(APP_DIR)  # pasta que contem a pasta do monitor
    for cand in [
        os.path.join(APP_DIR, "VIctory-7900XTX", "target", "diag", "victory.exe"),
        os.path.join(base, "VIctory-7900XTX", "target", "diag", "victory.exe"),
        os.path.join(APP_DIR, "victory-fork", "target", "release", "victory.exe"),
        os.path.join(base, "victory-fork", "target", "release", "victory.exe"),
    ]:
        if os.path.exists(cand):
            exe = cand
            break
    toml_dirs = [
        os.path.join(APP_DIR, "VIctory-7900XTX"),
        os.path.join(base, "VIctory-7900XTX"),
        os.path.join(APP_DIR, "victory-fork"),
        os.path.join(base, "victory-fork"),
    ]
    # dump padrao conhecido (D:/Leonida) se nenhum toml tiver game_dir
    fallback_game = "D:/Leonida/GTA6.Xbox.v1.0.2.1-BuildToRun/game_scarlett_master.exe"
    for d in toml_dirs:
        t = os.path.join(d, "victory.toml")
        if os.path.exists(t):
            m = re.search(r"game_dir\s*=\s*['\"](.+?)['\"]", open(t, encoding="utf-8").read())
            if m:
                gd = m.group(1)
                for name in ("game_scarlett_master.exe",):
                    p = os.path.join(gd, name)
                    if os.path.exists(p):
                        game = p
                        break
                if not game:
                    game = gd
            break
    if not game and os.path.exists(fallback_game):
        game = fallback_game
    return exe, game

# (valor default, grupo, tooltip curto)
SWITCH_CATALOG = [
    # ---- perf base (validados nas runs A-D)
    ("VICTORY_RELAY", "2", "perf", "relay thread lands GPU results beside the submit thread"),
    ("VICTORY_LATE_WAIT", "3", "perf", "submissions fly before the GPU wait lands"),
    ("VICTORY_WIDE_BACK", "1", "perf", "shadow snapshots + compare across threads"),
    ("VICTORY_CARD_BUFFERS_MIN_MB", "1", "perf", "smallest title buffer copied to card memory"),
    ("VICTORY_CARD_BUFFERS_MB", "1024", "perf", "max bytes of card-memory buffer copies"),
    ("VICTORY_TEXTURE_NO_WAIT", "1", "perf", "texture refresh via kernel, no remap lock"),
    ("VICTORY_PIPELINE_WAIT_MS", "500", "pipelines", "wait budget per frame for a pipeline being made (0 = never)"),
    ("VICTORY_PIPELINE_WAIT_SHARED", "1", "pipelines", "budget shared by all threads"),
    # ---- fps do fxceless (main novo: 6.0 -> 10.3 fps)
    ("VICTORY_JOIN_PARTS", "", "fps", "+6%: consecutive list parts become one submission (29 parts -> 9)"),
    ("VICTORY_BACK_OWN_VIEW", "", "fps", "+9%: shader writes return through the layer's own view (submit 93->81ms)"),
    ("VICTORY_BACK_OWN_CHECK", "", "fps", "count stores that miss the own-view path (0 in 1.36M)"),
    ("VICTORY_INDIRECT_GATHER", "", "fps", "indirect records clamped in one pass by offset table (10.3 fps)"),
    ("VICTORY_INDIRECT_WINDOW", "1", "fps", "indirect commands clamped per window (cuts clamp passes ~2x)"),
    ("VICTORY_INDIRECT_WINDOW_CHECK", "", "fps", "re-clamp every gathered record and compare on the card"),
    ("VICTORY_CARD_REHASH_RUNS", "1", "fps", "re-hash in runs of touching pieces (with GATHER: 104->97ms)"),
    ("VICTORY_COPY_CHANGED", "", "fps", "copy refresh sends only 256-byte chunks that differ"),
    ("VICTORY_COPY_PLAIN", "", "fps", "title-memory copies as rep movsb w/ kernel fallback (issue 1149: the one switch worth turning on)"),
    ("VICTORY_CARD_METER_PIPELINES", "", "fps", "card time table by pipeline"),
    ("VICTORY_FPS_COUNTER", "", "fps", "fps drawn in the top-left of every frame (0 = off)"),
    ("VICTORY_JOIN_LISTS", "", "fps", "join host lists that follow one another"),
    ("VICTORY_JOIN_MOST", "", "fps", "join most consecutive parts"),
    ("VICTORY_ASYNC_SUBMIT", "", "fps", "submissions sent on a submit thread of their own"),
    ("VICTORY_ASYNC_PRESENT", "", "fps", "presents on their own line (measured: no gain)"),
    ("VICTORY_QUEUE2", "", "fps", "second copy queue for fetches (3 = with landing MB)"),
    ("VICTORY_CARD_FETCH_INFLIGHT", "", "fps", "card fetches inside their flights"),
    ("VICTORY_CARD_FETCH_INFLIGHT_MB", "", "fps", "max bytes of in-flight card fetches"),
    # ---- memory / in-place
    ("VICTORY_INPLACE", "", "memory", "1 = windows of title memory as host heaps; 2 = buffers too"),
    ("VICTORY_INPLACE_MB", "", "memory", "max title memory in place at once"),
    ("VICTORY_LEVELS_ON_DEMAND", "", "memory", "levels.rpf mapped on demand (was: whole 72GB)"),
    ("VICTORY_CARD_ROOM_FOR_BUFFERS", "", "memory", "reserve card room for buffers"),
    ("VICTORY_KEEP_SNAPSHOTS", "", "memory", "keep submission snapshots (256MB default) instead of recopying 1050MB/frame"),
    ("VICTORY_KEEP_SNAPSHOTS_MB", "256", "memory", "max bytes of kept snapshots"),
    ("VICTORY_CARD_WINDOWS_MB", "", "memory", "card-side windows of title memory (bus reads per draw)"),
    # ---- diagnostics
    ("VICTORY_D3D12_DEBUG", "", "debug", "D3D12 debug layer (names the failing command)"),
    ("VICTORY_D3D12_GBV", "", "debug", "GPU-based validation"),
    ("VICTORY_D3D12_DRED", "", "debug", "Device Removed Extended Data"),
    ("VICTORY_COPY_MODE", "", "debug", "log where submission copies go and what limits them"),
    ("VICTORY_GPU_TIMING", "", "debug", "GPU timing of copies/compares"),
    ("VICTORY_TRAIL", "", "debug", "breadcrumb trail for crash reports"),
    ("VICTORY_FRAME_LOG", "", "debug", "per-frame log of what the frame is made of"),
    ("VICTORY_CARD_METER", "", "debug", "card busy meter (timestamp pair around every host list)"),
    ("VICTORY_DRIVER_WATCH", "", "debug", "watch the driver state (0 = off)"),
    ("VICTORY_PROFILE", "", "debug", "sampling profiler of the layer"),
    ("VICTORY_SKIP_PIPELINES", "", "debug", "hashes of pipelines to skip (compiler hangs)"),
]

DEFAULT_SWITCHES = {name: val for name, val, _g, _d in SWITCH_CATALOG if val}
SWITCH_GROUP = {name: g for name, val, g, _d in SWITCH_CATALOG}
SWITCH_DOC = {name: d for name, val, _g, d in SWITCH_CATALOG}

# Presets de experimento: nome -> {switch: valor} (switches fora do preset = OFF)
PRESETS = {
    "Run D (baseline)": {
        "VICTORY_RELAY": "2", "VICTORY_LATE_WAIT": "3", "VICTORY_WIDE_BACK": "1",
        "VICTORY_CARD_BUFFERS_MIN_MB": "1", "VICTORY_CARD_BUFFERS_MB": "1024",
        "VICTORY_TEXTURE_NO_WAIT": "1", "VICTORY_PIPELINE_WAIT_MS": "0",
    },
    "Run E (fxceless recipe)": {
        "VICTORY_RELAY": "2", "VICTORY_LATE_WAIT": "3", "VICTORY_WIDE_BACK": "1",
        "VICTORY_CARD_BUFFERS_MIN_MB": "1", "VICTORY_CARD_BUFFERS_MB": "1024",
        "VICTORY_TEXTURE_NO_WAIT": "1", "VICTORY_PIPELINE_WAIT_MS": "0",
        "VICTORY_JOIN_PARTS": "1", "VICTORY_BACK_OWN_VIEW": "1",
        "VICTORY_BACK_OWN_CHECK": "1", "VICTORY_INDIRECT_GATHER": "1",
        "VICTORY_INDIRECT_WINDOW_CHECK": "1", "VICTORY_CARD_REHASH_RUNS": "1",
        "VICTORY_COPY_CHANGED": "1",
    },
    "Run G (BKC - best known)": {
        "VICTORY_RELAY": "2", "VICTORY_LATE_WAIT": "3", "VICTORY_WIDE_BACK": "1",
        "VICTORY_CARD_BUFFERS_MIN_MB": "1", "VICTORY_CARD_BUFFERS_MB": "1024",
        "VICTORY_TEXTURE_NO_WAIT": "1", "VICTORY_PIPELINE_WAIT_MS": "0",
        "VICTORY_JOIN_PARTS": "1", "VICTORY_BACK_OWN_VIEW": "1",
        "VICTORY_BACK_OWN_CHECK": "1", "VICTORY_INDIRECT_GATHER": "1",
        "VICTORY_INDIRECT_WINDOW_CHECK": "1", "VICTORY_CARD_REHASH_RUNS": "1",
        "VICTORY_COPY_CHANGED": "1", "VICTORY_CARD_METER": "1",
    },
    "Run H (BKC + INDIRECT_WINDOW, Series X)": {
        "VICTORY_RELAY": "2", "VICTORY_LATE_WAIT": "3", "VICTORY_WIDE_BACK": "1",
        "VICTORY_CARD_BUFFERS_MIN_MB": "1", "VICTORY_CARD_BUFFERS_MB": "1024",
        "VICTORY_TEXTURE_NO_WAIT": "1", "VICTORY_PIPELINE_WAIT_MS": "0",
        "VICTORY_JOIN_PARTS": "1", "VICTORY_BACK_OWN_VIEW": "1",
        "VICTORY_BACK_OWN_CHECK": "1", "VICTORY_INDIRECT_GATHER": "1",
        "VICTORY_INDIRECT_WINDOW": "1", "VICTORY_INDIRECT_WINDOW_CHECK": "1",
        "VICTORY_CARD_REHASH_RUNS": "1", "VICTORY_COPY_CHANGED": "1",
        "VICTORY_CARD_METER": "1",
    },
    "Run I (FLAWED - crash 7min, do not run)": {
        "VICTORY_RELAY": "2", "VICTORY_LATE_WAIT": "3", "VICTORY_WIDE_BACK": "1",
        "VICTORY_CARD_BUFFERS_MIN_MB": "1", "VICTORY_CARD_BUFFERS_MB": "1024",
        "VICTORY_TEXTURE_NO_WAIT": "1", "VICTORY_PIPELINE_WAIT_MS": "0",
        "VICTORY_JOIN_PARTS": "1", "VICTORY_BACK_OWN_VIEW": "1",
        "VICTORY_BACK_OWN_CHECK": "1", "VICTORY_INDIRECT_GATHER": "1",
        "VICTORY_INDIRECT_WINDOW": "1", "VICTORY_INDIRECT_WINDOW_CHECK": "1",
        "VICTORY_CARD_REHASH_RUNS": "1", "VICTORY_COPY_CHANGED": "1",
        "VICTORY_COPY_PLAIN": "1", "VICTORY_CARD_METER": "1",
    },
    "Run J (validation: BKC, Series X, no COPY_PLAIN, +D3D12_DEBUG)": {
        "VICTORY_RELAY": "2", "VICTORY_LATE_WAIT": "3", "VICTORY_WIDE_BACK": "1",
        "VICTORY_CARD_BUFFERS_MIN_MB": "1", "VICTORY_CARD_BUFFERS_MB": "1024",
        "VICTORY_TEXTURE_NO_WAIT": "1", "VICTORY_PIPELINE_WAIT_MS": "0",
        "VICTORY_JOIN_PARTS": "1", "VICTORY_BACK_OWN_VIEW": "1",
        "VICTORY_BACK_OWN_CHECK": "1", "VICTORY_INDIRECT_GATHER": "1",
        "VICTORY_INDIRECT_WINDOW": "1", "VICTORY_INDIRECT_WINDOW_CHECK": "1",
        "VICTORY_CARD_REHASH_RUNS": "1", "VICTORY_COPY_CHANGED": "1",
        "VICTORY_CARD_METER": "1",
        "VICTORY_D3D12_DEBUG": "1", "VICTORY_D3D12_DRED": "1",
    },
    "Run K (J + COPY_PLAIN, diagnostics build - tests the I crash)": {
        "VICTORY_RELAY": "2", "VICTORY_LATE_WAIT": "3", "VICTORY_WIDE_BACK": "1",
        "VICTORY_CARD_BUFFERS_MIN_MB": "1", "VICTORY_CARD_BUFFERS_MB": "1536",
        "VICTORY_TEXTURE_NO_WAIT": "1", "VICTORY_PIPELINE_WAIT_MS": "0",
        "VICTORY_JOIN_PARTS": "1", "VICTORY_BACK_OWN_VIEW": "1",
        "VICTORY_BACK_OWN_CHECK": "1", "VICTORY_INDIRECT_GATHER": "1",
        "VICTORY_INDIRECT_WINDOW": "1", "VICTORY_INDIRECT_WINDOW_CHECK": "1",
        "VICTORY_CARD_REHASH_RUNS": "1", "VICTORY_COPY_CHANGED": "1",
        "VICTORY_COPY_PLAIN": "1", "VICTORY_CARD_METER": "1",
        "VICTORY_D3D12_DEBUG": "1", "VICTORY_D3D12_DRED": "1",
    },
    "Run A (7900XTX fork, preset K adaptado - 12 switches)": {
        "VICTORY_RELAY": "2", "VICTORY_LATE_WAIT": "3", "VICTORY_WIDE_BACK": "1",
        "VICTORY_CARD_BUFFERS_MIN_MB": "1", "VICTORY_CARD_BUFFERS_MB": "1536",
        "VICTORY_TEXTURE_NO_WAIT": "1", "VICTORY_PIPELINE_WAIT_MS": "0",
        "VICTORY_JOIN_PARTS": "1", "VICTORY_INDIRECT_WINDOW": "1",
        "VICTORY_CARD_REHASH_RUNS": "1",
        "VICTORY_D3D12_DEBUG": "1", "VICTORY_D3D12_DRED": "1",
    },
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
    r"\(([\d.]+) being made(?:; ([\d.]+) names, ([\d.]+) attempts creating now"
    r"(?:, ([\d.]+) creating now)?)?\) and ([\d.]+) indirect"
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
    "c_total":      (re.compile(r"creates: ([\d.]+) started"), 0),
    "c_overlap":    (re.compile(r"([\d.]+) overlapping"), 0),
    "c_done":       (re.compile(r"([\d.]+) done"), 0),
    "c_refused":    (re.compile(r"([\d.]+) stores refused"), 0),
    "title_card_ms":(re.compile(r"card time of the title's lists by what they bind, per frame over \d+ frames: ([\d.]+) ms"), 0),
    "title_lists":  (re.compile(r"ms in ([\d.]+) lists of ([\d.]+) kinds"), 0),
}

TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)\.(\d+)Z")

# ------------------------------------------------------------------- i18n

STRINGS = {
    "pt-BR": {
        "exe_label": "victory.exe:",
        "game_label": "exe do titulo:",
        "tab_switches": "Switches",
        "tab_measure": "Measurements",
        "switches_frame": " switches de ambiente ",
        "btn_launch": "LAUNCH (hook no processo)",
        "btn_attach_latest": "ATTACH ao log mais recente",
        "btn_attach_pick": "ATTACH a outro log...",
        "btn_stop": "STOP",
        "btn_compare": "COMPARE ultimos 2",
        "lbl_fps": "fps",
        "lbl_ms": "ms/frame",
        "lbl_top": "gargalo",
        "lbl_queue": "fila de texturas",
        "lbl_aux": "auxiliares",
        "lbl_mem": "memoria do jogo",
        "lbl_pipe": "criacao de pipelines",
        "legend_gpu": "submit wait (contorno)",
        "legend_copyb": "copy-back",
        "legend_tex": "textures",
        "legend_copies": "copies",
        "legend_prep": "prep resto",
        "st_parado": "parado",
        "st_hook": "hook no processo pid={pid}",
        "st_hook_sw": "hook no processo (switches: {n})",
        "st_saiu": "o jogo saiu",
        "st_ativo": "hook ativo",
        "st_stall": "STALL {s:.0f}s!",
        "st_bad_paths": "caminhos invalidos",
        "st_no_logs": "nenhum log em {d}",
        "st_attach": "attach: {f}",
        "dlg_exe": "victory.exe do emulador",
        "dlg_game": "exe do titulo",
        "dlg_log": "log de run do victory",
        "err_launch": "ERRO ao iniciar: {e}",
        "chart_caption": "ultimos {n} frames | escala ate {m:.0f} ms",
        "ref_100": "100 ms",
        "ref_33": "33 ms",
        "stall_msg": "\n!!! STALL {s:.0f}s sem burst de fps | log fazendo: {k}\n",
        "stall_over": "!!! stall acabou - frames voltaram\n",
        "summary_saved": "\nresumo salvo: {p}\n",
        "compare_need": "\ncompare: precisa de 2 resumos (rode 2 sessoes)\n",
        "compare_head": "\n=== comparacao ===\nantes : {a}\ndepois: {b}\n",
        "cmp_metric": "metrica", "cmp_before": "antes", "cmp_after": "depois", "cmp_delta": "delta",
        "v_ok": "ok",
        "v_gpu": "GPU (draws)",
        "v_copyb": "copy-back",
        "v_tex": "textures",
        "v_copies": "copies up",
        "v_drop": "drops x pipeline",
        "queue_fmt": "{u} upadas / {w} na fila",
    },
    "en-US": {
        "exe_label": "victory.exe:",
        "game_label": "title exe:",
        "tab_switches": "Switches",
        "tab_measure": "Measurements",
        "switches_frame": " environment switches ",
        "btn_launch": "LAUNCH (hook the process)",
        "btn_attach_latest": "ATTACH to latest log",
        "btn_attach_pick": "ATTACH to another log...",
        "btn_stop": "STOP",
        "btn_compare": "COMPARE last 2",
        "lbl_fps": "fps",
        "lbl_ms": "ms/frame",
        "lbl_top": "bottleneck",
        "lbl_queue": "texture queue",
        "lbl_aux": "auxiliary",
        "lbl_mem": "game memory",
        "lbl_pipe": "pipeline creation",
        "legend_gpu": "submit wait (outline)",
        "legend_copyb": "copy-back",
        "legend_tex": "textures",
        "legend_copies": "copies",
        "legend_prep": "rest of prep",
        "st_parado": "idle",
        "st_hook": "hooked into process pid={pid}",
        "st_hook_sw": "hooked (switches: {n})",
        "st_saiu": "the game exited",
        "st_ativo": "hook active",
        "st_stall": "STALL {s:.0f}s!",
        "st_bad_paths": "invalid paths",
        "st_no_logs": "no logs in {d}",
        "st_attach": "attach: {f}",
        "dlg_exe": "emulator victory.exe",
        "dlg_game": "title exe",
        "dlg_log": "victory run log",
        "err_launch": "failed to start: {e}",
        "chart_caption": "last {n} frames | scale up to {m:.0f} ms",
        "ref_100": "100 ms",
        "ref_33": "33 ms",
        "stall_msg": "\n!!! STALL {s:.0f}s without an fps burst | log busy with: {k}\n",
        "stall_over": "!!! stall over - frames are back\n",
        "summary_saved": "\nsummary saved: {p}\n",
        "compare_need": "\ncompare: needs 2 summaries (run 2 sessions)\n",
        "compare_head": "\n=== comparison ===\nbefore: {a}\nafter : {b}\n",
        "cmp_metric": "metric", "cmp_before": "before", "cmp_after": "after", "cmp_delta": "delta",
        "v_ok": "ok",
        "v_gpu": "GPU (draws)",
        "v_copyb": "copy-back",
        "v_tex": "textures",
        "v_copies": "copies up",
        "v_drop": "drops x pipeline",
        "queue_fmt": "{u} uploaded / {w} waiting",
    },
}

LANG_FILE = os.path.join(APP_DIR, ".victory_monitor_lang")

def load_lang():
    try:
        return open(LANG_FILE, encoding="utf-8").read().strip() or "pt-BR"
    except OSError:
        return "pt-BR"

def save_lang(lang):
    try:
        open(LANG_FILE, "w", encoding="utf-8").write(lang)
    except OSError:
        pass

T = load_lang()

def tr(key, **kw):
    s = STRINGS.get(T, STRINGS["pt-BR"]).get(key, key)
    return s.format(**kw) if kw else s

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
        g = [float(x) if x is not None else 0.0 for x in m.groups()]
        return {
            "t": line_ts(line),
            "fps": g[0], "ms": g[1],
            "prep": g[2], "copies": g[3], "copies_mb": g[4],
            "tex": g[5], "tex_mb": g[6],
            "gpu": g[7], "copyb": g[8], "copyb_mb": g[9],
            "draws": g[10], "drop": g[11], "making": g[12],
            "creating_names": g[13] or 0.0,
            "creating_attempts": g[14] or 0.0,
            "indirect": g[15],
            "aux": dict(aux),
        }, True
    return None, False

def verdict(frame):
    """Onde esta o topo do gargalo deste frame."""
    parts = {
        tr("v_gpu"): frame["gpu"],
        tr("v_copyb"): frame["copyb"],
        tr("v_tex"): frame["tex"],
        tr("v_copies"): frame["copies"],
        tr("v_drop"): frame["drop"] * 0.5,  # estimativa: cada drop ~0.5 ms de montagem
    }
    top = max(parts, key=parts.get)
    if frame["ms"] <= 100:
        return tr("v_ok")
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

    def __init__(self, exe, game_exe, switches, out_queue, stop_event, on_exit, cwd=None, console=None):
        super().__init__(daemon=True)
        self.switches = switches
        self.exe = exe
        self.game_exe = game_exe
        self.q = out_queue
        self.stop_event = stop_event
        self.on_exit = on_exit
        self.cwd = cwd
        self.console = console
        self.proc = None

    def run(self):
        env = dict(os.environ)
        env.update(self.switches)
        args = [self.exe, "run", self.game_exe]
        if self.console and "series-s" in self.console:
            args += ["--console", "series-s"]
        args.append("--execute")
        try:
            self.proc = subprocess.Popen(
                args,
                cwd=self.cwd, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
        except Exception as e:
            self.q.put(tr("err_launch", e=e) + "\n")
            self.on_exit()
            return
        self.q.put(f"__PID__ {self.proc.pid}\n")
        for line in self.proc.stdout:
            if self.stop_event.is_set():
                break
            self.q.put(strip_ansi(line))
        if self.proc.poll() is None and not self.stop_event.is_set():
            self.terminate_child()
        self.q.put("__SAIU__\n")
        self.on_exit()

    def terminate_child(self):
        """Para o jogo. O victory.exe pode rodar elevated (dados em D:, dump de
        titulo) e o terminate() de um pai nao-elevado falha com
        PermissionError [WinError 5]; cai para taskkill /F, e ignora se o
        processo ja saiu sozinho."""
        try:
            self.proc.terminate()
        except PermissionError:
            try:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/F"],
                               capture_output=True, timeout=10,
                               text=True, encoding="utf-8", errors="replace")
            except Exception:
                pass
        except OSError:
            pass  # processo ja morto
        try:
            self.proc.wait(timeout=10)
        except Exception:
            pass

    def stop(self):
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.terminate_child()

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
        "creating_names": med(frames, "creating_names"),
        "creating_attempts": med(frames, "creating_attempts"),
        "overlap_janela": aux_last.get("c_overlap", 0),
        "c_total_janela": aux_last.get("c_total", 0),
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
    out = [f"{tr('cmp_metric'):28s} {tr('cmp_before'):>12s} {tr('cmp_after'):>12s} {tr('cmp_delta'):>10s}"]
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
        root.title("Victory monitor")
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
        self.var_pipeline = tk.StringVar(value="-")
        self.build()
        self.root.after(100, self.poll)

    # ---------------------------------------------------------- interface
    def _build_top(self, top):
            guess_exe, guess_game = guess_paths()

            # seletor de idioma
            lang_cell = tk.Frame(top, bg=DARK_BG)
            lang_cell.grid(row=0, column=3, rowspan=2, padx=(10, 0), sticky="ne")
            tk.Label(lang_cell, text="idioma / language", bg=DARK_BG, fg="#7a7f8a",
                     font=("Consolas", 8)).pack(anchor="e")
            self.lang_var = tk.StringVar(value=T)
            langs = ["pt-BR", "en-US"]
            self.lang_menu = tk.OptionMenu(
                lang_cell, self.lang_var, *langs, command=self.on_lang_change)
            self.lang_menu.config(bg="#1c1f26", fg=DARK_FG, activebackground="#27506f",
                                  activeforeground=DARK_FG, relief=tk.FLAT,
                                  highlightthickness=0, font=("Consolas", 10))
            self.lang_menu["menu"].config(bg="#1c1f26", fg=DARK_FG,
                                          activebackground="#27506f", font=("Consolas", 10))
            self.lang_menu.pack(anchor="e")

            tk.Label(top, text=tr("exe_label"), bg=DARK_BG, fg=DARK_FG).grid(row=0, column=0, sticky="w")
            self.var_exe = tk.StringVar(value=guess_exe)
            tk.Entry(top, textvariable=self.var_exe, width=52, bg="#1c1f26", fg=DARK_FG,
                     insertbackground=DARK_FG, relief=tk.FLAT).grid(row=0, column=1, padx=4, sticky="we")
            self.btn_pick_exe = tk.Button(top, text="...", command=self.pick_exe, bg="#26304a", fg=DARK_FG,
                      relief=tk.FLAT, width=3)
            self.btn_pick_exe.grid(row=0, column=2, padx=(2, 10))

            tk.Label(top, text=tr("game_label"), bg=DARK_BG, fg=DARK_FG).grid(row=1, column=0, sticky="w")
            self.var_game = tk.StringVar(value=guess_game)
            tk.Entry(top, textvariable=self.var_game, width=52, bg="#1c1f26", fg=DARK_FG,
                     insertbackground=DARK_FG, relief=tk.FLAT).grid(row=1, column=1, padx=4, sticky="we")
            self.btn_pick_game = tk.Button(top, text="...", command=self.pick_game, bg="#26304a", fg=DARK_FG,
                      relief=tk.FLAT, width=3)
            self.btn_pick_game.grid(row=1, column=2, padx=(2, 10))
            top.columnconfigure(1, weight=1)

            # console profile (tooltip explica: o dump precisa ser do console escolhido)
            prof = tk.Frame(top, bg=DARK_BG)
            prof.grid(row=2, column=0, columnspan=3, sticky="w", pady=(2, 0))
            lbl = tk.Label(prof, text="console:", bg=DARK_BG, fg="#7a7f8a",
                     font=("Consolas", 9))
            lbl.pack(side=tk.LEFT)
            lbl.bind("<Enter>", lambda e: self.show_tip(e, "console profile",
                "MUST match your game dump: the title reads the console type "
                "(registry ConsoleMode 0x64=X, 0x60=S) and only loads the world "
                "if the dump's assets match. A Series X dump needs series-x; a "
                "Series S dump needs series-s (mismatches hang at the loading "
                "screen with 0 draws)."))
            lbl.bind("<Leave>", lambda e: self.hide_tip())
            self.prof_var = tk.StringVar(value="series-x (default)")
            pm = tk.OptionMenu(prof, self.prof_var, "series-x (default)", "series-s")
            pm.config(bg="#1c1f26", fg=DARK_FG, activebackground="#27506f",
                      activeforeground=DARK_FG, relief=tk.FLAT,
                      highlightthickness=0, font=("Consolas", 10))
            pm["menu"].config(bg="#1c1f26", fg=DARK_FG,
                              activebackground="#27506f", font=("Consolas", 10))
            pm.pack(side=tk.LEFT, padx=6)
            pm.bind("<Enter>", lambda e: self.show_tip(e, "console profile",
                "MUST match your game dump (Series X dump = series-x; Series S "
                "dump = series-s). Mismatch hangs at the loading screen: the "
                "title waits for console-S assets that a Series X dump does "
                "not contain."))
            pm.bind("<Leave>", lambda e: self.hide_tip())

    def on_lang_change(self, lang):
        """Troca o idioma: salva a escolha e reconstroi a janela."""
        global T
        T = lang
        save_lang(lang)
        # destruir e reconstruir tudo dentro da mesma root
        for w in self.root.winfo_children():
            w.destroy()
        self.build()
        self.var_status.set(tr("st_parado"))

    def build(self):
        pad = dict(padx=6, pady=4)


        # notebook com 2 abas: Switches | Measurements
        holder = tk.Frame(self.root, bg=DARK_BG)
        holder.pack(fill=tk.BOTH, expand=True)

        # campos de caminho (fixo acima do notebook)
        top = tk.Frame(self.root, bg=DARK_BG)
        top.pack(fill=tk.X, **pad)
        self._build_top(top)

        self.nb = ttk.Notebook(holder)
        style = ttk.Style()
        style.theme_use('default')
        style.configure('Dark.TNotebook', background=DARK_BG, borderwidth=0)
        style.configure('Dark.TNotebook.Tab', background="#1c1f26", fg=DARK_FG,
                        padding=[14, 6], font=("Consolas", 10))
        style.map('Dark.TNotebook.Tab', background=[('selected', '#2e3340')],
                  fg=[('selected', ACCENT)])
        style.configure('Dark.TFrame', background=DARK_BG)
        self.nb.configure(style='Dark.TNotebook')
        self.nb.pack(fill=tk.BOTH, expand=True)

        # ---- ABA 1: SWITCHES
        tab_sw = tk.Frame(self.nb, bg=DARK_BG)
        self.nb.add(tab_sw, text=tr("tab_switches"))
        sw_holder = tk.Frame(tab_sw, bg=DARK_BG)
        sw_holder.pack(fill=tk.BOTH, expand=True)
        self.sw_canvas = tk.Canvas(sw_holder, bg=DARK_BG, highlightthickness=0)
        sb = tk.Scrollbar(sw_holder, orient=tk.VERTICAL, command=self.sw_canvas.yview)
        self.sw_canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.sw_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.sw_inner = tk.Frame(self.sw_canvas, bg=DARK_BG)
        self.sw_canvas.create_window((0, 0), window=self.sw_inner, anchor="nw", tags="inner")
        self.sw_inner.bind("<Configure>", self._on_sw_inner_configure)
        self.sw_canvas.bind("<Configure>", self._on_sw_canvas_configure)
        self.sw_canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # switches
        self.swf = tk.LabelFrame(self.sw_inner, text=tr("switches_frame"), bg=DARK_BG, fg=DARK_FG)
        self.swf.pack(fill=tk.X, **pad)
        self.sw_vars = {}      # nome -> valor (StringVar do Entry)
        self.sw_on = {}        # nome -> BooleanVar do checkbox
        self.tip_window = None
        groups = {}
        for k, v, g, d in SWITCH_CATALOG:
            groups.setdefault(g, []).append((k, v, d))
        gi = 0
        for gname, items in groups.items():
            gf = tk.LabelFrame(self.swf, text=f" {gname} ", bg=DARK_BG, fg="#7a7f8a",
                               highlightbackground="#2e3340", highlightthickness=1)
            gf.grid(row=gi // 2, column=gi % 2, sticky="nw", padx=6, pady=4, ipadx=4, ipady=2)
            for i, (k, v, d) in enumerate(items):
                val_var = tk.StringVar(value=v)
                on_var = tk.BooleanVar(value=bool(v))
                self.sw_vars[k] = val_var
                self.sw_on[k] = on_var
                cell = tk.Frame(gf, bg=DARK_BG)
                cell.grid(row=i // 2, column=i % 2, sticky="w", padx=4, pady=1)
                cb = tk.Checkbutton(cell, text=k.replace("VICTORY_", ""), variable=on_var,
                           command=lambda kk=k: self._on_switch_toggle(kk),
                           bg=DARK_BG, fg=DARK_FG, selectcolor="#1c1f26",
                           activebackground=DARK_BG, activeforeground=DARK_FG
                           )
                cb.pack(side=tk.LEFT)
                cb.bind("<Enter>", lambda e, kk=k, dd=d: self.show_tip(e, kk, dd))
                cb.bind("<Leave>", lambda e: self.hide_tip())
                ent = tk.Entry(cell, textvariable=val_var, bg="#1c1f26", fg=DARK_FG,
                     insertbackground=DARK_FG, relief=tk.FLAT, width=6)
                ent.pack(side=tk.LEFT)
                ent.bind("<Enter>", lambda e, kk=k, dd=d: self.show_tip(e, kk, dd))
                ent.bind("<Leave>", lambda e: self.hide_tip())
            gi += 1

        # botoes (aba switches)
        btns = tk.Frame(self.sw_inner, bg=DARK_BG)
        btns.pack(fill=tk.X, **pad)
        self.btn_launch = tk.Button(btns, text=tr("btn_launch"),
                                    command=self.launch, bg="#1d3a55", fg=DARK_FG,
                                    activebackground="#27506f", relief=tk.FLAT, padx=10)
        self.btn_launch.pack(side=tk.LEFT, **pad)
        tk.Button(btns, text=tr("btn_attach_latest"), command=self.attach_latest,
                  bg="#26304a", fg=DARK_FG, activebackground="#33405e",
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, **pad)
        tk.Button(btns, text=tr("btn_attach_pick"), command=self.attach_pick,
                  bg="#26304a", fg=DARK_FG, activebackground="#33405e",
                  relief=tk.FLAT, padx=10).pack(side=tk.LEFT, **pad)
        self.btn_stop = tk.Button(btns, text=tr("btn_stop"), command=self.stop, bg="#4a2626",
                                  fg=DARK_FG, activebackground="#633434",
                                  relief=tk.FLAT, padx=10, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, **pad)
        tk.Label(btns, text="preset:", bg=DARK_BG, fg="#7a7f8a",
                 font=("Consolas", 9)).pack(side=tk.LEFT, padx=(10, 2))
        self.preset_var = tk.StringVar(value="(choose)")
        self.preset_menu = tk.OptionMenu(btns, self.preset_var, *sorted(PRESETS.keys()),
                                         command=self.apply_preset)
        self.preset_menu.config(bg="#1c1f26", fg=DARK_FG, activebackground="#27506f",
                                activeforeground=DARK_FG, relief=tk.FLAT,
                                highlightthickness=0, font=("Consolas", 10))
        self.preset_menu["menu"].config(bg="#1c1f26", fg=DARK_FG,
                                        activebackground="#27506f", font=("Consolas", 10))
        self.preset_menu.pack(side=tk.LEFT)
        self.btn_compare = tk.Button(btns, text=tr("btn_compare"), command=self.compare,
                                     bg="#26452e", fg=DARK_FG, activebackground="#33593c",
                                     relief=tk.FLAT, padx=10)
        self.btn_compare.pack(side=tk.LEFT, **pad)
        tk.Label(btns, textvariable=self.var_status, bg=DARK_BG, fg=ACCENT).pack(side=tk.LEFT, **pad)

        # guarda refs para a troca de idioma
        self.btn_attach_latest_w = btns.winfo_children()[1]
        self.btn_attach_pick_w = btns.winfo_children()[2]

        # ---- ABA 2: MEASUREMENTS
        tab_me = tk.Frame(self.nb, bg=DARK_BG)
        self.nb.add(tab_me, text=tr("tab_measure"))

        # numeros ao vivo
        live = tk.Frame(tab_me, bg=DARK_BG)
        live.pack(fill=tk.X, **pad)
        self.live_labels = {}
        self.live_label_widgets = {}
        first_cell = True
        for key, label in [("fps", tr("lbl_fps")), ("ms", tr("lbl_ms")), ("top", tr("lbl_top")),
                           ("queue", tr("lbl_queue")), ("aux", tr("lbl_aux")),
                           ("mem", tr("lbl_mem")), ("pipe", tr("lbl_pipe"))]:
            if not first_cell:
                tk.Frame(live, width=1, bg="#2e3340").pack(side=tk.LEFT, fill=tk.Y, padx=4)
            first_cell = False
            cell = tk.Frame(live, bg=DARK_BG)
            cell.pack(side=tk.LEFT, expand=True, fill=tk.X)
            lw = tk.Label(cell, text=label, bg=DARK_BG, fg="#7a7f8a",
                     font=("Consolas", 9))
            lw.pack()
            self.live_label_widgets[key] = lw
            var = {"fps": self.var_fps, "ms": self.var_ms, "top": self.var_top,
                   "queue": self.var_queue, "aux": self.var_aux,
                   "mem": self.var_mem, "pipe": self.var_pipeline}[key]
            tk.Label(cell, textvariable=var, bg=DARK_BG, fg=DARK_FG,
                     font=("Consolas", 13, "bold")).pack()
        self.var_queue, self.var_aux = self.var_queue, self.var_aux

        # grafico
        self.canvas = tk.Canvas(tab_me, height=240, bg="#0d0f14", highlightthickness=0)
        self.canvas.pack(fill=tk.X, **pad)
        self.canvas.bind("<Configure>", lambda e: self.redraw())

        # legenda
        leg = tk.Frame(tab_me, bg=DARK_BG)
        leg.pack(fill=tk.X, **pad)
        for color, name in [("#e05a4e", tr("legend_gpu")), ("#e0a34e", tr("legend_copyb")),
                            ("#b04ec9", tr("legend_tex")), ("#4ea3e0", tr("legend_copies")),
                            ("#58b46a", tr("legend_prep"))]:
            tk.Label(leg, text="■", fg=color, bg=DARK_BG).pack(side=tk.LEFT)
            tk.Label(leg, text=name + "   ", fg=DARK_FG, bg=DARK_BG).pack(side=tk.LEFT)

        # texto (log bruto filtrado + compare)
        self.text = tk.Text(self.sw_inner, height=10, bg="#0d0f14", fg="#9aa0aa",
                            insertbackground=DARK_FG, relief=tk.FLAT,
                            font=("Consolas", 9))
        self.text.pack(fill=tk.X, **pad)

    def _on_sw_inner_configure(self, event):
        self.sw_canvas.configure(scrollregion=self.sw_canvas.bbox("all"))
        self._fit_sw_height()

    def _on_sw_canvas_configure(self, event):
        self.sw_canvas.itemconfigure("inner", width=event.width)

    def _fit_sw_height(self):
        self.sw_canvas.update_idletasks()
        self.sw_canvas.configure(height=self.sw_inner.winfo_reqheight())

    def _on_mousewheel(self, event):
        self.sw_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _on_switch_toggle(self, name):
        """Checkbox desmarcado limpa o campo; marcado com campo vazio restaura o default."""
        var = self.sw_vars.get(name)
        on = self.sw_on.get(name)
        if var is None or on is None:
            return
        if not on.get():
            var.set("")
        elif var.get() == "":
            for k, v, _g, _d in SWITCH_CATALOG:
                if k == name and v:
                    var.set(v)
                    break

    def show_tip(self, event, name, doc):
        self.hide_tip()
        if not doc:
            return
        x = event.x_root + 12
        y = event.y_root + 12
        self.tip_window = tw = tk.Toplevel(self.root)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tf = tk.Frame(tw, bg="#2a2f3a", padx=6, pady=4)
        tf.pack()
        tk.Label(tf, text=name, bg="#2a2f3a", fg=ACCENT,
                 font=("Consolas", 9, "bold")).pack(anchor="w")
        tk.Label(tf, text=doc, bg="#2a2f3a", fg=DARK_FG, wraplength=420,
                 justify=tk.LEFT, font=("Consolas", 9)).pack(anchor="w")

    def hide_tip(self):
        if self.tip_window:
            self.tip_window.destroy()
            self.tip_window = None

    def apply_preset(self, name):
        """Aplica um preset: marca os switches do preset com seus valores,
        desmarca todos os outros. _console no preset seta o profile."""
        if name not in PRESETS:
            return
        wanted = PRESETS[name]
        if "_console" in wanted:
            self.prof_var.set(wanted["_console"])
        for k, var in self.sw_vars.items():
            if k in wanted:
                self.sw_on[k].set(True)
                var.set(wanted[k])
            else:
                self.sw_on[k].set(False)
                var.set("")
        n = len([k for k in wanted if not k.startswith("_")])
        self.var_status.set(f"preset: {name} ({n} switches)")

    # ------------------------------------------------------------ acoes
    def pick_exe(self):
        p = filedialog.askopenfilename(
            title=tr("dlg_exe"),
            filetypes=[("victory.exe", "victory*.exe"), ("exe", "*.exe"), ("todos", "*.*")])
        if p:
            self.var_exe.set(p)

    def pick_game(self):
        p = filedialog.askopenfilename(
            title=tr("dlg_game"),
            filetypes=[("exe", "*.exe"), ("todos", "*.*")])
        if p:
            self.var_game.set(p)

    def current_switches(self):
        return {k: self.sw_vars[k].get() for k, on in self.sw_on.items()
                if on.get() and self.sw_vars[k].get() != ""}

    def launch(self):
        exe = self.var_exe.get().strip()
        game = self.var_game.get().strip()
        if not (os.path.exists(exe) and os.path.exists(game)):
            self.var_status.set(tr("st_bad_paths"))
            return
        cwd = os.path.dirname(os.path.dirname(os.path.dirname(exe)))  # <repo>/target/release/exe -> <repo>
        if not os.path.exists(os.path.join(cwd, "Cargo.toml")):
            cwd = os.path.dirname(exe)
        self.stop()
        self.stop_event = threading.Event()
        self.frames.clear()
        self.aux.clear()
        self.pending = None
        self.pending_aux = {}
        self.recent_lines.clear()
        self.started_at = time.time()
        sw = self.current_switches()
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_path = os.path.join(LOG_DIR, f"run_gui_{stamp}.log")
        os.makedirs(LOG_DIR, exist_ok=True)
        self.meta = {"quando": stamp, "fonte": "launch", "switches": sw, "log": self.log_path}
        self.log_file = open(self.log_path, "w", encoding="utf-8")
        prof = self.prof_var.get()
        console = "series-s" if "series-s" in prof else None
        self.meta["console"] = console or "series-x"
        self.source = ProcSource(exe, game, sw, self.q, self.stop_event, self.on_source_exit, cwd, console)
        self.source.start()
        self.btn_stop.config(state=tk.NORMAL)
        self.btn_launch.config(state=tk.DISABLED)
        self.var_status.set(tr("st_hook_sw", n=len(sw)))

    def attach_latest(self):
        logs = sorted(p for p in os.listdir(LOG_DIR) if p.endswith(".log")) if os.path.isdir(LOG_DIR) else []
        if not logs:
            self.var_status.set(tr("st_no_logs", d=LOG_DIR))
            return
        self.attach(os.path.join(LOG_DIR, logs[-1]))

    def attach_pick(self):
        p = filedialog.askopenfilename(initialdir=LOG_DIR, title=tr("dlg_log"), filetypes=[("log", "*.log")])
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
        self.var_status.set(tr("st_attach", f=os.path.basename(path)))

    def stop(self):
        if self.source:
            self.source.stop()
            self.source = None
        self.btn_stop.config(state=tk.DISABLED)
        self.btn_launch.config(state=tk.NORMAL)
        self.var_status.set(tr("st_parado"))
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
        self.text.insert(tk.END, tr("summary_saved", p=p))
        self.text.see(tk.END)

    def compare(self):
        sums = sorted(
            (os.path.getmtime(os.path.join(SUMMARY_DIR, p)), os.path.join(SUMMARY_DIR, p))
            for p in os.listdir(SUMMARY_DIR)
        ) if os.path.isdir(SUMMARY_DIR) else []
        if len(sums) < 2:
            self.text.insert(tk.END, tr("compare_need"))
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
        self.redraw()
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
                msg = tr("stall_msg", s=since, k=top)
                self.text.insert(tk.END, msg)
                self.text.see(tk.END)
                self.var_status.set(tr("st_stall", s=since))
            if self.pid and now - self.last_mem_poll > 5.0:
                self.last_mem_poll = now
                mem = proc_commit_mb(self.pid)
                if mem:
                    self.var_mem.set(f"commit {mem[0]:.0f} MB (peak {mem[1]:.0f})")
        else:
            if self.stall_on:
                self.stall_on = False
                self.text.insert(tk.END, tr("stall_over"))
                self.text.see(tk.END)
                self.var_status.set(tr("st_ativo"))

    def on_line(self, line):
        if line.startswith("__PID__"):
            self.pid = int(line.split()[1])
            self.var_status.set(tr("st_hook", pid=self.pid))
            return
        if line == "__SAIU__":
            self.var_status.set(tr("st_saiu"))
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
            q = tr("queue_fmt", u=int(aux.get('tex_uploaded', 0)), w=int(aux.get('tex_waiting', 0)))
            self.var_queue.set(q)
            self.var_aux.set(
                f"busy {aux.get('card_busy', 0):.0f}% | hold {aux.get('hold_still', 0):.0f} ms | "
                f"refresh {aux.get('tex_refresh', 0):.0f} ms | drop {self.pending['drop']:.0f}"
            )
        if self.pending:
            names = self.pending.get("creating_names", 0)
            att = self.pending.get("creating_attempts", 0)
            tcm = aux.get("title_card_ms", 0)
            self.var_pipeline.set(
                f"{names:.0f}/{att:.0f} criando | card titulo {tcm:.0f} ms"
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
            # empilhado DENTRO do ms medido (issue #1: gpu/submit-wait e sobreposto,
            # nao um pedaco do frame - antes a barra virava ms + gpu).
            segs = [
                (f["copies"], "#4ea3e0"),                          # copies up
                (f["tex"], "#b04ec9"),                             # textures
                (f["copyb"], "#e0a34e"),                           # copy-back
                (f["prep"] - f["tex"] - f["copies"], "#58b46a"),   # prep resto (>=0)
                (f["ms"] - f["prep"] - f["copyb"], "#33363e"),     # resto/submit (>=0)
            ]
            y = h
            for v, color in segs:
                if v <= 0:
                    continue
                hh = min(v * scale, y)
                y -= hh
                c.create_rectangle(x, y, x + bw, y + hh, fill=color, width=0)
            # submit wait (gpu): marcador independente na altura dele - pode exceder ms
            gpu_h = min(f["gpu"] * scale, h)
            gy = h - gpu_h
            c.create_rectangle(x, gy, x + bw, gy + gpu_h, outline="#e05a4e", width=1)
            # linha de 100 ms e de 33 ms
        # grade vertical: uma linha a cada 30 frames
        grid_n = 30
        for gi in range(1, n // grid_n + 1):
            gx = x0 + gi * grid_n * (bw + 1)
            c.create_line(gx, 0, gx, h, fill="#23262e")
        for ms_, color in [(100, "#3d5a3d"), (33.3, "#2a4a5a")]:
            y = h - ms_ * (h / maxms)
            c.create_line(x0, y, w, y, fill=color, dash=(2, 4))
            c.create_text(w - 30, y - 8, text=f"{ms_:.0f} ms", fill=color,
                          font=("Consolas", 8), anchor="e")
        c.create_text(10, 10, anchor="nw", fill="#7a7f8a",
                      text=tr("chart_caption", n=n, m=maxms),
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
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    root.geometry("1280x860")
    root.state("zoomed")  # abre maximizada
    MonitorGUI(root)
    root.mainloop()

if __name__ == "__main__":
    main()
