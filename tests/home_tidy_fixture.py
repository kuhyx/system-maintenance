"""Build a small but realistic fake home for the home-tidy tests.

The fixture mirrors the real hazards one level deep: a git repo whose
tracked file and pre-commit config reference another repo, a venv-style
absolute shebang, a symlink in ``~/.local/bin`` into a repo, a user unit
using ``%h``, ``user-dirs.dirs`` pointing at ``$HOME/``, a scoped
``.claude.json``, a Claude per-project memory dir, a service dir with a
compose file, and loose junk in the root.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

MANIFEST = """
[root]
allow = ["src", "vendor", "services", "data", "archive", "inbox", "Downloads", "Documents"]
git_only = ["src", "vendor"]
warn_bucket_size = 3
broad_buckets = ["src"]

[sweep]
grace_minutes = 60
max_entry_bytes = 100000
max_entry_count = 50
inbox_nag_days = 14
dry_run_hours_after_install = 24

[bridges]
expires = ""
paths = []

[xdg]
DESKTOP = "$HOME/inbox"
DOCUMENTS = "$HOME/Documents"

[env]
GOPATH = "$HOME/data/go"

[refs]
user_roots = ["~/.config/systemd/user", "~/.local/bin", "~/.claude/projects/*/memory"]
user_files = ["~/.zshrc"]
user_globs = ["~/.local/lib/python3*/site-packages/*.pth"]
sudo_roots = []
claude_json = "~/.claude.json"
claude_json_keys = ["mcpServers", "projects"]
claude_projects_dir = "~/.claude/projects"
repo_untracked_globs = [".git/hooks/*", ".env", ".venv/bin/*", ".venv/pyvenv.cfg"]
nongit_globs = ["*.yml", "*/*.yml"]
prune_dirs = [".git", "node_modules", "__pycache__", ".venv"]
prune_suffixes = [".jsonl"]
max_file_bytes = 100000

[map]
"todo" = "src/todo"
"utils" = "src/utils"
"wrap/inner" = "src/inner"
"wrap" = "archive/2026-01-01-wrap-leftovers"
"thirdparty" = "vendor/thirdparty"
"svc" = "services/svc"
"models" = "data/models"
"backup-2026-01-01" = "archive/2026-01-01-backup"
"junk.txt" = "inbox/junk.txt"

[aliases]
"gone" = "src/utils/gone"

[[hook]]
entry = "svc"
before = ["echo before-svc"]
after = ["echo after-svc"]

[rebind]
user = ["echo daemon-reload"]
sudo = []
restart_user_units = []
restart_sudo_units = []
commit_order_first = ["src/utils"]
finish_script = "~/fake_finish.sh"

[verify]
user = ["test -f ~/src/todo/README.md", "test -L ~/.local/bin/tool"]
sudo = []
"""

FAKE_FINISH = """#!/bin/bash
# Fake finish_auto.sh: -P -m subject repo files...
set -euo pipefail
shift 3
repo="$1"; shift
git -C "$repo" add -- "$@"
git -C "$repo" -c user.name=t -c user.email=t@t commit -q -m "rewrite" -- "$@"
echo "committed $repo"
"""


def _git_init(repo: Path, files: dict[str, str]) -> None:
    repo.mkdir(parents=True)
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True, env=env)


def build_home(home: Path) -> Path:
    """Populate ``home`` and return the manifest path written inside it."""
    h = str(home)
    _git_init(home / "todo", {
        "README.md": f"see ~/utils/scripts/x.sh and {h}/utils/lib\nand ~/gone/old.py\n",
        ".pre-commit-config.yaml": "entry: ~/utils/scripts/check.sh\n",
        "run.sh": "#!/bin/bash\ncd $HOME/todo\n",
    })
    _git_init(home / "utils", {"scripts/x.sh": "#!/bin/bash\necho x\n", "bin/tool": "#!/bin/sh\necho tool\n"})
    _git_init(home / "thirdparty", {"a.txt": "nothing\n"})
    (home / "wrap").mkdir()
    _git_init(home / "wrap" / "inner", {"i.txt": f"{h}/wrap/inner/i.txt\n"})
    (home / "wrap" / "left.txt").write_text("leftover\n")
    (home / "todo" / ".env").write_text(f"ROOT={h}/todo\n")
    (home / "todo" / ".git" / "hooks" / "pre-commit").write_text(f"#!/bin/sh\n{h}/todo/.venv/bin/python\n")
    venv_bin = home / "todo" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    (venv_bin / "pip").write_text(f"#!{h}/todo/.venv/bin/python3\n")
    (venv_bin / "python3").write_bytes(b"\x7fELF\0\0binary")
    (venv_bin / "python").write_text("#!/bin/sh\necho 3.99\n")
    for exe in (venv_bin / "python", venv_bin / "pip", home / "utils" / "bin" / "tool"):
        exe.chmod(0o755)
    (home / "todo" / ".venv" / "pyvenv.cfg").write_text(f"command = /usr/bin/python3 -m venv {h}/todo/.venv\n")
    (home / "svc").mkdir()
    (home / "svc" / "docker-compose.yml").write_text(f"volumes:\n  - {h}/svc/data:/data\n  - {h}/todo/dist:/dist\n")
    (home / "models").mkdir()
    (home / "models" / "big.bin").write_bytes(b"\0" * 10)
    (home / "backup-2026-01-01").mkdir()
    (home / "junk.txt").write_text("junk\n")
    (home / "Downloads").mkdir()
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "todo.service").write_text("[Service]\nWorkingDirectory=%h/todo\nExecStart=%h/todo/run.sh\n")
    (home / ".config" / "user-dirs.dirs").write_text('XDG_DESKTOP_DIR="$HOME/"\nXDG_DOWNLOAD_DIR="$HOME/Downloads"\nXDG_DOCUMENTS_DIR="$HOME/"\n')
    lbin = home / ".local" / "bin"
    lbin.mkdir(parents=True)
    os.symlink(home / "utils" / "bin" / "tool", lbin / "tool")
    os.symlink("../../utils/scripts/x.sh", lbin / "rel-tool")
    (lbin / "wrapper").write_text(f"#!/bin/sh\nexec {h}/utils/bin/tool\n")
    sp = home / ".local" / "lib" / "python3.99" / "site-packages"
    sp.mkdir(parents=True)
    (sp / "__editable__.todo.pth").write_text(f"{h}/todo\n")
    (home / ".zshrc").write_text("export PATH=$HOME/utils/bin:$PATH\n")
    (home / ".claude.json").write_text(
        '{"numStartups": 1, "mcpServers": {"t": {"command": "' + h + '/utils/bin/tool", "args": ["--dir", "' + h + '/todo"]}},'
        ' "projects": {"' + h + '/todo": {"history": ["ran ' + h + '/todo/x"], "mcpServers": {"p": {"command": "' + h + '/todo/.venv/bin/python"}}},'
        ' "' + h + '/other": {"history": []}}}\n'
    )
    mem = home / ".claude" / "projects" / ("-" + h.strip("/").replace("/", "-") + "-todo") / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text(f"- lives at {h}/todo\n")
    (home / ".claude" / "projects" / ("-" + h.strip("/").replace("/", "-") + "-todo") / "abc.jsonl").write_text(f"{h}/todo history\n")
    finish = home / "fake_finish.sh"
    finish.write_text(FAKE_FINISH)
    finish.chmod(0o755)
    manifest = home / "home-tidy.toml"
    manifest.write_text(MANIFEST)
    return manifest
