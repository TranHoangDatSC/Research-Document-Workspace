# Keeps `pytest tests/` from importing the live-stack scripts in
# tests/integration/ (they need Docker running and are run one by one).
collect_ignore = ["integration"]
