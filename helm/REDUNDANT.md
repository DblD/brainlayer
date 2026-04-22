# ⚠️ This Helm chart is REDUNDANT

Deployment is GitOps-managed via Flux. Authoritative manifests live at:

`~/.code/scratch/k8s-platform/platform/apps/brainlayer/`

Kept here temporarily as reference while the first cluster deploy is verified.
**Delete once cluster deploy is confirmed stable** (see `DEPLOY.md` in repo root).

Do NOT `helm install` from this chart — it will collide with Flux-managed
resources.
