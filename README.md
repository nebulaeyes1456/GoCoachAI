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
- **Growth profiles (成长视图)** — create one profile per player, archive reviewed games
  or import external SGF files into a per-player game library, and get a computed
  **skill portrait**: per-phase average loss, weakness ranking, estimated rank, and
  recent trend. The AI coach then writes a personalized improvement plan with themed
  homework that links straight into the problem library.
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
[Releases](https://github.com/nebulaeyes1456/GoCoachAI/releases) page and run it.
It installs to `%LOCALAPPDATA%\Programs\弈友` with desktop shortcuts — no
administrator rights needed.

### Option B — run from source

Requirements: Windows 10/11, Python 3.10+.

```powershell
git clone https://github.com/nebulaeyes1456/GoCoachAI.git
cd YOUR_REPO
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
# copy and fill in your API key (DeepSeek or a local Ollama)
copy data\config.example.yaml data\config.yaml
# start the desktop app
.\.venv\Scripts\python.exe backend\desktop.py
# …or run the web UI only
.\.venv\Scripts\python.exe -m uvicorn backend.main:app --app-dir . --port 8765
# then open http://127.0.0.1:8765
```

The KataGo engine and neural networks live in `engine/` (MIT-licensed, preconfigured).
A GPU (OpenCL) is auto-detected; CPU fallback works out of the box.

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
