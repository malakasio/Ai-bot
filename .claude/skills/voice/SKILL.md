Skill: Voice Pipeline

Purpose

Handle real-time bidirectional voice interaction with <500ms end-to-end latency.

Pipeline Architecture

Microphone → WebSocket → Deepgram Nova-3 STT → VAD check → Claude LLM → ElevenLabs Flash TTS → WebSocket → Speaker
Latency Budget

STT (Deepgram Nova-3): 150–300ms
LLM first token (Claude 3.5 Haiku): ~350ms
TTS first audio (ElevenLabs Flash v2.5): ~75ms
Network overhead: ~50ms
Total target: <500ms
Barge-in Protocol

Maintain high-priority VAD thread alongside TTS playback
On speech detected during TTS playback:
Wait 200ms to distinguish barge-in from backchanneling ("uh-huh", "yeah")
If speech continues: fire user.interrupt event
Immediately stop TTS audio stream
Clear audio buffer
Route new audio to STT
Known Failure Modes

Deepgram connection drop: reconnect with exponential backoff (1s, 2s, 4s, 8s)
ElevenLabs rate limit: fallback to pyttsx3 (local TTS, lower quality)
LLM timeout: return cached "I'm processing, one moment..." audio
Lessons Learned

(Updated automatically by self-improvement loop)
