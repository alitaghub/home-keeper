# Starlette's TestClient is typed against httpx2 but falls back to httpx at runtime, which
# is what this workspace installs. Point the type checker at httpx so TestClient is typed.
from httpx import *
