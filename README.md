# YiYou (弈友) v2 — Your AI Go Coach

YiYou is a desktop AI Go (Baduk / Weiqi) coach for Windows. It reviews your games with
KataGo, explains every key move in plain language through a large language model, plays
out variations **in sync with the commentary** on the board, and helps you train with a
1,500+ problem tsumego library.

> Go, also known as Baduk or Weiqi, is a 4,000-year-old strategy board game.

## Highlights

- **KataGo-powered game review** — win-rate curve, blunder/question/good-move
  classification, top candidate moves, score lead, and a per-point **territory (moku)
  overlay** right on the board.
- **Synchronized commentary** — explanations are delivered in segments; the board plays
  the variation as each sentence is shown, like a live game lecture. Pause, resume, or
  jump to any segment.
- **Life & death explained properly** — corner/local positions are analyzed with local
  KataGo search only (no confusing whole-board AI lines). The coach states the result
  explicitly: *unconditional life / ko life / seki / unconditional kill / ko kill*, and
  tells you **whether you can tenuki** (play elsewhere) or must answer immediately.
- **Smart problem library** — 1,500+ classical problems (Guanzi Pu, Xuanxuan Qijing,
  Gokyo Shumyo, etc.) with instant judging, graded difficulty, per-problem goals
  (live / kill / capturing race / save the cutting stones …), and auto-generated deep
  explanations after you solve them.
- **Capture any position as a problem** — while reviewing a game, one click turns the
  current local position into a verified practice problem.
- **Growth chains (题链)** — the library grows from *sources* and *lineages*, not just a
  flat pile of problems: pick a joseki (attach-and-retreat, double wing, small avalanche,
  3-3 invasion …) and practice the life-and-death and capturing-race problems that grow
  out of it, step by step, so you can see **where each tsumego comes from and why it
  appears**. Chain steps replay from the previous step's answer, and one click starts a
  continuous practice run along the chain.
- **Growth profiles (成长视图)** — create one profile per player, archive reviewed games
  or import external SGF files into a per-player game library, and get a computed
  **skill portrait**: per-phase average loss, weakness ranking, estimated rank, and
  recent trend. The AI coach then writes a personalized improvement plan with themed
  homework that links straight into the problem library.
- **Instant life-and-death verdict** — hand the coach any local shape and get a
  second-scale read: who is alive or dead, whether it is a capturing race or a
  seki (by liberty analysis), whether there is a ko, and how much you lose by
  playing elsewhere.
- **Play against a human-like AI** — adjustable strength profiles (20 kyu to 1 dan)
  powered by KataGo's human-simulation model, with instant hints when you blunder.
- **AI voice commentary** — optional text-to-speech so you can listen instead of read.

## Screenshot

```
┌──────────────────────────────┬────────────────────────────────────────┐
│  Board (always visible)      │  ⑤ Commentary                          │
│  • territory numbers         │  ▸ 1. White should play at D4…        │
│  • win-rate curve below      │  ▸ 2. Black answers at D7, White C7…   │
│  • move strip / controls     │  ▶ Sync lecture   ⏸   ■   📚 Save     │
└──────────────────────────────┴────────────────────────────────────────┘
```

## Getting started

### Option A — prebuilt installer (recommended)

Download `YiYou-Setup.exe` from the
[latest release](https://github.com/nebulaeyes1456/GoCoachAI/releases/latest) and run it.
It ships the KataGo engine, 44 growth chains and 41 verified problems, and imports
them on first start — no setup beyond your own (optional) DeepSeek key.
It installs to `%LOCALAPPDATA%\Programs\弈友` with desktop shortcuts — no
administrator rights needed.

### Option B — run from source

Requirements: Python 3.10+ (Windows 10/11, or Linux x64).

Windows (PowerShell):

```powershell
git clone https://github.com/nebulaeyes1456/GoCoachAI.git
cd YOUR_REPO
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
# 1) fetch the KataGo engine + network (not in the repo: ~100 MB, MIT-licensed)
.\.venv\Scripts\python.exe scripts\download_katago.py
# 2) copy and fill in your API key (DeepSeek, or point it at a local Ollama)
copy data\config.example.yaml data\config.yaml
# 3) start the desktop app
.\.venv\Scripts\python.exe backend\desktop.py
# …or run the web UI only
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --app-dir . --port 8765
# then open http://127.0.0.1:8765
```

Linux (bash):

```bash
git clone https://github.com/nebulaeyes1456/GoCoachAI.git
cd YOUR_REPO
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
# 1) fetch the engine + network — the script auto-picks the Linux x64 build
./.venv/bin/python scripts/download_katago.py
# 2) config — default engine paths work as-is (the backend drops the .exe
#    suffix on non-Windows); fill in your own API key if you want AI commentary
cp data/config.example.yaml data/config.yaml
# 3) start the backend and open the web UI in your browser
./.venv/bin/python -m uvicorn backend.main:app --app-dir . --port 8765
# open http://127.0.0.1:8765
```

macOS: the KataGo project ships **no official macOS binaries** for recent
releases — build KataGo from source first, then follow the Linux steps.
The app itself is plain Python + a browser UI, so everything else works.

A fresh clone has **no engine**: `engine/` holds the MIT-licensed KataGo binaries and
networks but is too large for git, so run `scripts/download_katago.py` first (it also
fetches the model). A GPU (OpenCL) is auto-detected; CPU fallback works out of the box.

Without an LLM key the app still reviews, plays and drills — only the written/spoken
commentary needs DeepSeek (cloud) or Ollama (local).

**Before you redistribute a build:** `scripts/build_desktop.py` strips
`backend/config.yaml` and `data/config.yaml` from the bundle and scans the output for
key-like strings — never ship an installer built by hand from a tree that still has
your key in it.

## AI coach configuration

The coach uses an LLM to explain moves. Set `coach.provider` in `data/config.yaml`:

- `deepseek` — cloud API (low cost, ~¥0.004 per problem explanation)
- `ollama` — fully local, no API key needed

No LLM? Everything else (analysis, review, practice, play) still works.

## Tech stack

- **Backend** — Python 3 / FastAPI / uvicorn, SQLite
- **Engine** — [KataGo](https://github.com/lightvector/KataGo) v1.18 (OpenCL/Eigen)
- **Frontend** — Vue 3 (no build step) + [WGo.js](https://github.com/waltheri/wgo.js)
- **Desktop shell** — pywebview (single-file PyInstaller build)

## Repository layout

```
backend/     FastAPI services: review, coach (LLM), problems, play, engine,
             progress (player profiles, archives, skill portraits)
frontend/    Vue3 SPA (no build): board, curves, commentary player
engine/      KataGo executables + networks (see engine/README or LICENSE)
scripts/     importers, problem seeders, e2e Playwright checks
tests/       unittest suite (34+ cases)
docs/        architecture & contracts
```

## License

Source code: MIT. KataGo engine/networks: see `engine/`.
