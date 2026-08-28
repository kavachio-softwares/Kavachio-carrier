# Helm chart: kavachio

```bash
# Dev
helm upgrade --install kavachio charts/kavachio -f charts/kavachio/values-dev.yaml -n kavachio --create-namespace

# Production
helm upgrade --install kavachio charts/kavachio -f charts/kavachio/values-prod.yaml -n kavachio
```
