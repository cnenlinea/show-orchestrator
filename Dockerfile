# The web UI (web.py) on a server, behind its login.
#   docker build -t show-orchestrator .        then see compose.yaml, or tools/arcane_deploy.py for the Arcane host
# Shows live in the /app/shows volume (seeded from the image on start, never overwriting), builds in /app/build.
FROM python:3.14-slim

# git: reathon installs from a Git fork (requirements.txt)
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# python-rtmidi has no Linux wheel, and a server has no MIDI ports to audition notes on (the UI lists none);
# pytest is for development.
RUN grep -viE '^(python-rtmidi|pytest)\b' requirements.txt > /tmp/requirements.txt \
 && pip install --no-cache-dir -r /tmp/requirements.txt

COPY main.py web.py ./
COPY show_orchestrator/ show_orchestrator/
COPY templates/ templates/
COPY shows/ seed/shows/

ENV PYTHONUNBUFFERED=1
RUN mkdir -p /app/shows /app/build
VOLUME ["/app/shows", "/app/build"]

EXPOSE 8771
# The shows from the image go into the volume only where the volume has no file of that name, so what was
# edited on the server survives a redeploy. (`;`: cp -n reports skipped files as a failure on newer coreutils.)
CMD ["sh", "-c", "cp -rn /app/seed/shows/. /app/shows/ 2>/dev/null; exec python web.py --host 0.0.0.0 --port 8771"]
