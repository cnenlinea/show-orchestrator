"""Deploy the web UI to the Arcane host through Arcane's API: the image built there, the container run there, the
shows kept in a volume that outlives every redeploy. (The same approach as SauronsEye's tools/arcane_deploy.py.)

    python tools/arcane_deploy.py status     what the host has: workspace, image builds, container, volumes
    python tools/arcane_deploy.py deploy     sync + build + up
    python tools/arcane_deploy.py sync       copy the Docker build context into the host's build workspace
    python tools/arcane_deploy.py build      build show-orchestrator:latest there
    python tools/arcane_deploy.py up         recreate the container from the image (env: the login)

Settings come from the environment, then this repository's .env, then SauronsEye's .env (keys in any case):
    arcane_server           the Arcane host, e.g. arcane.example.org
    arcane_api              an Arcane API key (or arcane_apikey, arcane_api_key)
    orchestrator_users      "name:password,name:password"  -> SHOW_ORCHESTRATOR_USERS
    orchestrator_password   one password, user "show"       -> SHOW_ORCHESTRATOR_PASSWORD
                            (falls back to replay_password, the SauronsEye replay's)
`up` refuses to start the container without a login unless given --open.

`sync` uploads what the Dockerfile copies (the files .dockerignore lets through, shows and their media included)
into Arcane's build workspace, one request per file, skipping binaries already there with the same size. `build`
runs Arcane's image build on it. `up` removes the running container (its volumes stay) and creates it again:
port 8771, volumes show-orchestrator-shows (/app/shows) and show-orchestrator-build (/app/build). On start the
container copies the image's shows into the volume only where the volume has no file of that name, so a show
edited on the server is kept, and a show changed on this machine reaches the server only once the server's copy
is deleted.

`--show shows/<...>/<show>.yaml` (repeatable) sends only those shows from shows/, and of each only what it uses:
the show file, its note lock, included files and media (as a bundle holds them). Without it, all of shows/ goes.
A proxy in front of Arcane may refuse a large request (Cloudflare: 100 MB), so `sync` warns about files that big.
"""
from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILES = [ROOT / ".env", Path("H:/SauronsEye/.env")]
API_KEY_NAMES = ("arcane_api", "arcane_apikey", "arcane_api_key")
LARGE_FILE = 95 * 1000**2  # just under Cloudflare's 100 MB request limit

NAME = "show-orchestrator"                    # the image, the container and the build workspace
PORT = "8771"
WORKSPACE = "/" + NAME                        # in Arcane's builds workspace (browse paths are relative to it)
CONTEXT = "/builds" + WORKSPACE               # the same folder as the image build sees it
ENV_ID = "0"                                  # Arcane's local Docker environment
SHOWS_VOLUME, BUILD_VOLUME = f"{NAME}-shows", f"{NAME}-build"
UA = "show-orchestrator-arcane-deploy/1"      # Cloudflare in front of Arcane blocks Python's default agent
TEXT_SUFFIXES = (".py", ".html", ".yaml", ".yml", ".json", ".txt", ".csv", ".lock", "Dockerfile")


def settings(env_files: list[Path]) -> dict[str, str]:
    """KEY=VALUE from the .env files (the first file that has a key wins), under the environment; keys lowercased."""
    out: dict[str, str] = {}
    for path in reversed(env_files):
        if path.is_file():
            for line in path.read_text(encoding="utf-8-sig").splitlines():
                key, sep, value = line.strip().partition("=")
                if sep and key and not key.startswith("#"):
                    out[key.strip().lower()] = value.strip().strip("\"'")
    out.update({k.lower(): v for k, v in os.environ.items()})
    return out


class Arcane:
    def __init__(self, host: str, key: str, env: str = ENV_ID):
        self.base = (host if host.startswith("http") else "https://" + host).rstrip("/") + "/api"
        self.key = key
        self.env = env

    def raw(self, method: str, path: str, data: bytes | None = None, kind: str = "application/json",
            timeout: float = 600.0) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "X-API-Key": self.key, "Content-Type": kind, "Accept": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as ex:
            return ex.code, ex.read()

    def call(self, method: str, path: str, body=None, timeout: float = 600.0) -> dict:
        code, text = self.raw(method, path, json.dumps(body).encode() if body is not None else None, timeout=timeout)
        try:
            res = json.loads(text or b"{}")
        except ValueError:
            res = {"raw": text[:4000].decode(errors="replace")}
        if code >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {code} {res.get('detail') or res.get('error') or res}")
        return res

    def e(self, path: str) -> str:
        return f"/environments/{self.env}{path}"

    def upload(self, dest_dir: str, data: bytes, name: str) -> None:
        """One file into a build workspace folder (multipart; the file keeps its name)."""
        b = uuid.uuid4().hex
        body = io.BytesIO()
        body.write(f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
                   f"Content-Type: application/octet-stream\r\n\r\n".encode())
        body.write(data)
        body.write(f"\r\n--{b}--\r\n".encode())
        code, text = self.raw("POST", self.e(f"/builds/browse/upload?path={urllib.parse.quote(dest_dir)}"),
                              body.getvalue(), f"multipart/form-data; boundary={b}")
        if code >= 400:
            raise RuntimeError(f"upload {name} -> {dest_dir}: HTTP {code} {text[:300].decode(errors='replace')}")

    def container(self) -> dict | None:
        try:
            return self.call("GET", self.e(f"/containers/{NAME}")).get("data")
        except RuntimeError as ex:
            if "404" in str(ex):
                return None
            raise

    def volumes(self) -> dict[str, dict]:
        return {v["name"]: v for v in self.call("GET", self.e("/volumes?limit=200")).get("data") or [] if v.get("name")}

    def workspace(self, sub: str = "") -> list[dict]:
        try:
            return self.call("GET", self.e(f"/builds/browse?path={urllib.parse.quote(WORKSPACE + sub)}")).get("data") or []
        except RuntimeError:
            return []

    def builds(self, n: int = 5) -> list[dict]:
        return [b for b in self.call("GET", self.e("/images/builds?limit=50")).get("data") or []
                if NAME in " ".join(b.get("tags") or [])][:n]


def show_files(shows: list[Path]) -> set[str]:
    """What the given show files use, as paths relative to the repo (see show_orchestrator.bundle)."""
    sys.path.insert(0, str(ROOT))
    from show_orchestrator.bundle import show_dependencies
    from show_orchestrator.parser import Parser

    needed = set()
    for show in shows:
        relative = (show if show.is_absolute() else Path.cwd() / show).resolve().relative_to(ROOT)
        needed.update(str(p) for p in show_dependencies(ROOT, relative, Parser().load_show(ROOT / relative)))
    return needed


def context_files(shows: list[Path] | None = None) -> list[tuple[str, Path]]:
    """[(path relative to the repo, absolute path)] of what the image build needs: .dockerignore's whitelist,
    with only what `shows` use from shows/ when they are given."""
    needed = show_files(shows) if shows else None
    allow, deny = [], []
    for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            (allow if line.startswith("!") else deny).append(line.lstrip("!"))

    def denied(rel: str) -> bool:
        parts = rel.split("/")
        for d in deny:
            if d == "**":
                continue
            d2 = d.replace("**/", "")
            if fnmatch.fnmatch(rel, d) or fnmatch.fnmatch(rel, d2) or any(fnmatch.fnmatch(p, d2) for p in parts):
                return True
        return False

    out, seen = [], set()
    for pat in allow:
        for p in sorted(ROOT.glob(pat.rstrip("/"))):
            for f in sorted(p.rglob("*")) if p.is_dir() else [p]:
                rel = f.relative_to(ROOT).as_posix()
                if needed is not None and rel.startswith("shows/") and rel not in needed:
                    continue
                if f.is_file() and not denied(rel) and rel not in seen:
                    seen.add(rel)
                    out.append((rel, f))
    return out


def sync(a: Arcane, shows: list[Path] | None = None) -> int:
    files = context_files(shows)
    print(f"build context: {len(files)} files, {sum(p.stat().st_size for _, p in files) / 1e6:.1f} MB -> {CONTEXT}")
    for rel, p in files:
        if p.stat().st_size > LARGE_FILE:
            print(f"  warning: {rel} is {p.stat().st_size / 1e6:.0f} MB; a proxy in front of Arcane may refuse it")
    there, there_dirs = {}, set()                        # rel -> size on the host, to skip what matches

    def walk(sub: str):
        for e in a.workspace(sub):
            rel = (sub + "/" + e["name"]).lstrip("/")
            if e.get("isDirectory"):
                there_dirs.add(rel)
                walk("/" + rel)
            else:
                there[rel] = e.get("size")
    walk("")
    made, sent, skipped = set(), 0, 0
    for rel, p in files:
        if there.get(rel) == p.stat().st_size and not rel.endswith(TEXT_SUFFIXES):
            skipped += 1                                 # a binary of the same size: the same file
            continue
        d = os.path.dirname(rel)
        parts = d.split("/") if d else []
        for i in range(1, len(parts) + 1):               # mkdir is not recursive: each parent first
            sub = "/".join(parts[:i])
            if sub not in made:
                if sub not in there_dirs:
                    a.call("POST", a.e(f"/builds/browse/mkdir?path={urllib.parse.quote(WORKSPACE + '/' + sub)}"), {})
                made.add(sub)
        a.upload(WORKSPACE + ("/" + d if d else ""), p.read_bytes(), p.name)
        sent += 1
        if sent % 25 == 0:
            print(f"  {sent} sent ...")
    # The workspace holds only what the image needs, so its .dockerignore is minimal (the host's BuildKit reads the
    # repository's whitelist differently, as SauronsEye found).
    a.upload(WORKSPACE, b"**/__pycache__\n*.part\n", ".dockerignore")
    print(f"  {sent} sent, {skipped} binaries already there")
    return 0


def commit_tag() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True,
                              timeout=10).stdout.strip() or "local"
    except (OSError, subprocess.SubprocessError):
        return "local"


def build(a: Arcane) -> int:
    tag = commit_tag()
    print(f"building {NAME}:latest ({tag}) on the host from {CONTEXT} ...")
    t0 = time.time()
    code, text = a.raw("POST", a.e("/images/build"), json.dumps({
        "contextDir": CONTEXT, "dockerfile": "Dockerfile", "tags": [f"{NAME}:latest", f"{NAME}:{tag}"],
        "load": True, "pull": True, "noCache": False}).encode(), timeout=3600)
    lines = [ln for ln in text.decode(errors="replace").splitlines() if ln.strip()]
    print("\n".join("  " + ln[:160] for ln in lines[-12:]))
    rec = (a.builds(1) or [{}])[0]
    print(f"  HTTP {code}, {time.time() - t0:.0f} s; last build record: {rec.get('status')} "
          f"{rec.get('createdAt', '')[:19]} tags {rec.get('tags')} {(rec.get('error') or '')[:200]}")
    return 0 if code < 400 and rec.get("status") == "success" else 1


def login_env(conf: dict[str, str]) -> list[str]:
    """The container's login: SHOW_ORCHESTRATOR_USERS / _PASSWORD from the settings."""
    env = []
    if conf.get("orchestrator_users"):
        env.append(f"SHOW_ORCHESTRATOR_USERS={conf['orchestrator_users']}")
    password = conf.get("orchestrator_password")
    if not password and conf.get("replay_password"):
        print("  login: no orchestrator_password, using replay_password (user \"show\")")
        password = conf["replay_password"]
    if password:
        env.append(f"SHOW_ORCHESTRATOR_PASSWORD={password}")
    return env


def up(a: Arcane, conf: dict[str, str], allow_open: bool) -> int:
    env = login_env(conf)
    if not env and not allow_open:
        raise SystemExit("no login: set orchestrator_users or orchestrator_password in .env (or pass --open)")
    cont = a.container()
    if cont:
        print(f"removing the running {NAME} ({cont.get('image')}, created {cont.get('created', '')[:19]}); its volumes stay ...")
        a.call("DELETE", a.e(f"/containers/{cont.get('id') or NAME}?force=true"))
        time.sleep(2)
    print(f"creating {NAME} from {NAME}:latest (env: {', '.join(e.split('=')[0] for e in env) or 'no login'}) ...")
    res = a.call("POST", a.e("/containers"), {
        "name": NAME, "image": f"{NAME}:latest", "restartPolicy": "unless-stopped", "ports": {PORT: PORT},
        "volumes": [f"{SHOWS_VOLUME}:/app/shows", f"{BUILD_VOLUME}:/app/build"], "env": env,
        "labels": {"app": NAME, "show-orchestrator.commit": commit_tag()}}, timeout=300)
    print(" ", json.dumps(res.get("data") or res)[:200])
    time.sleep(4)
    return status(a)


def status(a: Arcane) -> int:
    ws = a.workspace()
    print("workspace: ", f"{CONTEXT}: {', '.join(e['name'] for e in ws)}" if ws else f"{CONTEXT}: empty (run sync)")
    for b in a.builds(2):
        print("build:     ", f"{b.get('status')} {b.get('createdAt', '')[:19]} {b.get('durationMs', 0) / 1000:.0f} s "
                             f"tags {b.get('tags')} {(b.get('error') or '')[:120]}")
    cont = a.container()
    if cont:
        st = cont.get("state")
        labels = cont.get("labels") or {}
        print("container: ", f"{NAME} {st.get('status') if isinstance(st, dict) else st}, image {cont.get('image')}, "
                             f"created {cont.get('created', '')[:19]}, commit {labels.get('show-orchestrator.commit') or '?'}")
    else:
        print("container:  none")
    vols = a.volumes()
    for n in (SHOWS_VOLUME, BUILD_VOLUME):
        print(f"volume:     {n}: {'there' if n in vols else 'none'}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["status", "deploy", "sync", "build", "up"])
    ap.add_argument("--host", default=None, help="Arcane host (.env arcane_server)")
    ap.add_argument("--key", default=None, help="Arcane API key (.env arcane_api)")
    ap.add_argument("--env-file", action="append", type=Path, default=None,
                    help=f"a .env to read (repeatable; default: {', '.join(str(p) for p in ENV_FILES)})")
    ap.add_argument("--open", action="store_true", help="start the container even without a login")
    ap.add_argument("--show", dest="shows", action="append", type=Path, default=None,
                    help="send only this show (and what it uses) from shows/; repeatable (default: all of shows/)")
    args = ap.parse_args(argv)
    conf = settings(args.env_file or ENV_FILES)
    host = args.host or conf.get("arcane_server")
    key = args.key or next((conf[k] for k in API_KEY_NAMES if conf.get(k)), None)
    if not host or not key:
        raise SystemExit("arcane_server and arcane_api must be in .env (or --host / --key)")
    arc = Arcane(host, key)
    if args.cmd == "status":
        return status(arc)
    if args.cmd == "sync":
        return sync(arc, args.shows)
    if args.cmd == "build":
        return build(arc)
    if args.cmd == "up":
        return up(arc, conf, args.open)
    if not login_env(conf) and not args.open:  # before minutes of upload and build, not after
        raise SystemExit("no login: set orchestrator_users or orchestrator_password in .env (or pass --open)")
    if sync(arc, args.shows) or build(arc):
        return 1
    return up(arc, conf, args.open)


if __name__ == "__main__":
    sys.exit(main())
