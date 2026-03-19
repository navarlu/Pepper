# Startup Flow

## Current recommended flow

### 1. Start Docker stack
```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d livekit redis weaviate voice-agent session-manager listener
```

### 2. Bring up Pepper Ethernet
```bash
nmcli connection up pepper-ethernet
ip route get 10.0.0.149
nc -vz -s 10.0.0.200 10.0.0.149 9559
```

### 3. Stabilize Pepper (Python 2.7)
```bash
pyenv shell naoqi27
unset LD_LIBRARY_PATH
export NAOQI_ROOT="$HOME/Projects/FEL/Pepper/robot/choregraphe"
export PYTHONPATH="$NAOQI_ROOT/lib/python2.7/site-packages"
python2 robot/utils/safe_startup.py
```

### 4. Start bridge
```bash
python2 robot/src/bridge.py
```

### 5. Start user microphone client
```bash
uv run python robot/src/user_client.py
```

### 6. Open operator dashboard
```
http://127.0.0.1:8787/
```

---

## Restart Docker stack
```bash
docker compose -f docker/docker-compose.yml --env-file .env down
docker compose -f docker/docker-compose.yml --env-file .env up -d livekit redis weaviate voice-agent session-manager listener
```

## Verify NAOqi connection
```bash
python2 - <<'PY'
import qi
s = qi.Session()
s.connect("tcp://10.0.0.149:9559")
print("CONNECTED")
PY
```
