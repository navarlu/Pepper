# Migration plan: wifi → ethernet for Pepper audio

> **Status:** PLAN ONLY — not implemented yet. Current working setup is wifi-only
> (Pepper at `10.0.0.149` on `icub_0047-wifi`, RPi on same wifi).
>
> **Why migrate:** measured ~84 Mbps via eth0 vs ~34 Mbps via wlan0, plus wired
> = lower jitter for audio streaming. We accepted wifi as a fallback that works
> "well enough", but wired is the durable answer.

## Where we are right now

| component | path | IP |
|---|---|---|
| RPi → internet | wlan0 (`icub_0047-wifi`) | 10.0.0.124 |
| RPi → Pepper | wlan0 (same SSID) | reaches 10.0.0.149 |
| RPi eth0 | **disabled** (`nmcli con down pepper-ethernet`) | - |
| Pepper | wifi (icub_0047-wifi) | 10.0.0.149 |
| Pepper ethernet | unused | - |
| docker `PEPPER_QI_URL` | `tcp://10.0.0.149:9559` (set via env on bridge+safe-startup) | - |

## Target end-state

| component | path | IP |
|---|---|---|
| RPi → internet | eth0 (lab DHCP, default route metric 100) | 192.168.210.78 |
| RPi → Pepper | eth0 (same lab subnet) | reaches 192.168.210.113 |
| RPi wlan0 | optional fallback (or off entirely) | 10.0.0.124 if kept |
| Pepper | **ethernet** to the same switch | 192.168.210.113 (old lab DHCP lease) |
| Pepper wifi | **disabled via connmanctl on her** | - |
| docker `PEPPER_QI_URL` | `tcp://192.168.210.113:9559` (compose default) | - |

## Why this is non-trivial

- **Pepper has BOTH wifi and ethernet active by default.** Her OS will prefer wifi if both are up, so just plugging in the cable doesn't switch her over.
- **In earlier sessions her ethernet sometimes silently didn't activate** (rx_packets stayed at 0 across an entire boot). Could be cable-seat issue or her OS treating wifi as exclusive primary.
- **Disabling her wifi is one-way unless you can reach her.** Once wifi is off and ethernet is the only path, if ethernet fails to come up you've locked yourself out. Need a verified rollback path.
- **The RPi's `pepper-ethernet` NM profile previously had `10.0.0.200/24` (manual)** which conflicted with wlan0's `10.0.0.124/24` — that conflict is what broke routing during the wifi work. The migration must NOT re-introduce that conflict.

## Plan — 5 phases, each independently verifiable

### Phase 0 — Pre-flight (no system changes)

Goal: confirm prerequisites before changing anything.

- [ ] Pepper's ethernet cable is plugged firmly into her body's RJ45 (under the back hatch — confirm it clicks).
- [ ] Other end goes to the switch she shared with RPi.
- [ ] You have the lab SSH password for Pepper (`nao` user, lab-specific password — see Pepper.pdf "Kuka+A" hint).
- [ ] You can power-cycle her physically (chest button hold ~8s for hard shutdown if needed — per Pepper.pdf).
- [ ] Tablet on her chest works as an alternative wifi-toggle path if SSH fails.

**Rollback plan if something goes wrong later:** keep RPi's wlan0 connected to `icub_0047-wifi` throughout the migration. Worst case we go back to today's working setup with a chest-button reboot of her.

### Phase 1 — Re-enable RPi eth0 with fixed addressing (RPi side only)

Goal: RPi gets back on lab ethernet without re-creating the 10.0.0.x conflict.

- [ ] **Edit the `pepper-ethernet` NM profile to drop the manual `10.0.0.200/24`** so eth0 uses ONLY lab DHCP. Keep the link-local 169.254/16 fallback so direct-cable to her still works as a last resort.
  ```bash
  sudo nmcli con modify pepper-ethernet ipv4.addresses "169.254.1.1/16"
  sudo nmcli con modify pepper-ethernet ipv4.method auto
  sudo nmcli con up pepper-ethernet
  ```
- [ ] Verify: eth0 has only `192.168.210.78` (or similar lab DHCP) and `169.254.1.1/16`. No `10.0.0.x` on eth0.
  ```bash
  ip -4 addr show eth0
  ip route get 192.168.210.1     # via eth0 ✓
  ip route get 10.0.0.149         # via wlan0 (unchanged from now)
  ```
- [ ] Verify internet still works via eth0 (metric 100 < wlan0 600 = eth0 wins by default).
  ```bash
  ip route | grep default
  curl -sS --interface eth0 -w "%{http_code}\n" -o /dev/null https://www.google.com
  ```

**Rollback:** `sudo nmcli con down pepper-ethernet`. wlan0 takes over.

### Phase 2 — Verify she comes up on her ethernet (in parallel, no commitment yet)

Goal: confirm her ethernet works BEFORE disabling her wifi (so we have a fallback).

- [ ] Power her on, run the catcher in **dual-target mode** — probe both her wifi IP AND lab IP simultaneously. The catcher's auto-discover sweeps eth0 subnet AND known IPs — so it'll race whichever wakes up first.
  ```bash
  uv run python robot/scripts/pepper_catch_fire.py
  # let it run; if she comes up on ethernet, it'll find 192.168.210.x
  # if she only comes up on wifi, it'll find 10.0.0.149
  ```
- [ ] Check what IPs appeared on eth0's ARP during her boot. If a `28:24:ff:*` MAC shows up at a 192.168.210.x address → her ethernet activated. If only the wifi side shows her, ethernet isn't activating.
  ```bash
  ip neigh show dev eth0 | grep -E "28:24:ff|00:13:95"
  ```
- [ ] **Decision point:** if her ethernet didn't activate spontaneously, proceed to Phase 3. If it DID activate, you can skip directly to Phase 4 (she has both interfaces up; once we update `PEPPER_QI_URL` to her ethernet IP, audio routes wired without disabling her wifi at all).

**Rollback:** none needed — Phase 2 is read-only observation.

### Phase 3 — Disable her wifi (one-way, only if Phase 2 didn't show her on ethernet)

Goal: force her to use ethernet as primary by removing wifi.

- [ ] First, make sure you can SSH into her at her current wifi IP `10.0.0.149` from RPi (RPi still has wlan0 up).
  ```bash
  ssh nao@10.0.0.149
  # password: <lab Kuka+A password>
  ```
- [ ] **Once SSH is confirmed, also confirm her ethernet has come up at the OS level**:
  ```bash
  # inside Pepper:
  ifconfig                   # look for eth0 with an IP
  sudo connmanctl services   # look for an active "Wired" entry
  ```
- [ ] **If wired is listed AND has an IP**: it's safe to disable wifi. If wired is missing → STOP, the cable isn't working at the OS level, debug there before continuing.
- [ ] Disable wifi durably:
  ```bash
  # inside Pepper:
  sudo connmanctl disable wifi
  sudo reboot
  ```
- [ ] After reboot (~90s), find her on the lab subnet:
  ```bash
  uv run python robot/scripts/pepper_catch_fire.py 192.168.210.113
  ```

**Rollback:** if she doesn't come back on ethernet:
- Hold chest button 8s → hard reboot
- If still no ethernet: use her tablet UI to re-enable wifi
- Worst case: physically remove her ethernet cable and let wifi re-associate

### Phase 4 — Update docker services for the new IP

Goal: bridge + safe-startup talk to her at the lab ethernet IP.

- [ ] Update env var:
  ```bash
  cd /home/lucas/Projects/FEL/Pepper
  PEPPER_QI_URL=tcp://192.168.210.113:9559 \
    docker compose -f docker/docker-compose.yml up -d --force-recreate safe-startup bridge
  ```
- [ ] (Note: this is the **default** in `docker-compose.yml`, so subsequent `up -d` without env override will also work.)
- [ ] Verify:
  ```bash
  docker compose -f docker/docker-compose.yml logs --tail 5 safe-startup bridge
  # expect: "Pepper already online" + "[pepper_audio] Client connected"
  ```

**Rollback:** override env back to `tcp://10.0.0.149:9559`, recreate bridge+safe-startup. (Only works if her wifi is still on, i.e. Phase 3 wasn't completed.)

### Phase 5 — Optional cleanup / test audio quality

Goal: lock in the win.

- [ ] Test audio: have a 5-minute conversation, listen for jitter / dropouts. Compare subjectively to the wifi setup.
- [ ] (Optional) Disable RPi wifi entirely if you want pure-eth simplicity:
  ```bash
  nmcli radio wifi off
  # to re-enable: nmcli radio wifi on
  ```
- [ ] Update `debug.md` with the new working topology so the next room-move is easier.

## Decision tree summary

```
Phase 0 → Phase 1 (RPi side) → Phase 2 (boot her, observe)
                                ↓
                         ethernet active?
                          ↙          ↘
                       yes            no
                         ↓             ↓
                      Phase 4      Phase 3 (SSH, disable wifi)
                                       ↓
                              ethernet still active?
                              ↙              ↘
                            yes               no
                             ↓                ↓
                          Phase 4         ROLLBACK (chest button + tablet)
```

## Open questions before starting

1. **Lab Pepper SSH password** — confirm we have the actual password (the Kuka+A hint).
2. **Does the switch-to-lab uplink still work?** Last we checked we got a `192.168.210.78` lease via eth0 — should still hold but worth re-verifying at start of Phase 1.
3. **Is the wifi the way she's been deployed for the lab's other purposes?** If other people use her over wifi, disabling it might break their setups. Worth confirming with whoever else accesses her.

## What we are explicitly NOT doing in this plan

- Not changing the docker-compose `PEPPER_QI_URL` default (it's already `192.168.210.113`).
- Not touching the catcher script — it auto-discovers either path.
- Not touching the production tool-calling / agent code (the previous session's work stands).
- Not changing anything on woska — voice-agent there talks to RPi via the SSH reverse tunnel which is interface-agnostic.
