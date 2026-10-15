# Same pinned base, by digest, as tools/fake-erp and backend/Dockerfile —
# Scorecard's PinnedDependencies treats an unpinned image as a supply-chain
# pivot point, and a fixture whose Python drifts under it stops being
# deterministic. Dependabot's docker ecosystem bumps tag + digest together.
FROM python:3.14-slim@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

WORKDIR /srv

# Hash-pinned install (`--require-hashes`): every package, transitive deps
# included, must match a hash in the lock. Regenerate requirements.txt from
# requirements.in with the `uv pip compile` line at the top of requirements.in.
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY app.py .
COPY providers/ providers/

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
