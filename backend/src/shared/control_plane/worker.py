"""Local worker adapter.

This is the disposable Vicoa-owned lifecycle. It creates a git worktree and
runs a bounded local process. It does not call Antigravity, Codex, Claude, or
/opt/agent-control.

The live Antigravity launcher is integrations.headless.antigravity.session.AntigravitySession.start.
Calling that against the existing agy-1 or agy-2 homes would touch live work
and spend quota, so this adapter refuses those homes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .store import LIVE_AGENT_CONTROL_DB, ControlPlane, ControlPlaneError

LIVE_RUNTIME_HOMES = {
    "/home/agentctl/runtimes/agy-1",
    "/home/agentctl/runtimes/agy-2",
}


class LocalWorker:
    def __init__(self, plane: ControlPlane, work_root: str | Path):
        self.plane = plane
        self.work_root = Path(work_root)

    def launch(self, task_id: int, *, repo: str | Path, argv: list[str], pool: str | None = None) -> dict:
        repo_path = Path(repo).resolve()
        if repo_path == Path("/opt/projects") or str(repo_path).startswith("/opt/projects/"):
            raise ControlPlaneError("live_project", "refusing a live Agent Control project")
        if Path(LIVE_AGENT_CONTROL_DB) in repo_path.parents:
            raise ControlPlaneError("live_project", "refusing the live control-plane tree")
        task = self.plane.task(task_id)
        if task["protected"] or task["import_hold"] or task["source_system"] == "agent-control":
            raise ControlPlaneError("held", "refusing to launch held, protected, or imported work")
        decision = self.plane.route(task_id, pool=pool)
        account_id = decision["account_id"]
        self._refuse_live_home(account_id)
        worktree = self._worktree(task_id, repo_path)
        started = self.plane.start_task(task_id, account_id=account_id, session_id=f"local-{task_id}")
        if started["worker_status"] != "running":
            raise ControlPlaneError("state", "worker did not enter running")
        try:
            proc = subprocess.run(
                argv,
                cwd=worktree,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
            )
        except subprocess.TimeoutExpired as exc:
            self.plane.crash_worker(task_id)
            raise ControlPlaneError("timeout", "local worker timed out") from exc
        output = (proc.stdout or proc.stderr or "")[:4000]
        completed = self.plane.complete_worker(
            task_id,
            worktree_path=str(worktree),
            branch=f"canary/task-{task_id}",
            output=output,
        )
        completed["exit_code"] = proc.returncode
        completed["route"] = decision
        completed["worktree"] = str(worktree)
        return completed

    def _refuse_live_home(self, account_id: str) -> None:
        with self.plane._conn() as db:
            row = db.execute("SELECT runtime_home FROM accounts WHERE id=?", (account_id,)).fetchone()
        if row is None:
            raise ControlPlaneError("not_found", account_id)
        home = str(Path(row["runtime_home"]))
        if home in LIVE_RUNTIME_HOMES:
            raise ControlPlaneError("live_runtime", "refusing a live Agent Control runtime home")

    def _worktree(self, task_id: int, repo: Path) -> Path:
        dest = self.work_root / f"task-{task_id}"
        if dest.exists():
            return dest
        self.work_root.mkdir(parents=True, exist_ok=True)
        branch = f"canary/task-{task_id}"
        proc = subprocess.run(
            ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(dest), "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ControlPlaneError("worktree", proc.stderr.strip() or "worktree add failed")
        return dest
