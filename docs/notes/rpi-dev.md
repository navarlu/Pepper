# RPi vs Ubuntu (main) Development Differences

Quick reference for what differs between the `rpi` branch (RPi 5) and `origin/main` (Ubuntu laptop).

## qi Library

- **Ubuntu**: `qi==3.1.5` installed via pip (in `requirements.txt`)
- **RPi**: self-built from source at `/home/lucas/Projects/FEL/QI_test/libqi-python/build/build/linux-armv8-gcc-release`
- Must add the path to `sys.path` before `import qi` in any script that uses it
- Build source repo: `/home/lucas/Projects/FEL/QI_test/libqi-python/`
- Requires boost 1.83.0 shared libs from conan build: `/home/lucas/.conan2/p/b/boost00dddf9f5dc9e/p/lib`
- Scripts that import qi must either set `LD_LIBRARY_PATH` or re-exec with it set

## Bridge

- **Ubuntu (main)**: separate `bridge3.py` (Python 3) + original `bridge.py` (Python 2) both exist
- **RPi**: `bridge.py` ported to Python 3 in-place, `bridge3.py` removed
- Key py3 changes: `urllib.parse`, `b"".join(parts)` for audio payload, `python3` shebang
- **py3 fix**: `_write_json` must encode body to bytes (`json.dumps().encode("utf-8")`) — otherwise `/health` returns empty body

## Docker

| Area | Ubuntu (main) | RPi |
|---|---|---|
| Base image | `python:3.11-slim` | `python:3.12-slim` (must match qi build) |
| PortAudio | built from source (PulseAudio support) | `apt install libportaudio2` |
| Extra apt deps | `autoconf, automake, libtool, libpulse-dev` | `libglib2.0-0` (for qi) |
| Bridge container | runs `bridge3.py`, no special mounts | runs `bridge.py` with `network_mode: host`, mounts qi + boost + libqi native libs |
| Redis | healthcheck, `on-failure` restart | no healthcheck, `unless-stopped` |
| user-client | PulseAudio mounts, mic env vars | ALSA via `devices: /dev/snd`, `group_add: audio` |
| Build network | `network: host` | default |

### Bridge container qi mounts (RPi)

The bridge container needs three volume mounts for qi to work:
- `/home/lucas/Projects/FEL/QI_test/libqi-python/build/build/linux-armv8-gcc-release` → `/opt/qi` (Python qi package)
- `/home/lucas/.conan2/p/b/boost00dddf9f5dc9e/p/lib` → `/opt/boost-lib` (boost shared libs)
- `/home/lucas/Projects/FEL/QI_test/local_qi/lib` → `/opt/qi-native-lib` (libqi.so.4)

Container env: `PYTHONPATH=/opt/qi`, `LD_LIBRARY_PATH=/opt/boost-lib:/opt/qi-native-lib`

### Startup command

```bash
# Start everything (no profiles — every service runs by default):
docker compose -f docker/docker-compose.yml --env-file .env up -d

# Rebuild and recreate all services:
docker compose -f docker/docker-compose.yml --env-file .env up -d --force-recreate --build
```

The `.env` file is in the project root (not `docker/`), must be passed with `--env-file .env`.

## Session Manager

- **Ubuntu**: has agent dispatch failure detection (15s timeout, resets to idle if agent never joins room)
- **RPi**: that logic removed

## Pepper IP / Network

- **Ubuntu default**: `tcp://10.0.0.149:9559` (WiFi, static)
- **RPi default**: `tcp://192.168.210.113:9559` (ethernet, DHCP — may change)
- `safe_startup.py` has auto-discovery for when IP is unknown

## sounddevice / Audio

- Ubuntu uses PulseAudio backend (PortAudio built from source with PulseAudio support)
- RPi uses ALSA backend (system PortAudio package)
