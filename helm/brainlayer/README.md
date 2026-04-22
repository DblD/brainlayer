# BrainLayer Helm chart

Containerized BrainLayer daemon. Default deploy = empty memory DB + BGE embedder, exposing `/embed` and `/search` on `:8787` inside the cluster.

## Why this exists

BrainLayer runs great on Dario's Mac (localhost:8787, personal memory of 73K chunks). But other services — notably **scott-gpt** — need the embedder at runtime, and localhost isn't reachable from a k8s cluster. This chart packages the daemon so any in-cluster consumer can call it.

## Build + push image

```bash
cd ~/.code/scratch/brainlayer
docker build -t harbor.dbld.tech/brainlayer/daemon:1.0.0 .
docker push harbor.dbld.tech/brainlayer/daemon:1.0.0
```

First build pulls the ~1.3 GB BAAI/bge-large-en-v1.5 model into the image. Subsequent builds are fast (~seconds) thanks to Docker layer cache.

## Install

```bash
kubectl create ns brainlayer
helm upgrade --install brainlayer helm/brainlayer \
  -n brainlayer --set image.tag=1.0.0
```

## Consumer example (scott-gpt)

In `scott-gpt` Helm values:

```yaml
env:
  BRAINLAYER_URL: "http://brainlayer.brainlayer.svc.cluster.local:8787"
```

Or cross-namespace via ExternalName, or expose via ingress.

## Import Dario's personal memory (optional)

By default the PVC is empty — the daemon will create a fresh DB on first start. To migrate your Mac's ~546MB brainlayer.db:

```bash
# From the Mac
kubectl -n brainlayer cp ~/.local/share/brainlayer/brainlayer.db \
  $(kubectl -n brainlayer get pod -l app.kubernetes.io/name=brainlayer -o name | head -1):/data/brainlayer/brainlayer.db

# Restart to reload
kubectl -n brainlayer rollout restart deploy/brainlayer
```

Safer alternative — deploy a *new* instance per use case:
- `brainlayer-personal` namespace → Dario's memory
- `brainlayer-shared` namespace → clean, only serves `/embed` for other apps

## GPU (optional)

bge-large runs fine on CPU (~50-80 ms/query). If you have a GPU node, set:

```yaml
nodeSelector:
  gpu: nvidia
env:
  CUDA_VISIBLE_DEVICES: "0"
```

and ensure the base image has CUDA runtime (build `Dockerfile` on `nvidia/cuda:12.x-cudnn-runtime-*` instead of `python:3.12-slim`).
