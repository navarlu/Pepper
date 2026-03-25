# RPi Run Commands

Safe startup runs automatically inside Docker (safe-startup service).

Pepper can be turned on before or after — the watchdog will detect her.

## 1. Start all services

```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d
```

## 2. Start user-client (audio profile, optional)

```bash
docker compose -f docker/docker-compose.yml --env-file .env --profile audio up -d user-client
```

## 3. Rebuild and restart everything

```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d --build
```

## 4. Restart single service (e.g. bridge after code change)

```bash
docker compose -f docker/docker-compose.yml --env-file .env restart bridge
```

## 5. Check logs

```bash
docker compose -f docker/docker-compose.yml --env-file .env logs --tail=30 bridge
docker compose -f docker/docker-compose.yml --env-file .env logs --tail=30 listener
docker compose -f docker/docker-compose.yml --env-file .env logs --tail=30 safe-startup
```
