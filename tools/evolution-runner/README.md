# Heliox Evolution Runner

This local image is the only supported execution backend for generated code
candidates. Build it from the repository root:

```powershell
docker build -f tools/evolution-runner/Dockerfile -t heliox-evolution-runner:0.10.0 .
```

The daemon never pulls this image automatically. At runtime it starts the image
with networking disabled, a read-only root filesystem, all Linux capabilities
dropped, `no-new-privileges`, CPU/memory/PID limits, no inherited credentials,
and only one disposable Git worktree mounted at `/workspace`.

If Docker or this exact local image is unavailable, evolutionary evaluation
fails closed. There is no host-process fallback.

The harness compares each candidate with the recorded baseline and archives
deterministic/advisory evidence. It cannot merge, push, publish a release,
access release credentials, modify the installed application, or start a live
user experiment. An exact candidate-ID promotion request is evidence for
external human review, not automatic promotion.
