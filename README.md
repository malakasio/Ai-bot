JARVIS v6.0 — Αυτόνομος Ψηφιακός Βοηθός

100% δωρεάν stack — τρέχει χωρίς API keys, χωρίς μηνιαίο κόστος.

"Αυτό δεν είναι demo. Είναι ο χάρτης για να χτίσεις κάτι πραγματικό." — Blueprint v3
Free Stack

Component	Free Option	Optional Paid Upgrade
LLM	Ollama (Llama 3.2, Mistral)	Anthropic Claude
STT	faster-whisper (local)	Deepgram Nova-3
TTS	edge-tts (Microsoft free)	ElevenLabs Flash v2.5
Embeddings	sentence-transformers (local)	OpenAI ada-002
Database	SQLite + sqlite-vec	—
Hosting	Oracle Cloud Free Tier	Hetzner CX32 (~€15/mo)
Κόστος: €0/μήνα (μόνο ρεύμα/bandwidth αν τρέχεις locally)

Quick Start

# 1. Clone
git clone https://github.com/your/jarvis
cd jarvis

# 2. Setup (Ubuntu 22.04)
chmod +x scripts/setup.sh
sudo ./scripts/setup.sh --with-ollama

# 3. Configure
cp .env.example .env
nano .env  # Minimum: TELEGRAM_BOT_TOKEN + TELEGRAM_USER_ID

# 4. Start
./scripts/start_dev.sh
Architecture

┌─────────────────────────────────────────────────────┐
│                    JARVIS v6.0                      │
│                                                     │
│  Voice Pipeline              Agent System           │
│  ┌──────────────┐           ┌──────────────────┐   │
│  │ STT          │           │ Coordinator       │   │
│  │ (Whisper/DG) │           │ Sub-agents        │   │
│  │ VAD Process  │    LLM    │ Agent Teams       │   │
│  │ TTS          │◄─────────►│ Tool Registry     │   │
│  │ (edge/EL)    │  (Ollama  │                   │   │
│  └──────────────┘  /Claude) │ Memory System     │   │
│                             │ (SQLite+vectors)  │   │
│  KAIROS Daemon              └──────────────────┘   │
│  ┌──────────────┐                                   │
│  │ Poll every 5m│           Security Zones          │
│  │ autoDream    │           ┌──────────────────┐   │
│  │ GitHub watch │           │ Green/Yellow/Red  │   │
│  │ Notifications│           │ Sandbox (Docker)  │   │
│  └──────────────┘           │ Audit log         │   │
│                             └──────────────────┘   │
│  API / Telegram Bot                                 │
│  FastAPI + WebSocket + PWA Dashboard               │
└─────────────────────────────────────────────────────┘
Free Deployment Options

Option 1: Oracle Cloud Always Free (Best)

4 OCPUs ARM, 24GB RAM — plenty for all models
200GB storage, unlimited bandwidth
Instructions: docs/oracle-cloud-setup.md
Option 2: Your own machine (Raspberry Pi 5 or any Linux)

sudo ./scripts/setup.sh --with-ollama
Option 3: Test on Google Colab

Open notebooks/colab_setup.ipynb — sessions expire but great for testing.

Milestones (Pass/Fail Criteria)

Milestone 1: Voice ✓

Voice question → answer in <900ms (programmatic measurement)
Barge-in: stops and listens in <300ms
5 consecutive questions without crash or memory leak
Milestone 2: Memory ✓

Start conversation, say something important, close
24h later: remembers correctly
Memory retrieval < 200ms for top-5 results
Memory never exceeds 30% of context window
Milestone 3: Autonomous Action ✓

Agent creates a file autonomously without breaking anything
Audit log records every action with timestamp
Wrong action → rollback in <30 seconds
Milestone 4: Always-On ✓

System reboot → daemon returns automatically
Telegram /status works from anywhere
KAIROS runs every 5min without drift
Configuration

All configuration via environment variables (.env):

# Minimum required (no API keys needed):
JARVIS_HOME=/home/jarvis
USER_TIMEZONE=Europe/Athens

# Telegram (free, required for mobile control):
TELEGRAM_BOT_TOKEN=xxx
TELEGRAM_USER_ID=123456789

# For lab mode (pentesting/network tools in isolated environment):
JARVIS_LAB_MODE=true   # Only in isolated networks you own

# Optional paid upgrades:
ANTHROPIC_API_KEY=sk-ant-...   # Claude API
DEEPGRAM_API_KEY=...           # Faster STT
ELEVENLABS_API_KEY=...         # Higher quality TTS
Security Zones

Zone	Paths	Access
Green	~/jarvis/workspace/	Full read/write
Yellow	~/*	Read OK, write needs confirmation
Orange	~/Documents/	Confirmation required
Red	/etc, /var, /sys	Blocked (needs JARVIS_ZONE=red)
Black	/proc, /sys/kernel	Never
Lab Mode (Experimental, Educational Only)

For network security research in isolated, owned environments:

JARVIS_LAB_MODE=true
JARVIS_ZONE=red   # Only if you need /etc access too
Enables: nmap, tcpdump, HTTP requests to any domain, credential vault. Never use on networks you don't own.

150+ Bugs Fixed (v6)

See jarvis_blueprint_v6.pdf for the complete list. Key fixes:

Full agentic tool-use loop (not single-call)
max_tokens=8192 (not 2048)
Anthropic exact token counting (not tiktoken)
FIFO trim removes pairs not single messages
VAD in separate process (not asyncio — GIL issue)
Server streams audio bytes to client (no mpv/PulseAudio)
systemd LoadCredential (not Environment=)
Tenacity OUTSIDE semaphore (deadlock fix)
Circuit breaker bounded deque (memory leak fix)
Persistent session secret (no forced logout on restart)
...and 140+ more
Self-Improvement Loop

Task executes → Score (0-100) → If score < 70:
  → Agent proposes SKILL.md update
  → You review via /skill_proposals
  → Accept/reject via API or Telegram
  → SKILL.md updated (append-only, versioned)
Core code: NEVER auto-updated by agent. Only SKILL.md files.

License

MIT — build freely, learn openly.
