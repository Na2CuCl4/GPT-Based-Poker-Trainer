# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Development server** (Flask debug, localhost:5000):
```bash
python main.py
```

**Production server** (single worker required — game sessions live in process memory):
```bash
gunicorn --worker-class gthread -w 1 --threads 50 --bind 0.0.0.0:5000 --timeout 120 wsgi:app
```

There are no tests or linting configurations in this project.

## Architecture

GPT-powered Texas Hold'em trainer. The player interacts through a browser; the backend runs a game engine and makes GPT calls for AI opponents and coaching.

**Request flow:**
1. Player action → REST POST `/api/game/action`
2. `web/server.py` advances the engine, triggers GPT calls for AI opponents
3. New `GameState` is broadcast via WebSocket `state_update` event
4. On hand end, advisor analyzes via GPT → `hand_result` event

**Key layers:**

- `poker/` — Pure Python game engine. `game_engine.py` is the state machine (street progression, side pots, all-in, run-it-twice). `game_state.py` holds `GameState` and `PlayerState` data classes. `hand_evaluator.py` wraps the `treys` library for hand ranking.

- `ai/` — GPT integration. `opponent.py` sends per-style system prompts and parses structured `OpponentDecision` responses. `advisor.py` generates real-time hints and post-hand analysis. `gpt_client.py` wraps the OpenAI SDK with Pydantic-validated structured outputs. `schemas.py` defines all Pydantic models for structured GPT outputs.

- `web/` — Flask + Flask-SocketIO server (`server.py`). Per-client `GameEngine` instances are stored in a process-level dict keyed by Socket.IO client ID — this is why only `-w 1` is safe. The frontend is a single-page vanilla JS app (`static/js/app.js`, ~37KB) using Socket.IO.

## Configuration

`game_config.yaml` (or `config/game_config.yaml`, which is gitignored) controls everything:

```yaml
ai:
  model: "gpt-4o"
  base_url: "..."   # OpenAI-compatible endpoint
  api_key: "..."
  response_delay: 1 # seconds before AI acts

table:
  num_opponents: 3  # 2–5
  starting_chips: 1000
  max_chips: 2000   # auto-rebuy/cashout threshold

training:
  show_hints: true
  post_hand_analysis: true
  show_opponent_styles: false

features:
  run_it_twice: true
  four_color_deck: true
```

The `.secret_key` file holds the Flask session secret and is gitignored. It is auto-generated on first run if missing.

## AI Opponent Styles

Six styles defined in `ai/opponent.py`, each with a distinct system prompt: `tight_aggressive`, `loose_aggressive`, `tight_passive`, `loose_passive`, `balanced`, `random`. The `random` style re-assigns randomly each hand.

## Card Representation

`poker/card.py` uses rank strings (`2`–`9`, `T`, `J`, `Q`, `K`, `A`) and suit strings (`s`, `h`, `d`, `c`). Unicode suit symbols and four-color mode (spades=black, hearts=red, diamonds=blue, clubs=green) are handled in the frontend based on config.
