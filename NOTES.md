https://docs.google.com/document/d/18wjGIsm8TbbgqFEvbgbX9QL37sdoOspIgIwiWLcWKaw/edit?pli=1&tab=t.0#heading=h.kmtf914entwk

## gitguide:

### 1) Update GitHub (source of truth)

```bash
cd /home/lucas/Projects/FEL/Pepper
git add docs/thesis/latex/main
git commit -m "thesis: update related work section"
git push origin main
```

### 2) Mirror same state to Overleaf (checkpoint)

```bash
cd /home/lucas/Projects/FEL/Pepper
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

## operator panel adress:

http://100.111.97.63:8787/

## run on GPU server

```bash
ssh navarlu2@ptak.felk.cvut.cz
ssh lie
tmux attach -t lie

vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes
```

## safe_startup_example:

```text
lucas@lucas-rpi-5-8gb:~/Projects/FEL/Pepper$ uv run python robot/utils/safe_startup.py 
Safe startup 3 staring...
[info] no URL provided, waiting for Pepper to appear on the network...
[info] (you can power on Pepper now — this will keep retrying)
[discover] searching for Pepper on the network...
[discover] attempt 2, retrying...
[discover] attempt 3, retrying...
[discover] attempt 4, retrying...
[discover] mDNS resolved pepper.local -> 192.168.210.113
[discover] 192.168.210.113 found via mDNS but port 9559 not open yet
[discover] attempt 5, retrying...
[discover] mDNS resolved pepper.local -> 192.168.210.113
[info] discovered Pepper at tcp://192.168.210.113:9559
[W] 1774355076.247677 16476 qi.path.sdklayout: No Application was created, trying to deduce paths
[info] waiting for NAOqi at tcp://192.168.210.113:9559
[wait] connect attempt 1 to tcp://192.168.210.113:9559...
[wait] waiting for connect future (timeout 10s)...
[wait] connected!
[info] connected; waiting for core services...
[info] service ready: ALMotion
[info] service ready: ALAutonomousLife
[info] service ready: ALRobotPosture
[info] service ready: ALDiagnosis
[ok] ALMotion.setDiagnosisEffectEnabled(False) -> None
[warn] ALAutonomousLife.setState('disabled') failed:    AutonomousLife::setState
        Calls to the setState method are not currently allowed. Did you finish the getting started wizard?
[ok] setAutonomousAbilityEnabled(AutonomousBlinking, False) -> None
[ok] setAutonomousAbilityEnabled(BackgroundMovement, False) -> None
[ok] setAutonomousAbilityEnabled(BasicAwareness, False) -> None
[ok] setAutonomousAbilityEnabled(ListeningMovement, False) -> None
[ok] setAutonomousAbilityEnabled(SpeakingMovement, False) -> None
[ok] ALMotion.wakeUp() -> False
[ok] ALRobotPosture.goToPosture('StandInit', 0.6) -> False
[ok] ALDiagnosis.getPassiveDiagnosis() -> []
[ok] ALDiagnosis.getActiveDiagnosis() -> []
[done] stabilization complete. Pepper at tcp://192.168.210.113:9559
```
