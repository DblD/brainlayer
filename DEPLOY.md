# Deploy

BrainLayer runs locally on Dario's Mac as a personal daemon (`localhost:8787`). For in-cluster consumers (scott-gpt's RAG, future apps), there's a GitOps-managed cluster instance at `brainlayer.brainlayer.svc.cluster.local:8787`.

## What lives where

| Concern | Location |
|---|---|
| Daemon code | `src/brainlayer/` (this repo) |
| Container image build | `Dockerfile` (this repo, prefetches bge-large model) |
| K8s manifests | `k8s-platform/platform/apps/brainlayer/` |
| Flux Kustomization | `k8s-platform/platform/flux/kustomization-brainlayer.yaml` |

> **NOTE**: `helm/` directory in this repo is **redundant** — superseded by the
> GitOps manifests above. Kept for reference until cluster deploy is confirmed
> working; slated for removal.

## Cluster deploy (first time)

```bash
# 1. Build + push image (heavy — ~1.5 GB due to baked-in model)
cd ~/.code/scratch/brainlayer
docker build -t harbor.dbld.tech/brainlayer/daemon:1.0.0 .
docker push harbor.dbld.tech/brainlayer/daemon:1.0.0

# 2. Commit k8s-platform
cd ~/.code/scratch/k8s-platform
git add platform/apps/brainlayer platform/flux/kustomization-brainlayer.yaml
git commit -m "feat(brainlayer): initial cluster deploy 1.0.0"
git push

# 3. Wait for Flux. First boot loads model into RAM (~60-90s).
flux reconcile kustomization brainlayer -n flux-system --with-source
kubectl -n brainlayer rollout status deploy/brainlayer

# 4. Test /embed
kubectl -n brainlayer port-forward svc/brainlayer 8787:8787 &
curl -X POST http://localhost:8787/embed \
  -H 'content-type: application/json' \
  -d '{"text":"ping"}' | jq '.dim'
# → 1024
```

## ⚠️ Empty = half-useless

Only `/embed` is useful on a freshly deployed cluster brainlayer. `/search`, `/context`, `/brain/graph` all hit an empty sqlite-vec DB. scott-gpt only uses `/embed`, so it doesn't care — but any future tenant expecting semantic memory will find nothing.

**Commission with either a migration (below) or an explicit decision to ship empty and populate via `POST /digest` calls over time.**

## Optional: migrate personal DB into cluster

If you want the cluster instance to also carry your 73K+ chunks of cross-project memory:

```bash
pkill -f "brainlayer.daemon"   # stop local to avoid WAL contention
POD=$(kubectl -n brainlayer get pod -l app=brainlayer -o jsonpath='{.items[0].metadata.name}')
kubectl -n brainlayer cp ~/.local/share/brainlayer/brainlayer.db $POD:/data/brainlayer/brainlayer.db
kubectl -n brainlayer rollout restart deploy/brainlayer

# restart local on Mac
cd ~/.code/scratch/brainlayer
nohup .venv/bin/python3 -m brainlayer.daemon --http 8787 --mcp > /tmp/brainlayer.log 2>&1 &
```

Cluster and local instances diverge from that point forward. No auto-sync.

## Local dev

Still runs on your Mac for personal memory:
```bash
cd ~/.code/scratch/brainlayer
.venv/bin/python3 -m brainlayer.daemon --http 8787 --mcp
```

scott-gpt local dev points at `http://localhost:8787` (default `BRAINLAYER_URL`). Production scott-gpt points at `http://brainlayer.brainlayer.svc.cluster.local:8787`.
