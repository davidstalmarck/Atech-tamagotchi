#!/usr/bin/env python3
"""Tamagotchi bridge: the PC half of the Atech voice pet.

The pet's life (hunger, joy, energy) runs on the board. This process gives it a
voice and a memory:

1. **Voice in.** Hold the talk button and the board streams 16 kHz mic audio.
   On release it is transcribed (OpenAI speech-to-text).
2. **Brain.** The words, the pet's current stats and recent care events go to
   Claude, which answers in character with JSON: what to say, which face to
   make, and an optional action (feed, play, sleep...).
3. **Voice out.** The reply is synthesised (OpenAI TTS), resampled to 16 kHz and
   streamed to the speaker while the pet lip-syncs and shows a caption.
4. **Persistence.** Stats and the conversation are saved in
   ~/.config/atech_tamagotchi/state.json and restored (with the time that passed
   applied) whenever the board reconnects.

    python tama_bridge.py run                    # the normal mode
    python tama_bridge.py demo                   # cycle every face, no AI
    python tama_bridge.py say "hello, I am Pip"  # TTS smoke test
    python tama_bridge.py poke feed_meal         # send one action
    python tama_bridge.py status                 # print the saved pet
"""

from __future__ import annotations

import argparse
import array
import base64
import io
import json
import os
import re
import sys
import threading
import time
import wave
from pathlib import Path

from atech import Board

STATE_FILE = Path.home() / ".config" / "atech_tamagotchi" / "state.json"

MIC_RATE = 16000
CHUNK_SAMPLES = 512                    # 32 ms per spk_play_pcm line
LEAD_S = 0.15                          # how far ahead of real time we send
HISTORY_TURNS = 12                     # user+assistant pairs kept for context

# Same rates as the firmware (seconds per point), used to age the pet while the
# board was unplugged.
RATES_AWAKE = {"food": 72.0, "joy": 54.0, "energy": 108.0}
RATES_ASLEEP = {"food": 144.0, "joy": 108.0, "energy": -12.0}   # negative = recovers

EMOTIONS = ["neutral", "happy", "love", "sad", "surprised", "dizzy", "angry", "sleepy", "excited"]
ACTIONS = ["none", "feed_snack", "feed_meal", "play", "sleep", "wake"]

REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "say": {"type": "string", "description": "What the pet says out loud. One or two short sentences."},
        "emotion": {"type": "string", "enum": EMOTIONS},
        "action": {"type": "string", "enum": ACTIONS},
    },
    "required": ["say", "emotion", "action"],
    "additionalProperties": False,
}

PERSONA = """\
You are {name}, a tiny virtual pet living inside a small electronic gadget on \
your owner's desk. You have a round mint-green body, a little sprout on your \
head, a speaker for a voice and a microphone for ears. Your owner talks to you \
by holding a button.

How you talk:
- Everything you say is spoken aloud through a tiny speaker, so reply with one \
or two short sentences, at most about 25 words. Plain words only: no emoji, \
markdown, lists or stage directions.
- You are curious, playful and affectionate, a bit childlike, never mean. You \
remember what your owner told you earlier in the conversation.
- Your feelings follow your stats. When you're hungry, lonely or tired, it \
colours what you say. Mention it naturally sometimes, but don't nag every time.
- You can play simple spoken games (guessing, riddles, word games) if asked.

Your stats arrive with each message, from 0 to 100: food (100 = full), joy and \
energy. Below 20 is a real need.

Choosing fields:
- "emotion" is the face you make while talking.
- "action" makes something happen to your body. Choose one only when your \
owner actually does or offers it in this message: food or a treat means \
feed_snack, a real meal means feed_meal, playing a game or a toy means play, \
"good night" or "go to sleep" means sleep, and waking you up means wake. \
Otherwise use "none". Talking about food is not feeding you.
- If your food is above 90 you are too full to eat, so politely refuse with \
action "none".
"""


def clean(text: str, n: int = 120) -> str:
    """Make text safe for the firmware's tiny parser: ASCII, no quotes/backslashes."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    text = (text.replace("’", "'").replace("‘", "'")
                .replace("“", "'").replace("”", "'").replace("—", "-"))
    text = text.encode("ascii", "replace").decode().replace('"', "'").replace("\\", "/")
    return text if len(text) <= n else text[: n - 1] + "~"


def resample_24k_to_16k(pcm: bytes) -> bytes:
    """Linear 3:2 resample of s16le mono. Pure Python; fine for a few seconds."""
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder != "little":
        src.byteswap()
    out = array.array("h")
    n = len(src)
    pos = 0.0
    while pos < n - 1:
        i = int(pos)
        f = pos - i
        out.append(int(src[i] * (1 - f) + src[i + 1] * f))
        pos += 1.5
    if sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


# ---------------------------------------------------------------- saved pet
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            print(f"[tama] {STATE_FILE} is corrupt; starting a new pet", file=sys.stderr)
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_FILE)


def aged_stats(stats: dict, seconds: float) -> dict:
    """Apply the time the board was unplugged, at the firmware's own rates."""
    out = dict(stats)
    asleep = bool(stats.get("asleep"))
    rates = RATES_ASLEEP if asleep else RATES_AWAKE
    for key, per in rates.items():
        out[key] = max(0.0, min(100.0, float(stats.get(key, 80)) - seconds / per))
    if asleep and out["energy"] >= 100:
        out["asleep"] = 0
    out["age"] = int(stats.get("age", 0) + seconds / 60)
    return out


class Bridge:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.board: Board | None = None
        self.tx_lock = threading.Lock()
        self.cancel = threading.Event()
        self.busy = threading.Lock()                # one voice turn at a time
        self.audio = bytearray()
        self.capturing = False
        self.state = load_state()
        self.state.setdefault("name", args.name or "Pip")
        if args.name:
            self.state["name"] = args.name
        self.state.setdefault("history", [])
        self.recent: list[str] = []                 # care events since the last turn
        self._openai = None
        self._claude = None

    # ------------------------------------------------------------ board I/O
    def send(self, key: str, value: str = "") -> None:
        board = self.board
        if board is None:
            return
        try:
            with self.tx_lock:
                board.send(key, value)
        except Exception as exc:                    # unplugged mid-send
            print(f"[tama] send failed: {exc}", file=sys.stderr)
            self.board = None                       # connect_loop reconnects
            try:
                board.close()
            except Exception:
                pass

    def connect_loop(self) -> None:
        while True:
            if self.board is None:
                try:
                    self.board = Board.connect(port=self.args.port)
                    print("[tama] board connected")
                    time.sleep(1.5)                 # let the boot card finish
                    self.restore()
                    threading.Thread(target=self.event_loop, args=(self.board,), daemon=True).start()
                except Exception as exc:
                    print(f"[tama] waiting for board: {exc}")
            time.sleep(3.0)

    def restore(self) -> None:
        self.send("name", clean(self.state["name"], 12))
        self.send("volume", str(self.args.volume))
        stats = self.state.get("stats")
        if not stats:
            print(f"[tama] a new pet hatched: {self.state['name']}")
            return
        away = max(0.0, time.time() - self.state.get("saved_at", time.time()))
        stats = aged_stats(stats, away)
        print(f"[tama] restoring after {away / 60:.0f} min away: {fmt_stats(stats)}")
        self.send("restore", "{:.0f},{:.0f},{:.0f},{},{}".format(
            stats["food"], stats["joy"], stats["energy"], int(stats["age"]), int(stats.get("asleep", 0))))

    def event_loop(self, board: Board) -> None:
        for ev in board.events():
            if board is not self.board:
                return
            key, value = ev.key, ev.value
            if key == "mic_audio":
                if self.capturing:
                    try:
                        self.audio.extend(base64.b64decode(value))
                    except Exception:
                        pass
            elif key == "ptt":
                if value == "start":
                    self.cancel.set()               # barge-in: stop any reply in flight
                    self.audio = bytearray()
                    self.capturing = True
                elif value == "end" and self.capturing:
                    self.capturing = False
                    audio, self.audio = bytes(self.audio), bytearray()
                    threading.Thread(target=self.voice_turn, args=(audio,), daemon=True).start()
            elif key == "stats":
                self.on_stats(str(value))
            elif key == "care":
                print(f"[tama] care: {value}")
                self.recent.append(f"{time.strftime('%H:%M')} owner pressed a button: {value}")
                self.recent = self.recent[-6:]
            elif key == "need":
                print(f"[tama] needs: {value}")
                if self.args.chatty:
                    threading.Thread(target=self.spontaneous, args=(str(value),), daemon=True).start()
            elif key == "boot":
                print("[tama] board rebooted, restoring")
                time.sleep(1.5)
                self.restore()

    def on_stats(self, value: str) -> None:
        try:
            food, joy, energy, asleep, age = (int(x) for x in value.split(","))
        except ValueError:
            return
        self.state["stats"] = {"food": food, "joy": joy, "energy": energy, "asleep": asleep, "age": age}
        self.state["saved_at"] = time.time()
        save_state(self.state)

    # ------------------------------------------------------------ clients
    @property
    def openai(self):
        if self._openai is None:
            from openai import OpenAI
            self._openai = OpenAI()
        return self._openai

    @property
    def claude(self):
        if self._claude is None:
            import anthropic
            self._claude = anthropic.Anthropic()
        return self._claude

    # ------------------------------------------------------------ voice
    def voice_turn(self, audio: bytes) -> None:
        if not self.busy.acquire(blocking=False):
            return
        self.cancel.clear()
        try:
            seconds = len(audio) / 2 / MIC_RATE
            if seconds < 0.4:
                self.reset_face()
                return
            text = self.transcribe(audio).strip()
            print(f"[tama] heard ({seconds:.1f}s): {text!r}")
            if not text:
                self.send("emote", "surprised")
                self.send("say", "huh?")
                self.send("state", "idle")
                return
            if self.cancel.is_set():
                return
            self.respond(text)
        except Exception as exc:
            print(f"[tama] voice turn failed: {exc}", file=sys.stderr)
            self.send("emote", "dizzy")
            self.send("say", clean(f"oops: {exc}", 60))
            self.send("state", "idle")
        finally:
            self.busy.release()

    def spontaneous(self, need: str) -> None:
        """The pet speaks up on its own when a stat runs low (--chatty)."""
        if not self.busy.acquire(blocking=False):
            return
        self.cancel.clear()
        try:
            self.send("state", "thinking")
            self.respond(f"(Nobody said anything. You feel {need}. Say one short thing to get your owner's attention.)",
                         spoken_by_owner=False)
        except Exception as exc:
            print(f"[tama] spontaneous line failed: {exc}", file=sys.stderr)
            self.send("state", "idle")
        finally:
            self.busy.release()

    def respond(self, text: str, spoken_by_owner: bool = True) -> None:
        reply = self.ask_claude(text, spoken_by_owner)
        if self.cancel.is_set():
            return
        if reply is None:
            self.send("emote", "dizzy")
            self.send("say", "my head is fuzzy")
            self.send("state", "idle")
            return
        say, emotion, action = reply["say"], reply["emotion"], reply["action"]
        print(f"[tama] {self.state['name']} ({emotion}, {action}): {say}")

        # Synthesise while the action animation plays.
        pcm_box: dict = {}
        synth = threading.Thread(target=lambda: pcm_box.update(pcm=self.synthesize(say)), daemon=True)
        synth.start()
        if action != "none":
            self.send("do", action)
            time.sleep(1.6 if action.startswith("feed") or action == "play" else 0.4)
        synth.join()
        if self.cancel.is_set():
            return
        self.send("emote", emotion)
        self.speak_pcm(pcm_box.get("pcm", b""), caption=say)

    def transcribe(self, pcm: bytes) -> str:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(MIC_RATE)
            w.writeframes(pcm)
        buf.name = "speech.wav"
        buf.seek(0)
        if self.args.save_audio:
            Path(self.args.save_audio).write_bytes(buf.getvalue())
        return self.openai.audio.transcriptions.create(model=self.args.stt_model, file=buf).text

    def ask_claude(self, text: str, spoken_by_owner: bool) -> dict | None:
        import anthropic

        stats = self.state.get("stats") or {"food": 80, "joy": 80, "energy": 80, "asleep": 0, "age": 0}
        context = [f"[{time.strftime('%A %H:%M')}] your stats: {fmt_stats(stats)}"]
        if self.recent:
            context.append("since you last spoke: " + "; ".join(self.recent))
        heard = f"Your owner says: {text}" if spoken_by_owner else text
        user = "\n".join(context) + "\n\n" + heard

        history = self.state["history"][-2 * HISTORY_TURNS:]
        try:
            resp = self.claude.beta.messages.create(
                model=self.args.model,
                max_tokens=4000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=[{"type": "text", "text": PERSONA.format(name=self.state["name"]),
                         "cache_control": {"type": "ephemeral"}}],
                output_config={"effort": self.args.effort,
                               "format": {"type": "json_schema", "schema": REPLY_SCHEMA}},
                messages=history + [{"role": "user", "content": user}],
            )
        except anthropic.AuthenticationError:
            print("[tama] Claude auth failed: set ANTHROPIC_API_KEY", file=sys.stderr)
            return None
        except anthropic.RateLimitError:
            print("[tama] Claude rate limited", file=sys.stderr)
            return None
        except anthropic.APIStatusError as exc:
            print(f"[tama] Claude error {exc.status_code}: {exc.message}", file=sys.stderr)
            return None
        except anthropic.APIConnectionError:
            print("[tama] can't reach the Claude API", file=sys.stderr)
            return None

        if resp.stop_reason == "refusal":
            return {"say": "Hmm, let's talk about something else!", "emotion": "neutral", "action": "none"}
        raw = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            reply = json.loads(raw)
        except json.JSONDecodeError:
            print(f"[tama] unparseable reply: {raw!r}", file=sys.stderr)
            return None
        if reply.get("emotion") not in EMOTIONS:
            reply["emotion"] = "neutral"
        if reply.get("action") not in ACTIONS:
            reply["action"] = "none"
        reply["say"] = str(reply.get("say", ""))[: self.args.max_speech_chars]

        self.recent = []
        self.state["history"] = (history + [
            {"role": "user", "content": user},
            {"role": "assistant", "content": raw},
        ])[-2 * HISTORY_TURNS:]
        save_state(self.state)
        return reply

    def synthesize(self, text: str) -> bytes:
        resp = self.openai.audio.speech.create(
            model=self.args.tts_model,
            voice=self.args.voice,
            input=text,
            instructions=self.args.voice_style,
            response_format="pcm",                  # 24 kHz s16le mono
        )
        return resample_24k_to_16k(resp.content)

    def speak_pcm(self, pcm: bytes, caption: str = "") -> None:
        self.send("state", "talking")
        if caption:
            self.send("say", clean(caption))
        step = CHUNK_SAMPLES * 2
        start = time.monotonic()
        for i, off in enumerate(range(0, len(pcm), step)):
            if self.cancel.is_set() or self.board is None:
                self.send("spk_stop")
                return
            due = start + i * CHUNK_SAMPLES / MIC_RATE - LEAD_S
            delay = due - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self.send("spk_play_pcm", base64.b64encode(pcm[off: off + step]).decode())
        time.sleep(LEAD_S + 0.2)
        self.send("state", "idle")

    def reset_face(self) -> None:
        self.send("state", "idle")

    def run(self) -> None:
        if not os.environ.get("OPENAI_API_KEY"):
            print("[tama] OPENAI_API_KEY not set: the pet lives, but can't hear or speak", file=sys.stderr)
        self.connect_loop()


def fmt_stats(s: dict) -> str:
    return "food {:.0f}, joy {:.0f}, energy {:.0f}{}, age {}".format(
        s.get("food", 0), s.get("joy", 0), s.get("energy", 0),
        " (asleep)" if s.get("asleep") else "", fmt_age(int(s.get("age", 0))))


def fmt_age(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 1440:
        return f"{minutes // 60} h {minutes % 60} min"
    return f"{minutes // 1440} days {minutes // 60 % 24} h"


# ---------------------------------------------------------------- commands
def cmd_demo(args: argparse.Namespace) -> None:
    steps = [
        ("emote", "happy"), ("emote", "love"), ("emote", "sad"), ("emote", "surprised"),
        ("emote", "dizzy"), ("emote", "angry"), ("emote", "sleepy"), ("emote", "excited"),
        ("do", "feed_snack"), ("do", "play"), ("state", "thinking"), ("state", "talking"),
        ("state", "idle"), ("do", "sleep"), ("do", "wake"),
    ]
    with Board.connect(port=args.port) as board:
        time.sleep(1.5)
        board.send("say", "demo mode")
        for key, value in steps:
            print(f"-> {key} {value}")
            if key == "state" and value == "talking":
                board.send("say", "this is a long caption so you can watch it scroll by")
            board.send(key, value)
            time.sleep(args.hold)
        board.send("state", "idle")


def cmd_say(args: argparse.Namespace) -> None:
    bridge = Bridge(args)
    bridge.board = Board.connect(port=args.port)
    time.sleep(1.5)
    text = " ".join(args.text)
    bridge.send("emote", "happy")
    bridge.speak_pcm(bridge.synthesize(text), caption=text)


def cmd_poke(args: argparse.Namespace) -> None:
    with Board.connect(port=args.port) as board:
        time.sleep(1.5)
        board.send("do", args.action)
        time.sleep(0.5)


def cmd_status(args: argparse.Namespace) -> None:
    state = load_state()
    if not state.get("stats"):
        print("no saved pet yet")
        return
    away = time.time() - state.get("saved_at", time.time())
    print(f"{state.get('name', 'Pip')}: saved {away / 60:.0f} min ago")
    print("  saved:", fmt_stats(state["stats"]))
    print("  now:  ", fmt_stats(aged_stats(state["stats"], away)))
    print(f"  {len(state.get('history', [])) // 2} remembered exchanges")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: auto-detect)")
    ap.add_argument("--name", help="the pet's name (saved; default Pip)")
    ap.add_argument("--volume", type=int, default=50, help="speaker volume 0-100")
    ap.add_argument("--tts-model", default="gpt-4o-mini-tts")
    ap.add_argument("--voice", default="coral")
    ap.add_argument("--voice-style", default=(
        "Speak like a tiny, cheerful cartoon creature: bright, quick and playful, "
        "a little squeaky, with lots of warmth."))
    ap.add_argument("--max-speech-chars", type=int, default=300)
    sub = ap.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="run the bridge")
    run.add_argument("--model", default="claude-opus-5-5")
    run.add_argument("--effort", default="low", choices=["low", "medium", "high"],
                     help="Claude effort; low keeps voice replies snappy")
    run.add_argument("--stt-model", default="gpt-4o-mini-transcribe")
    run.add_argument("--chatty", action="store_true", help="let the pet speak up when it's hungry/lonely/tired")
    run.add_argument("--save-audio", help="also write each utterance to this .wav (debugging)")

    demo = sub.add_parser("demo", help="cycle every face and action, no AI")
    demo.add_argument("--hold", type=float, default=3.0)

    say = sub.add_parser("say", help="speak text through the pet")
    say.add_argument("text", nargs="+")

    poke = sub.add_parser("poke", help="send one action to the pet")
    poke.add_argument("action", choices=ACTIONS[1:])

    sub.add_parser("status", help="print the saved pet")

    args = ap.parse_args()
    {"run": lambda a: Bridge(a).run(), "demo": cmd_demo, "say": cmd_say,
     "poke": cmd_poke, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
