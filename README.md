# Atech Tamagotchi

A voice-agent virtual pet on an Atech 10-port board. The pet lives on the
board: its food, joy and energy drain over time, and you care for it with two
buttons. Hold the talk button to talk to it. Your voice goes to Claude, which
answers in character, picks a face and can act on what you say. Tell it "here's
a cookie" and it eats.

## Hardware

| port | module | role |
|------|--------|------|
| 1+2 | microphone (ICS-43434) | ears |
| 3 | button | **talk**: tap = pet, hold = push-to-talk |
| 4+5 | speaker (MAX98357A) | voice and chirps |
| 6 | NeoPixel 3x3 | stat lights: columns = food / joy / energy |
| 7+8 | ST7735 160x80 screen | the pet |
| 9 | button | **care**: tap = feed a snack (or wake), hold 0.8 s = lights out / wake |

Ports 1-6 are on the front face of the board; 7-9 are on the back. The screen
and the care button share the back face, so treat that face as the pet's front.

## Set up

```bash
# 1. Firmware. Needs the atech SDK with the corrected 10-port pin map (see below).
atech validate firmware && atech upload firmware

# 2. Bridge (voice + memory), on the PC the board is plugged into
pip install -r bridge/requirements.txt
export OPENAI_API_KEY=...       # speech-to-text and text-to-speech
export ANTHROPIC_API_KEY=...    # the pet's brain
python bridge/tama_bridge.py run --name Pip
```

**SDK requirement:** the 10-port GPIO map in the released atech package was
wrong on several ports. Build this firmware with an editable install of
`python-sdk` that includes the `fix/10port-gpio-map` changes:
`pip install -e ../python-sdk`. Check you're using it with
`python -c "import atech; print(atech.__file__)"`. The patched microphone
(streaming) and screen (`displayRegion`) drivers are vendored in
`firmware/modules/`.

Without the bridge the pet still lives, eats, sleeps and shows its stats. It
just can't talk, and it forgets its stats when it loses power.

## Bring-up order

1. Flash it. The boot card says `hatching` with `this way up ^`. If it's upside
   down, set `SCREEN_ROT = 2` in `firmware/project.yaml`, or try it live with
   `{"action":"flip","value":"2"}`.
2. Without the PC: tap the talk button (happy face and a chirp), tap the care
   button (it eats), hold the care button (lights out).
3. `python bridge/tama_bridge.py demo` cycles every face and action.
4. `python bridge/tama_bridge.py say "hello, I am Pip"` tests TTS and the speaker.
5. `run --save-audio /tmp/u.wav`, hold the talk button and speak, then listen to
   `u.wav` to check the mic.

## How it behaves

- **Stats.** Food empties in about 2 hours, joy in about 1.5 hours, and energy
  in about 3 hours. Asleep, the pet drains at half speed and recovers energy in
  about 20 minutes. Below 20 it nags with a sad face and a chirp, at most once
  every 10 minutes per need. With `--chatty` it also says something.
- **Care.** Petting adds joy (five quick pets make it dizzy). Snacks add food,
  but it refuses when it's over 90. Talking to it adds a little joy. When energy
  drops below 8 it falls asleep on its own.
- **Brain.** Each turn sends Claude the transcript, the current stats, the time
  and any button care since the last turn. Claude returns
  `{say, emotion, action}` through structured output. Actions are
  `feed_snack`, `feed_meal`, `play`, `sleep` and `wake`. The last 12 exchanges
  are kept as memory. The default model is `claude-opus-5-5` at `--effort low`
  to keep replies quick. Server-side refusal fallback is enabled.
- **Saved state.** The pet is saved in `~/.config/atech_tamagotchi/state.json`.
  On reconnect, the time the board was unplugged is applied at the firmware's
  own rates. `tama_bridge.py status` shows the saved pet.

The serial protocol is documented at the top of `firmware/project.yaml`.

## Not yet proven on hardware

The firmware builds; nothing here has run on a board yet.

- The double-width pairs 1+2, 4+5 and 7+8 are hardware-verified with an
  ST7735. The pair pin assignment is a property of the board, so the mic and
  speaker get the same verified lines. The generated screen constructor is
  `ST7735_TFT(36, 1, 35, 2)`.
- Speaker pair 4+5 uses GPIO 43/44 (UART0). The ROM boot log toggles those pins,
  so expect a click at power-on. A known open issue says the netlist lists port
  4's pins as `(43, 44)` and the YAML as `(44, 43)`. The screen worked on 4+5,
  but the speaker hasn't been tried there.
- The LED grid's orientation is assumed to have row 0 at the top. If the stat
  columns fill from the top, the module is rotated.
- Audio over USB in both directions is the same scheme as the 14-port Claw'd
  bot, which is also unproven on hardware. If speech is choppy, raise `LEAD_S`
  in the bridge.
