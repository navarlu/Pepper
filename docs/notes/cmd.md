## Common commands

Copy agent files to woska (after editing voice-agent code in local mode):
```bash
scp -J navarlu2@ptak.felk.cvut.cz \
  voice-agent/src/{agent.py,tools.py,config.py} \
  navarlu2@woska:/mnt/data_personal/navarlu2/work/Pepper/voice-agent/src/
```

SSH to ptak (jump host):
```bash
ssh navarlu2@ptak.felk.cvut.cz
```

Restart a service after compose env changes (formerly session-manager — now orchestrator):
```bash
docker compose -f docker/docker-compose.yml restart orchestrator
```

Switch agent mode (from inside the chat CLI):
```
/mode openai
/mode local
```
Or directly write the config file:
```bash
echo '{"agent_mode": "openai"}' > services/src/orchestrator_config.json
```
