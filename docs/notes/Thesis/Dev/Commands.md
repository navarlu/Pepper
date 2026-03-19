# Useful Commands

## Network diagnostics
```bash
ip -br link
ip -br addr
ip route get 10.0.0.149
ethtool enx00e04c026907 | grep "Link detected"
ping -I enx00e04c026907 -c 3 10.0.0.149
```

## Docker
```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d
docker compose -f docker/docker-compose.yml --env-file .env down
docker compose -f docker/docker-compose.yml --env-file .env ps
```

## Animation test
```bash
curl -X POST "http://localhost:5000/animation/Embarrassed_1"
```

## Choregraphe
```bash
./choregraphe/bin/choregraphe-bin
```

## Thesis data ingest
```bash
cd /home/lucas/Projects/FEL/Pepper/voice-agent
uv run python ../docs/thesis/resources/ingest_data.py
```

## Git → Overleaf sync
```bash
git add docs/thesis/latex/main
git commit -m "thesis: update ..."
git push origin main

git fetch overleaf
git worktree add /tmp/overleaf-sync overleaf/master
rsync -a --delete --exclude '.git' docs/thesis/latex/main/ /tmp/overleaf-sync/
cd /tmp/overleaf-sync
git add -A
git commit -m "Checkpoint from local thesis state"
git push overleaf HEAD:master
cd /home/lucas/Projects/FEL/Pepper
git worktree remove /tmp/overleaf-sync
```
