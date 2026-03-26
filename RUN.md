# RPi Run Commands

Safe startup runs automatically inside Docker (safe-startup service).

Pepper can be turned on before or after — the watchdog will detect her.

## 1. Start all services

This project's standard workflow includes both the `audio` and `debug`
profiles, so "all services" means the base stack plus:
- `user-client` (`audio`)
- `dev-console` (`debug`)

```bash
docker compose -f docker/docker-compose.yml --env-file .env --profile audio --profile debug up -d
```

## 2. Start only user-client

```bash
docker compose -f docker/docker-compose.yml --env-file .env --profile audio up -d user-client
```

## 3. Rebuild and restart everything

```bash
docker compose -f docker/docker-compose.yml --env-file .env --profile audio --profile debug up -d --build
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
