# k8s - Kubernetes manifests

## Apply order
```bash
kubectl apply -f k8s/base/namespace.yaml
kubectl apply -f k8s/base/secrets.example.yaml     # replace with a real secret first
kubectl apply -f k8s/base/postgres.yaml            # ExternalName alias -> managed DB (not a DB pod)
kubectl apply -f k8s/base/network-policy.yaml
kubectl apply -f k8s/services/
kubectl apply -f k8s/base/gateway.yaml
```

For templated multi-environment deploys, prefer the Helm chart in `charts/kavachio`.
