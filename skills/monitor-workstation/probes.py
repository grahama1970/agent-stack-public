"""Workstation health probes.

Each probe follows the signature ``def probe_*(autofix: bool) -> ProbeResult``
and is registered in ``ALL_PROBES`` at the bottom of this module.

Inputs: filesystem state, system commands.
Outputs: ProbeResult dataclasses consumed by monitor.py reporting layer.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from loguru import logger

STORAGE_12TB = Path(os.environ.get("EMBRY_STORAGE", "/mnt/storage12tb"))


# ---------------------------------------------------------------------------
# Probe framework
# ---------------------------------------------------------------------------

class ProbeStatus(str, Enum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"
    FIXED = "fixed"


@dataclass
class ProbeResult:
    probe_id: str
    name: str
    status: ProbeStatus
    message: str
    value: float = 0.0
    details: dict = field(default_factory=dict)
    auto_fixable: bool = False
    fix_applied: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _disk_usage_pct(path: str = "/") -> float:
    """Return disk usage percentage for a mount point."""
    usage = shutil.disk_usage(path)
    return (usage.used / usage.total) * 100


def _dir_size_gb(path: Path) -> float:
    """Return directory size in GB using du."""
    if not path.exists():
        return 0.0
    try:
        result = subprocess.run(
            ["du", "-sb", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return int(result.stdout.split()[0]) / (1024**3)
    except (subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    return 0.0


# ---------------------------------------------------------------------------
# Probe implementations
# ---------------------------------------------------------------------------

def probe_nvme_usage(autofix: bool = False) -> ProbeResult:
    """W01: Check NVMe root partition usage."""
    pct = _disk_usage_pct("/")
    if pct > 80:
        status = ProbeStatus.FAIL
        msg = f"CRITICAL: NVMe at {pct:.1f}% — target is 50–60%"
    elif pct > 60:
        status = ProbeStatus.WARN
        msg = f"NVMe at {pct:.1f}% — above 60% maintenance target"
    else:
        status = ProbeStatus.PASS
        msg = f"NVMe at {pct:.1f}% (target: 50–60%)"
    return ProbeResult("W01", "nvme-usage", status, msg, value=round(pct, 1))


# Artifact patterns that should be on 12TB, not NVMe
_ARTIFACT_GLOBS = [
    ("*.ckpt", 0),
    ("*.safetensors", 0),
    ("*.bin", 100),  # min MB
    ("*.gguf", 0),
    ("*.webm", 50),
    ("*.mkv", 50),
    ("*.mp4", 100),
]
_ARTIFACT_DIRS = ["models", "backups", "checkpoints", "training_data"]


def probe_nvme_artifacts(autofix: bool = False) -> ProbeResult:
    """W02: Scan for models/backups/media on NVMe that belong on 12TB."""
    home = Path.home()
    violations: list[str] = []

    # Check workspace for artifact directories
    workspace = home / "workspace"
    if workspace.exists():
        for artifact_dir_name in _ARTIFACT_DIRS:
            try:
                result = subprocess.run(
                    ["find", str(workspace), "-maxdepth", "4",
                     "-type", "d", "-name", artifact_dir_name],
                    capture_output=True, text=True, timeout=30,
                )
                for line in result.stdout.strip().splitlines():
                    p = Path(line)
                    # Skip if it's a symlink to 12TB
                    if p.is_symlink():
                        target = str(p.resolve())
                        if target.startswith(str(STORAGE_12TB)):
                            continue
                    size_gb = _dir_size_gb(p)
                    if size_gb > 0.5:  # Only flag dirs > 500MB
                        violations.append(f"{p} ({size_gb:.1f}GB)")
            except subprocess.TimeoutExpired:
                continue

    # Check for large model files in home
    for glob_pattern, min_mb in _ARTIFACT_GLOBS:
        try:
            result = subprocess.run(
                ["find", str(workspace), "-maxdepth", "5",
                 "-name", glob_pattern, "-type", "f"],
                capture_output=True, text=True, timeout=30,
            )
            for line in result.stdout.strip().splitlines():
                if not line:
                    continue
                p = Path(line)
                try:
                    size_mb = p.stat().st_size / (1024**2)
                    if size_mb >= max(min_mb, 100):
                        violations.append(f"{p} ({size_mb:.0f}MB)")
                except OSError:
                    continue
        except subprocess.TimeoutExpired:
            continue

    if violations:
        msg = f"{len(violations)} artifact(s) on NVMe should be on 12TB"
        return ProbeResult(
            "W02", "nvme-artifacts", ProbeStatus.WARN, msg,
            value=len(violations),
            details={"violations": violations[:20]},
        )
    return ProbeResult("W02", "nvme-artifacts", ProbeStatus.PASS,
                       "No artifact violations found")


def probe_cache_bloat(autofix: bool = False) -> ProbeResult:
    """W03: Check known cache directories for bloat."""
    home = Path.home()
    caches = {
        "uv": (home / ".cache/uv", 20),
        "huggingface": (home / ".cache/huggingface", 30),
        "pip": (home / ".cache/pip", 2),
        "npm": (home / ".cache/npm", 2),
    }

    bloated: list[str] = []
    total_gb = 0.0

    for name, (path, threshold_gb) in caches.items():
        size_gb = _dir_size_gb(path)
        total_gb += size_gb
        if size_gb > threshold_gb:
            bloated.append(f"{name}: {size_gb:.1f}GB (>{threshold_gb}GB)")

    fix_applied = False
    fix_commands: list[dict] = []
    if autofix and bloated:
        logger.info("Auto-fixing cache bloat...")
        uv_cmd = ["uv", "cache", "clean", "--force"] if _disk_usage_pct("/") > 60 else ["uv", "cache", "prune"]
        for cmd, timeout in [
            (uv_cmd, 600),
            (["pip", "cache", "purge"], 120),
            (["npm", "cache", "clean", "--force"], 120),
        ]:
            try:
                env = {**os.environ, "UV_LOCK_TIMEOUT": "300"} if cmd[:2] == ["uv", "cache"] else None
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
                fix_commands.append({
                    "command": cmd, "returncode": proc.returncode,
                    "stderr": proc.stderr[-300:] if proc.returncode else "",
                })
                logger.info("Ran: {}", " ".join(cmd))
            except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
                fix_commands.append({"command": cmd, "error": str(exc)})
        remaining_bloated = []
        for name, (path, threshold_gb) in caches.items():
            if _dir_size_gb(path) > threshold_gb:
                remaining_bloated.append(name)
        fix_applied = not remaining_bloated

    if bloated:
        status = ProbeStatus.FIXED if fix_applied else ProbeStatus.WARN
        msg = f"{len(bloated)} cache(s) over threshold: {', '.join(bloated)}"
    else:
        status = ProbeStatus.PASS
        msg = f"All caches within limits (total: {total_gb:.1f}GB)"

    return ProbeResult(
        "W03", "cache-bloat", status, msg,
        value=round(total_gb, 1), details={"fix_commands": fix_commands},
        auto_fixable=True, fix_applied=fix_applied,
    )


def probe_experiment_growth(autofix: bool = False) -> ProbeResult:
    """W04: Check experiment dirs on NVMe >50GB."""
    experiments = Path.home() / "workspace" / "experiments"
    if not experiments.exists():
        return ProbeResult("W04", "experiment-growth", ProbeStatus.SKIP,
                           "No experiments directory found")

    large: list[str] = []
    total_gb = 0.0

    try:
        for d in sorted(experiments.iterdir()):
            if not d.is_dir():
                continue
            # Skip symlinks pointing to 12TB
            if d.is_symlink():
                target = str(d.resolve())
                if target.startswith(str(STORAGE_12TB)):
                    continue
            size_gb = _dir_size_gb(d)
            total_gb += size_gb
            if size_gb > 50:
                large.append(f"{d.name}: {size_gb:.0f}GB")
    except OSError:
        pass

    if large:
        msg = f"{len(large)} experiment(s) >50GB on NVMe: {', '.join(large)}"
        return ProbeResult("W04", "experiment-growth", ProbeStatus.WARN, msg,
                           value=round(total_gb, 1),
                           details={"large_dirs": large})
    return ProbeResult("W04", "experiment-growth", ProbeStatus.PASS,
                       f"All experiments within limits (total: {total_gb:.0f}GB)",
                       value=round(total_gb, 1))


def probe_arango_backup(autofix: bool = False) -> ProbeResult:
    """W05: Check ArangoDB backup freshness and location."""
    backup_dir = STORAGE_12TB / "backups" / "arangodb"

    # Also check for backups on NVMe (violation)
    nvme_backup = Path.home() / ".local/state/devops-agent/arangodumps"
    if nvme_backup.exists() and any(nvme_backup.iterdir()):
        size_gb = _dir_size_gb(nvme_backup)
        if size_gb > 0.1:
            return ProbeResult(
                "W05", "arango-backup", ProbeStatus.WARN,
                f"ArangoDB backups on NVMe ({size_gb:.1f}GB) — should be on 12TB",
                value=size_gb,
            )

    if not backup_dir.exists():
        return ProbeResult("W05", "arango-backup", ProbeStatus.WARN,
                           f"Backup dir not found: {backup_dir}")

    # Find most recent backup
    backups = sorted(backup_dir.iterdir(), key=lambda p: p.stat().st_mtime,
                     reverse=True) if backup_dir.exists() else []
    if not backups:
        return ProbeResult("W05", "arango-backup", ProbeStatus.WARN,
                           "No backups found on 12TB")

    newest = backups[0]
    age_hours = (time.time() - newest.stat().st_mtime) / 3600

    if age_hours > 48:
        return ProbeResult("W05", "arango-backup", ProbeStatus.WARN,
                           f"Latest backup is {age_hours:.0f}h old (>{48}h threshold)",
                           value=round(age_hours, 1))
    return ProbeResult("W05", "arango-backup", ProbeStatus.PASS,
                       f"Latest backup: {newest.name} ({age_hours:.0f}h ago)",
                       value=round(age_hours, 1))


def probe_docker_reclaimable(autofix: bool = False) -> ProbeResult:
    """W06: Check Docker reclaimable space."""
    try:
        result = subprocess.run(
            ["docker", "system", "df", "--format", "{{.Type}}\t{{.Reclaimable}}"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            return ProbeResult("W06", "docker-reclaimable", ProbeStatus.SKIP,
                               "Docker not available")

        # Parse reclaimable sizes — format like "1.2GB (50%)" per line
        total_gb = 0.0
        for line in result.stdout.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            resource_type, _, reclaimable = line.partition("\t")
            if resource_type == "Local Volumes":
                continue
            # Volumes are persistent data; only count non-volume reclaimable space.
            size_str = reclaimable.split("(")[0].strip()
            if "GB" in size_str:
                total_gb += float(size_str.replace("GB", "").strip())
            elif "MB" in size_str:
                total_gb += float(size_str.replace("MB", "").strip()) / 1024
            elif "kB" in size_str:
                total_gb += float(size_str.replace("kB", "").strip()) / (1024**2)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ProbeResult("W06", "docker-reclaimable", ProbeStatus.SKIP,
                           "Docker command failed")

    if total_gb > 50:
        if autofix:
            try:
                proc = subprocess.run(
                    ["docker", "system", "prune", "-a", "-f"],
                    capture_output=True, text=True, timeout=900,
                )
                if proc.returncode == 0:
                    return ProbeResult(
                        "W06", "docker-reclaimable", ProbeStatus.FIXED,
                        f"Pruned {total_gb:.0f}GB of non-volume Docker data (volumes preserved)",
                        value=round(total_gb, 1), auto_fixable=True, fix_applied=True,
                        details={"volumes_pruned": False},
                    )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
        msg = f"Docker reclaimable: {total_gb:.0f}GB (>50GB) — run `docker system prune -a -f`"
        return ProbeResult("W06", "docker-reclaimable", ProbeStatus.WARN, msg,
                           value=round(total_gb, 1), auto_fixable=True)
    return ProbeResult("W06", "docker-reclaimable", ProbeStatus.PASS,
                       f"Docker reclaimable: {total_gb:.1f}GB",
                       value=round(total_gb, 1))


def _reap_ask_browser_surfaces() -> dict:
    """Close only browser surfaces claimed by Ask's ownership ledgers."""
    ask_run = Path(__file__).resolve().parent.parent / "ask" / "run.sh"
    receipts: dict[str, dict] = {}
    for name, args in {
        "windows": ["reap-windows", "--apply", "--json"],
        "tabs": ["close-stale-tabs", "--apply", "--prune-orphans", "--json"],
    }.items():
        try:
            proc = subprocess.run(
                [str(ask_run), *args], capture_output=True, text=True, timeout=120,
            )
            if proc.returncode != 0:
                receipts[name] = {"ok": False, "error": proc.stderr[-300:] or f"rc={proc.returncode}"}
                continue
            receipts[name] = {"ok": True, **json.loads(proc.stdout)}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
            receipts[name] = {"ok": False, "error": str(exc)}
    return receipts


def probe_zombie_processes(autofix: bool = False) -> ProbeResult:
    """W07: Check stale processes and reap only Ask-owned browser surfaces."""
    browser_cleanup = _reap_ask_browser_surfaces() if autofix else {}
    zombies: list[str] = []
    def _proc_age_hours(pid: str) -> Optional[float]:
        """Return process age in hours using /proc starttime + uptime."""
        try:
            with open(f"/proc/{pid}/stat") as handle:
                stat = handle.read().split()
            start_ticks = int(stat[21])
            with open("/proc/uptime") as handle:
                uptime_seconds = float(handle.read().split()[0])
            clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            age_seconds = uptime_seconds - (start_ticks / clk_tck)
            if age_seconds < 0:
                return None
            return age_seconds / 3600
        except (OSError, ValueError, IndexError, KeyError):
            return None

    # (pattern, max_age_hours, label)
    _PATTERNS = [
        ("claude", 24, "claude"), ("chromium", 24, "chromium"), ("chrome", 24, "chrome"),
        # 2026-08-27 incident: five codex TUIs aged 2-6 days held ~640k
        # inotify watches and exhausted the kernel budget (ENOSPC for every
        # new dev server). Codex sessions older than 48h are stale.
        ("codex", 48, "codex"),
        ("vitest", 1, "build-zombie"), ("jest", 1, "build-zombie"),
        ("webpack", 1, "build-zombie"), ("esbuild", 1, "build-zombie"),
    ]
    for pattern, max_age, tag in _PATTERNS:
        try:
            result = subprocess.run(
                ["pgrep", "-af", pattern], capture_output=True, text=True, timeout=10)
            for line in result.stdout.strip().splitlines():
                parts = line.split(None, 1)
                if len(parts) < 2 or ("monitor" in parts[1] and "workstation" in parts[1]):
                    continue
                pid, cmd = parts
                age_h = _proc_age_hours(pid)
                if age_h is None:
                    continue
                if age_h > max_age:
                    zombies.append(f"PID {pid} ({tag}, {age_h:.0f}h): {cmd[:80]}")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    closed = sum(
        len(receipt.get("closed", []))
        for receipt in browser_cleanup.values()
        if receipt.get("ok")
    )
    cleanup_errors = [name for name, receipt in browser_cleanup.items() if not receipt.get("ok")]
    details = {"zombies": zombies[:10]}
    if browser_cleanup:
        details["ask_browser_cleanup"] = browser_cleanup

    if zombies:
        suffix = f"; closed {closed} Ask-owned browser surface(s)" if closed else ""
        if cleanup_errors:
            suffix += f"; cleanup errors: {', '.join(cleanup_errors)}"
        return ProbeResult(
            "W07", "zombie-processes", ProbeStatus.WARN,
            f"{len(zombies)} stale process(es) above thresholds{suffix}",
            value=len(zombies), details=details, auto_fixable=True,
            fix_applied=closed > 0,
        )
    if cleanup_errors:
        return ProbeResult(
            "W07", "zombie-processes", ProbeStatus.WARN,
            f"Ask browser cleanup failed: {', '.join(cleanup_errors)}",
            details=details, auto_fixable=True,
        )
    if closed:
        return ProbeResult(
            "W07", "zombie-processes", ProbeStatus.FIXED,
            f"Closed {closed} stale Ask-owned browser surface(s)",
            details=details, auto_fixable=True, fix_applied=True,
        )
    return ProbeResult("W07", "zombie-processes", ProbeStatus.PASS,
                       "No stale processes found", details=details,
                       auto_fixable=True)


def probe_drive_health(autofix: bool = False) -> ProbeResult:
    """W08: Check SMART status of drives."""
    drives_checked = 0
    issues: list[str] = []

    for dev in ["/dev/nvme0n1", "/dev/sda"]:
        try:
            result = subprocess.run(
                ["sudo", "smartctl", "-H", dev],
                capture_output=True, text=True, timeout=15,
            )
            drives_checked += 1
            output = result.stdout.lower()
            if "passed" not in output and "ok" not in output:
                issues.append(f"{dev}: SMART check did not report PASSED")
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue

    if drives_checked == 0:
        return ProbeResult("W08", "drive-health", ProbeStatus.SKIP,
                           "smartctl not available or no drives accessible")
    if issues:
        return ProbeResult("W08", "drive-health", ProbeStatus.FAIL,
                           "; ".join(issues), value=len(issues))
    return ProbeResult("W08", "drive-health", ProbeStatus.PASS,
                       f"{drives_checked} drive(s) healthy",
                       value=drives_checked)


# Known disposable /tmp directories created by skills and their evals.
_TMP_SKILL_PREFIXES = (
    "code-review-workspace-",
    "learn_datalake_worker_",
    "extractor_",
    "project-state-uv-env-",
    "battle-evaluator-",
    "project-watchdog-ui-",
    "pw-proof",
    "zero-red-clone",
    "agent-skills-tau-dag-chart-tree",
)
_TMP_MAX_AGE_HOURS = 72


def _live_process_references() -> str:
    """Return current process cwd/command references used to protect live temp dirs."""
    references: list[str] = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            references.append(os.readlink(proc / "cwd"))
            references.append(
                (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")
            )
        except OSError:
            continue
    return "\n".join(references)


def probe_tmp_bloat(autofix: bool = False) -> ProbeResult:
    """W09: Remove recognized, inactive temporary workspaces after 72 hours."""
    tmp = Path("/tmp")
    now = time.time()
    live_references = _live_process_references()
    orphaned: list[Path] = []
    protected: list[str] = []

    try:
        for entry in tmp.iterdir():
            if not entry.is_dir() or not entry.name.startswith(_TMP_SKILL_PREFIXES):
                continue
            try:
                age_hours = (now - entry.stat().st_mtime) / 3600
            except OSError:
                continue
            if age_hours <= _TMP_MAX_AGE_HOURS:
                continue
            if str(entry) in live_references:
                protected.append(entry.name)
                continue
            orphaned.append(entry)
    except OSError:
        return ProbeResult("W09", "tmp-bloat", ProbeStatus.SKIP, "Cannot read /tmp")

    removed: list[str] = []
    if autofix:
        for entry in orphaned:
            logger.info("Removing stale temp dir: {}", entry.name)
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed.append(entry.name)

    remaining = [entry.name for entry in orphaned if entry.exists()]
    details = {
        "stale": [entry.name for entry in orphaned[:20]],
        "protected_active": protected[:20],
        "minimum_age_hours": _TMP_MAX_AGE_HOURS,
        "removed": removed[:20],
    }
    if orphaned:
        fixed = autofix and not remaining
        return ProbeResult(
            "W09", "tmp-bloat", ProbeStatus.FIXED if fixed else ProbeStatus.FAIL,
            f"{len(orphaned)} stale recognized temp dir(s); {len(removed)} removed; "
            f"{len(protected)} active dir(s) protected",
            value=len(orphaned), details=details,
            auto_fixable=True, fix_applied=fixed,
        )
    return ProbeResult(
        "W09", "tmp-bloat", ProbeStatus.PASS,
        f"No recognized temp dirs older than 72h; {len(protected)} active dir(s) protected",
        value=0, details=details,
    )


def probe_inotify_watches(autofix: bool = False) -> ProbeResult:
    """W10: Check inotify watch budget (2026-08-27 incident: exhaustion by
    stale agent sessions made every new file-watching dev server fail with
    ENOSPC while the limit itself looked healthy)."""
    try:
        limit = int(Path("/proc/sys/fs/inotify/max_user_watches").read_text())
    except (OSError, ValueError):
        return ProbeResult("W10", "inotify-watches", ProbeStatus.SKIP,
                           "cannot read inotify limits")

    usage: dict[str, int] = {}
    total = 0
    for proc in Path("/proc").glob("[0-9]*"):
        count = 0
        try:
            for fdinfo in (proc / "fdinfo").iterdir():
                try:
                    count += fdinfo.read_text().count("inotify wd:")
                except OSError:
                    continue
        except OSError:
            continue
        if count:
            total += count
            try:
                cmd = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode()[:60]
            except OSError:
                cmd = "?"
            usage[f"{proc.name} {cmd.strip()}"] = count

    pct = (total / limit) * 100 if limit else 0.0
    top = sorted(usage.items(), key=lambda kv: kv[1], reverse=True)[:5]
    details = {"total": total, "limit": limit,
               "top_consumers": [f"{c} watches: {k}" for k, c in top]}
    msg = f"{total}/{limit} watches in use ({pct:.0f}%)"
    if pct >= 90:
        return ProbeResult("W10", "inotify-watches", ProbeStatus.FAIL, msg,
                           value=round(pct, 1), details=details)
    if pct >= 70:
        return ProbeResult("W10", "inotify-watches", ProbeStatus.WARN, msg,
                           value=round(pct, 1), details=details)
    return ProbeResult("W10", "inotify-watches", ProbeStatus.PASS, msg,
                       value=round(pct, 1), details=details)


def probe_agent_cli_freshness(autofix: bool = False) -> ProbeResult:
    """W11: Keep claude/codex CLIs current (host + scillm container).

    Providers enforce minimum client versions server-side; a stale CLI or a
    stale impersonated version string starts failing with 400s on an arbitrary
    date (observed: claude-cli/2.1.75 rejected for claude-fable-5-1).
    Autofix runs the native updaters.
    """
    def _ver(cmd: list[str]) -> str:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
            m = re.search(r"\d+\.\d+\.\d+", out)
            return m.group(0) if m else ""
        except Exception:
            return ""

    def _npm_latest(pkg: str) -> str:
        try:
            out = subprocess.run(
                ["curl", "-s", "-m", "15", f"https://registry.npmjs.org/{pkg}/latest"],
                capture_output=True, text=True, timeout=20,
            ).stdout
            return json.loads(out).get("version", "")
        except Exception:
            return ""

    def _tup(v: str) -> tuple:
        return tuple(int(p) for p in v.split(".")) if v else (0,)

    scillm_cid = _ver_cid = subprocess.run(
        ["docker", "ps", "-qf", "name=scillm"], capture_output=True, text=True, timeout=15
    ).stdout.split()
    scillm_cid = scillm_cid[0] if scillm_cid else ""

    targets = [
        ("claude(host)", ["claude", "--version"], "@anthropic-ai/claude-code",
         ["claude", "update"]),
        ("codex(host)", ["codex", "--version"], "@openai/codex",
         ["npm", "install", "-g", "@openai/codex@latest"]),
    ]
    if scillm_cid:
        targets.append((
            "claude(scillm)",
            ["docker", "exec", scillm_cid, "claude", "--version"],
            "@anthropic-ai/claude-code",
            ["docker", "exec", scillm_cid, "npm", "install", "-g",
             "@anthropic-ai/claude-code@latest"],
        ))

    details, stale, fixed = {}, [], []
    latest_cache: dict[str, str] = {}
    for name, ver_cmd, pkg, fix_cmd in targets:
        installed = _ver(ver_cmd)
        latest = latest_cache.setdefault(pkg, _npm_latest(pkg))
        details[name] = {"installed": installed, "latest": latest}
        if not installed or not latest:
            continue  # missing CLI or registry unreachable: report only
        if _tup(installed) < _tup(latest):
            stale.append(name)
            if autofix:
                subprocess.run(fix_cmd, capture_output=True, timeout=300)
                after = _ver(ver_cmd)
                details[name]["after_fix"] = after
                if _tup(after) >= _tup(latest):
                    fixed.append(name)

    if all(not d["installed"] for d in details.values()):
        return ProbeResult("W11", "agent-cli-freshness", ProbeStatus.WARN,
                           "could not determine any CLI version", details=details)
    if not stale:
        return ProbeResult("W11", "agent-cli-freshness", ProbeStatus.PASS,
                           "claude/codex CLIs current", details=details,
                           auto_fixable=True)
    if fixed and len(fixed) == len(stale):
        return ProbeResult("W11", "agent-cli-freshness", ProbeStatus.FIXED,
                           f"updated: {', '.join(fixed)}", details=details,
                           auto_fixable=True, fix_applied=True)
    return ProbeResult("W11", "agent-cli-freshness", ProbeStatus.WARN,
                       f"stale: {', '.join(stale)}", value=float(len(stale)),
                       details=details, auto_fixable=True, fix_applied=bool(fixed))


# ---------------------------------------------------------------------------
# Probe registry
# ---------------------------------------------------------------------------

def probe_skill_symlinks(autofix: bool = False) -> ProbeResult:
    """W12: Keep one canonical skills tree; never archive duplicate trees."""
    canonical = Path.home() / "workspace/experiments/agent-skills/skills"
    violations = []
    removed = []
    checked = []
    errors = []
    if canonical.is_symlink() or not canonical.is_dir():
        return ProbeResult("W12", "skill-symlinks", ProbeStatus.FAIL,
                           "Canonical skills directory is missing or is a symlink")

    # Bound discovery to project roots, never descend into copied skill trees.
    roots = {Path.home()}
    try:
        for base in (Path.home() / "workspace", Path.home() / "workspace/experiments"):
            roots.update(p for p in base.iterdir() if p.is_dir() and not p.is_symlink())
        for root in sorted(roots):
            for agent in (".agents", ".claude", ".codex", ".cursor", ".opencode", ".pi", ".pi/agent"):
                parent = root / agent
                if not parent.is_dir() or parent.is_symlink():
                    continue
                link = parent / "skills"
                backups = sorted(parent.glob("skills.pre-symlink-*"))
                if not os.path.lexists(link) and not backups:
                    continue
                checked.append(str(link))
                valid = link.is_symlink() and link.resolve() == canonical
                if not valid:
                    violations.append({"path": str(link), "reason": "expected canonical symlink"})
                for backup in backups:
                    if autofix and valid and backup.is_dir() and not backup.is_symlink():
                        # Never follow links or delete a tree mounted from elsewhere.
                        if backup.stat().st_uid != os.getuid() or os.path.ismount(backup):
                            errors.append(f"Refusing unowned or mounted directory: {backup}")
                        else:
                            try:
                                shutil.rmtree(backup)
                                if not os.path.lexists(backup):
                                    removed.append(str(backup))
                            except OSError as exc:
                                errors.append(f"{backup}: {exc}")
                    if os.path.lexists(backup):
                        violations.append({"path": str(backup), "reason": "duplicate skills retained"})
    except OSError as exc:
        errors.append(str(exc))
    status = (ProbeStatus.FAIL if errors else ProbeStatus.WARN if violations
              else ProbeStatus.FIXED if removed else ProbeStatus.PASS)
    return ProbeResult(
        "W12", "skill-symlinks", status,
        f"{len(violations)} skill-link violation(s); {len(removed)} duplicate tree(s) removed",
        value=len(violations), auto_fixable=True, fix_applied=bool(removed),
        details={"canonical": str(canonical), "checked": checked,
                 "violations": violations, "removed": removed, "errors": errors},
    )


def probe_gpu_container_capability(autofix: bool = False) -> ProbeResult:
    """W13: GPU-attached containers must have working CUDA inside.

    Host driver reloads leave running containers with stale userspace driver
    libs: docker inspect still shows the GPU request, but CUDA init fails
    (observed 2026-09-15: embry-memory BLOCKED_DEPENDENCY for 15 days while
    (healthy)). Capability probe, not liveness.
    """
    def _run(cmd: list[str]) -> tuple[int, str]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            return r.returncode, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, str(exc)

    rc, out = _run(["docker", "ps", "--format", "{{.Names}}"])
    if rc != 0:
        return ProbeResult("W13", "gpu-container-capability", ProbeStatus.SKIP,
                           f"docker unavailable: {out[:120]}")
    gpu_containers = []
    for name in [n for n in out.splitlines() if n.strip()]:
        irc, inspect_out = _run(["docker", "inspect", "--format",
                                 "{{.HostConfig.DeviceRequests}}", name])
        if irc == 0 and "nvidia" in inspect_out and "gpu" in inspect_out:
            gpu_containers.append(name)
    if not gpu_containers:
        return ProbeResult("W13", "gpu-container-capability", ProbeStatus.PASS,
                           "no GPU-attached containers", value=0)
    stale, errors = [], []
    for name in gpu_containers:
        crc, cout = _run(["docker", "exec", name, "python3", "-c",
                          "import torch; raise SystemExit(0 if torch.cuda.is_available() else 3)"])
        if crc == 0:
            continue
        if "No module named torch" in cout or "ModuleNotFoundError" in cout:
            errors.append(f"{name}: torch not installed (cannot probe CUDA)")
        elif crc == 3:
            stale.append(f"{name}: CUDA unavailable inside container (stale driver libs?) — restart container")
        else:
            errors.append(f"{name}: probe failed rc={crc}: {cout[:120]}")
    status = (ProbeStatus.FAIL if stale else
              ProbeStatus.WARN if errors else ProbeStatus.PASS)
    return ProbeResult("W13", "gpu-container-capability", status,
                       f"{len(stale)}/{len(gpu_containers)} GPU container(s) with dead CUDA",
                       value=len(stale), auto_fixable=False,
                       details={"gpu_containers": gpu_containers, "stale": stale,
                                "errors": errors})


_MAIL_AUTH_DOMAINS = [
    # (domain, dkim_selector) — domains whose outbound mail must stay DMARC-aligned.
    ("grahama.co", "google"),
]


def probe_mail_auth(autofix: bool = False) -> ProbeResult:
    """W14: SPF + DMARC + DKIM records must exist for sending domains.

    2026-09-15 incident: grahama.co published DMARC p=quarantine with SPF as
    the only auth leg and no DKIM key. Every SPF-alignment break (forwarding,
    relays) failed DMARC and recipients quarantined per the domain's own
    policy (550 5.7.1). Invisible until a human read a bounce. This probe
    fails when the DKIM leg disappears (DNS wipe, key rotation without
    republish) and warns on missing SPF/DMARC.
    """
    def _dig_txt(name: str) -> str:
        try:
            r = subprocess.run(["dig", "+short", "TXT", name],
                               capture_output=True, text=True, timeout=15)
            return r.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            return ""

    failures, warnings = [], []
    for domain, selector in _MAIL_AUTH_DOMAINS:
        spf = _dig_txt(domain)
        if "v=spf1" not in spf:
            warnings.append(f"{domain}: no SPF record")
        dmarc = _dig_txt(f"_dmarc.{domain}")
        if "v=DMARC1" not in dmarc:
            warnings.append(f"{domain}: no DMARC record")
        dkim = _dig_txt(f"{selector}._domainkey.{domain}")
        if "v=DKIM1" not in dkim:
            enforcing = "p=quarantine" in dmarc or "p=reject" in dmarc
            msg = f"{domain}: DKIM record {selector}._domainkey missing"
            if enforcing:
                failures.append(msg + " while DMARC enforces "
                                + ("quarantine" if "p=quarantine" in dmarc else "reject")
                                + " — outbound mail will bounce on forwarded paths")
            else:
                warnings.append(msg)
    status = (ProbeStatus.FAIL if failures else
              ProbeStatus.WARN if warnings else ProbeStatus.PASS)
    return ProbeResult(
        "W14", "mail-auth", status,
        f"{len(failures)} mail-auth failure(s), {len(warnings)} warning(s) "
        f"across {len(_MAIL_AUTH_DOMAINS)} domain(s)",
        value=len(failures), auto_fixable=False,
        details={"domains": [d for d, _ in _MAIL_AUTH_DOMAINS],
                 "failures": failures, "warnings": warnings},
    )


def _abs_path_tokens(text: str) -> list[str]:
    """Extract absolute-path tokens from a shell-ish line (cron, ExecStart, job command)."""
    tokens = re.findall(r"/[^\s:;|&\"'`<>()]+", text)
    return [t.rstrip(",.") for t in tokens if len(t) > 1]


def probe_runtime_tmpfs(autofix: bool = False) -> ProbeResult:
    """W15: Watch RAM-backed tmpfs mounts (/run/user/<uid>, /dev/shm).

    A leaking skill run dir under /run/user silently consumes RAM (tmpfs):
    2026-09-20 it filled to 26G and nothing was watching. Report-only."""
    uid = os.getuid()
    mounts = [f"/run/user/{uid}", "/dev/shm"]
    per_mount: list[dict] = []
    biggest_pct, biggest_msg = 0.0, ""

    for mnt in mounts:
        try:
            st = os.statvfs(mnt)
        except OSError:
            per_mount.append({"mount": mnt, "error": "statvfs failed"})
            continue
        total = st.f_blocks * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        used_pct = (used / total * 100) if total else 0.0
        size_gb = total / (1024**3)
        entry = {
            "mount": mnt,
            "used_pct": round(used_pct, 1),
            "size_gb": round(size_gb, 1),
        }
        if used_pct > biggest_pct:
            biggest_pct = used_pct
            biggest_msg = f"{mnt} at {used_pct:.1f}% ({size_gb:.0f}GB tmpfs)"
        if mnt == mounts[0]:
            child_sizes: list[tuple[int, str]] = []
            try:
                with os.scandir(mnt) as it:
                    for child in it:
                        try:
                            if child.is_symlink() or not child.is_dir(follow_symlinks=False):
                                continue
                            child_sizes.append((_tree_size_bytes(child.path), child.name))
                        except OSError:
                            continue
            except OSError:
                pass
            child_sizes.sort(reverse=True)
            entry["top_children_gb"] = {
                name: round(size / (1024**3), 2) for size, name in child_sizes[:5]
            }
        per_mount.append(entry)

    errors = [e for e in per_mount if "error" in e]
    if errors and not any("used_pct" in e for e in per_mount):
        return ProbeResult("W15", "runtime-tmpfs", ProbeStatus.SKIP,
                           "no tmpfs mount could be stat'd", details={"mounts": per_mount})

    if biggest_pct >= 80:
        status, verb = ProbeStatus.FAIL, "RAM tmpfs nearly full"
    elif biggest_pct >= 50:
        status, verb = ProbeStatus.WARN, "RAM tmpfs above half"
    else:
        status, verb = ProbeStatus.PASS, "RAM tmpfs mounts within limits"
    msg = f"{verb}: {biggest_msg}" if biggest_msg else verb
    if errors:
        msg += f" ({len(errors)} mount(s) unreadable)"
    return ProbeResult("W15", "runtime-tmpfs", status, msg,
                       value=round(biggest_pct, 1), auto_fixable=False,
                       details={"mounts": per_mount})


def _tree_size_bytes(path: str) -> int:
    """Recursively sum file sizes under path; never follow symlinks, ignore
    permission errors (used for the W15 /run/user top-children breakdown)."""
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        total += _tree_size_bytes(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    except OSError:
        pass
    return total


# ---------------------------------------------------------------------------
# W16: executed-checkout freshness
# ---------------------------------------------------------------------------

_CHECKOUT_WARN_BEHIND = 20
_CHECKOUT_FAIL_BEHIND = 200
_GIT_TIMEOUT_S = 10


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", str(repo), *args],
                           capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
        return r.returncode, r.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return 1, str(exc)


def _repo_for_path(path_str: str) -> Optional[Path]:
    """Map a path (existing or not) to its git toplevel via the nearest
    existing parent directory; None when it is not inside a repository."""
    p = Path(path_str)
    candidate = p if p.is_dir() else p.parent
    for _ in range(16):
        if candidate.exists():
            rc, top = _git(candidate, "rev-parse", "--show-toplevel")
            if rc == 0 and top:
                return Path(top)
            return None
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return None


def _collect_executed_paths() -> dict[str, list[str]]:
    """Absolute $HOME paths executed by cron, the pi scheduler, and user
    systemd units. Returns source tag -> paths (source tag names the job/unit)."""
    home = str(Path.home())
    found: dict[str, list[str]] = {}

    def _add(source: str, text: str) -> None:
        for tok in _abs_path_tokens(text):
            if tok.startswith(home):
                found.setdefault(source, []).append(tok)

    # (a) crontab -l non-comment lines
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True,
                           timeout=_GIT_TIMEOUT_S)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    _add("cron", line)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # (b) ~/.pi/scheduler/jobs.json enabled jobs' command + workdir
    try:
        jobs = json.loads((Path.home() / ".pi/scheduler/jobs.json").read_text())
    except (OSError, json.JSONDecodeError):
        jobs = {}
    if isinstance(jobs, dict):
        for name, job in jobs.items():
            if not isinstance(job, dict) or not job.get("enabled", True):
                continue
            for fld in ("command", "workdir"):
                val = job.get(fld)
                if isinstance(val, str) and val:
                    _add(f"scheduler:{name}", val)

    # (c) systemctl --user cat: ExecStart / WorkingDirectory lines
    try:
        r = subprocess.run(
            ["systemctl", "--user", "list-unit-files", "--type=service", "--no-legend"],
            capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
        units = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    except (subprocess.TimeoutExpired, FileNotFoundError):
        units = []
    for i in range(0, len(units), 50):
        try:
            r = subprocess.run(["systemctl", "--user", "cat", *units[i:i + 50]],
                               capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        if r.returncode != 0 and not r.stdout:
            continue
        unit = "systemd"
        for line in r.stdout.splitlines():
            if line.startswith("# /"):
                unit = Path(line[2:].strip()).name
            elif line.lstrip("-").startswith(("ExecStart=", "WorkingDirectory=")):
                _add(f"systemd:{unit}", line.split("=", 1)[1])
    return found


def probe_checkout_freshness(autofix: bool = False) -> ProbeResult:
    """W16: Detect executed checkouts that drifted far behind their remote
    (cron/scheduler/systemd run hundreds-of-commits-stale copies for days) or
    that sit off main/master. Read-only: compares against the cached remote
    ref without fetching."""
    found = _collect_executed_paths()
    repo_sources: dict[Path, set[str]] = {}
    for source, paths in found.items():
        for p in paths:
            repo = _repo_for_path(p)
            if repo is not None:
                repo_sources.setdefault(repo, set()).add(source)

    repos: list[dict] = []
    errors: list[str] = []
    for repo, sources in sorted(repo_sources.items()):
        entry: dict = {"repo": str(repo), "used_by": sorted(sources)}
        rc, branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if rc != 0:
            errors.append(f"{repo}: branch lookup failed: {branch[:120]}")
            continue
        entry["branch"] = branch

        rc, upstream = _git(repo, "rev-parse", "--abbrev-ref",
                            "--symbolic-full-name", "@{u}")
        if rc == 0 and upstream:
            ref = upstream
        elif _git(repo, "rev-parse", "--verify", "--quiet", "origin/main")[0] == 0:
            ref = "origin/main"
        elif _git(repo, "rev-parse", "--verify", "--quiet", "origin/master")[0] == 0:
            ref = "origin/master"
        else:
            errors.append(f"{repo}: no upstream and no origin/main|origin/master")
            continue
        entry["compared_ref"] = ref

        rc, counts = _git(repo, "rev-list", "--left-right", "--count",
                          f"HEAD...{ref}")
        if rc != 0:
            errors.append(f"{repo}: rev-list failed: {counts[:120]}")
            continue
        ahead_s, _, behind_s = counts.partition("\t")
        try:
            entry["ahead"] = int(ahead_s)
            entry["behind"] = int(behind_s)
        except ValueError:
            errors.append(f"{repo}: unparseable rev-list output: {counts[:60]}")
            continue
        rc, ts = _git(repo, "log", "-1", "--format=%ct", ref)
        if rc == 0 and ts.isdigit():
            entry["ref_age_days"] = round((time.time() - int(ts)) / 86400, 1)

        hard = entry["behind"] > _CHECKOUT_FAIL_BEHIND
        stale = hard or entry["behind"] > _CHECKOUT_WARN_BEHIND \
            or branch not in ("main", "master")
        entry["verdict"] = "fail" if hard else "warn" if stale else "ok"
        repos.append(entry)

    flagged = [e for e in repos if e.get("verdict") != "ok"]
    failing = [e for e in repos if e.get("verdict") == "fail"]
    worst = max((e.get("behind", 0) for e in repos), default=0)
    worst_repo = next((e["repo"] for e in repos if e.get("behind") == worst), "")
    if any(e.get("verdict") == "fail" for e in repos):
        status = ProbeStatus.FAIL
        msg = (f"{len(failing)} executed checkout(s) >{_CHECKOUT_FAIL_BEHIND} behind, "
               f"{len(flagged) - len(failing)} more stale/off-main (of {len(repos)})")
    elif flagged:
        status = ProbeStatus.WARN
        msg = (f"{len(flagged)}/{len(repos)} executed checkout(s) stale "
               f">{_CHECKOUT_WARN_BEHIND} behind or off main/master")
    elif repos:
        status = ProbeStatus.PASS
        msg = f"{len(repos)} executed checkout(s) fresh"
    else:
        status = ProbeStatus.WARN
        msg = "no executed $HOME paths could be mapped to a git checkout"
    if worst and worst > _CHECKOUT_WARN_BEHIND:
        msg += f"; worst: {worst_repo} behind by {worst}"
    return ProbeResult("W16", "checkout-freshness", status, msg,
                       value=float(len(flagged)), auto_fixable=False,
                       details={"repos": repos, "errors": errors,
                                "sources": {s: len(ps) for s, ps in found.items()}})


# ---------------------------------------------------------------------------
# W17: pi scheduler job health
# ---------------------------------------------------------------------------

_LEAKED_EVAL_PREFIX = "zz_eval_"
_JOB_STALE_HOURS = 26


def _cron_daily_or_more(cron: str) -> bool:
    """True when the day-of-month, month, and day-of-week fields are all '*'
    (runs at least daily). Simple heuristic per spec; invalid cron -> False."""
    fields = cron.split()
    if len(fields) < 5:
        return False
    return fields[2] == "*" and fields[3] == "*" and fields[4] == "*"


def probe_scheduler_job_health(
    autofix: bool = False, jobs_path: Optional[Path] = None,
) -> ProbeResult:
    """W17: Flag enabled pi-scheduler jobs with dead paths, failed/timeout
    last runs, or never-run staleness; count leaked zz_eval_ jobs. Report-only
    (jobs_path parameter exists for adversarial evals)."""
    path = Path(jobs_path) if jobs_path is not None \
        else Path.home() / ".pi/scheduler/jobs.json"
    if not path.exists():
        return ProbeResult("W17", "scheduler-job-health", ProbeStatus.SKIP,
                           f"scheduler jobs file not found: {path}")
    try:
        jobs = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return ProbeResult("W17", "scheduler-job-health", ProbeStatus.WARN,
                           f"scheduler jobs file unreadable: {exc}")
    if not isinstance(jobs, dict):
        return ProbeResult("W17", "scheduler-job-health", ProbeStatus.WARN,
                           "scheduler jobs file has unexpected shape")

    now = time.time()
    cutoff = now - _JOB_STALE_HOURS * 3600
    dead: list[str] = []
    failed: list[str] = []
    never_run: list[str] = []
    leaked: list[str] = []
    enabled = 0

    for name, job in jobs.items():
        if not isinstance(job, dict) or not job.get("enabled", True):
            continue
        enabled += 1
        if name.startswith(_LEAKED_EVAL_PREFIX):
            leaked.append(name)
            continue

        dead_paths: list[str] = []
        for fld in ("command", "workdir"):
            val = job.get(fld)
            if isinstance(val, str):
                for tok in _abs_path_tokens(val):
                    if tok.startswith("/home/") and not os.path.exists(tok) \
                            and tok not in dead_paths:
                        dead_paths.append(tok)
        if dead_paths:
            dead.append(f"{name}: missing {' '.join(dead_paths)}")

        if job.get("last_status") in ("failed", "timeout"):
            failed.append(f"{name}: last_status={job.get('last_status')}")

        created = job.get("created_at")
        last_run = job.get("last_run")
        never = (
            isinstance(created, (int, float)) and created < cutoff
            and not isinstance(last_run, (int, float))
        ) or (
            isinstance(last_run, (int, float)) and last_run < cutoff
            and _cron_daily_or_more(str(job.get("cron", "")))
        )
        if never:
            never_run.append(
                f"{name}: last_run={last_run}, cron={job.get('cron', '?')}"
            )

    problems = len(dead) + len(failed)
    cautions = len(never_run) + len(leaked)
    if dead or failed:
        status = ProbeStatus.FAIL
        msg = (f"{len(dead)} dead path(s), {len(failed)} failed job(s)"
               + (f"; {cautions} caution(s)" if cautions else ""))
    elif never_run or leaked:
        status = ProbeStatus.WARN
        msg = (f"{len(never_run)} never-run job(s), "
               f"{len(leaked)} leaked zz_eval_ job(s)")
    else:
        status = ProbeStatus.PASS
        msg = f"{enabled} enabled job(s) healthy"
    return ProbeResult("W17", "scheduler-job-health", status, msg,
                       value=float(problems + cautions), auto_fixable=False,
                       details={"jobs_file": str(path), "enabled": enabled,
                                "dead_paths": dead[:50], "failed": failed[:50],
                                "never_run": never_run[:50],
                                "leaked_eval_jobs": {"count": len(leaked),
                                                     "names": leaked[:50]}})


ALL_PROBES = [
    ("W01", "nvme-usage", probe_nvme_usage),
    ("W02", "nvme-artifacts", probe_nvme_artifacts),
    ("W03", "cache-bloat", probe_cache_bloat),
    ("W04", "experiment-growth", probe_experiment_growth),
    ("W05", "arango-backup", probe_arango_backup),
    ("W06", "docker-reclaimable", probe_docker_reclaimable),
    ("W07", "zombie-processes", probe_zombie_processes),
    ("W08", "drive-health", probe_drive_health),
    ("W09", "tmp-bloat", probe_tmp_bloat),
    ("W10", "inotify-watches", probe_inotify_watches),
    ("W11", "agent-cli-freshness", probe_agent_cli_freshness),
    ("W12", "skill-symlinks", probe_skill_symlinks),
    ("W13", "gpu-container-capability", probe_gpu_container_capability),
    ("W14", "mail-auth", probe_mail_auth),
    ("W15", "runtime-tmpfs", probe_runtime_tmpfs),
    ("W16", "checkout-freshness", probe_checkout_freshness),
    ("W17", "scheduler-job-health", probe_scheduler_job_health),
]
