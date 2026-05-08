
## Agent:


# Pepper
    
- eyes indicating search


## Thesis:

- Declaration misto specifications
- Explain RAG
    - Data preparation
        - Finding links
        - Sracping web


## Look at:


## Experiment:


## Repo:

- student-tools -> main

## Bugs:

autonomous life turning off
update reame
happy making too much sound
add listening on VAD






Bigger TTS and STT?


lookup_person
return all numbers or filter just one?





tools o first round
animation
add imporatnt dates to knowldage
add documents for query search (maybe publications of hoffmans lab??)

important dates
opening hours

add say to tool calls


# Experiment

capture tools results
eval script (latency etc)
reste display

Speach lagging mid sentence
- Potential improvement: prebuffer/jitter buffer in `robot/src/bridge.py` before `sendRemoteBufferToOutput` (line 1304).
  Bridge currently sends 50 ms batches the moment they arrive, so NAOqi's internal output queue stays shallow.
  When a single qi-RPC stalls (logs show 100-357 ms vs 50 ms budget), the queue drains and audio gaps mid-sentence.
  Fix idea: per-utterance, accumulate ~200 ms (3200 frames @ 16 kHz) in `stereo_queue` before the first send,
  then flush back-to-back so NAOqi starts playback with a cushion. Reset the `primed` flag on new TCP conn
  and on the existing `size == 0` flush control frame. Add `PEPPER_PREBUFFER_FRAMES` env in `robot/src/config.py`.
  Won't help sustained Wi-Fi degradation (>200 ms continuous) — for that, on-Pepper UDP receiver is the real fix.
  Investigate further before implementing.
idk tool issue
smarter TTS
turn off shouldee leds