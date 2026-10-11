#!/usr/bin/env python3
"""
Tiny client for the Big Interview `resumeai` MCP (HYBRID_PIPELINE_PLAN.md §4A.1).

Two halves in one file:

  • call(cmd, **args)  — used by pipeline.py. Runs THIS file inside the MCP's own
    environment:
        uv --directory <agents.bi_mcp_dir> run python bi_scan.py <cmd> --json-args '{...}'
    and returns the MCP's JSON envelope ({ok, data} / {ok: false, error: {code,
    message, hint}}). No new dependency in this repo.

  • main()             — runs in the MCP env: starts `resumeai-mcp` over stdio (MCP
    Python SDK), calls ONE tool, prints the envelope as one JSON line. Every call
    goes through the MCP's public tools, so its safety rules (one tool at a time,
    2–5 s human delays, daily op budget, backup-before-delete) all still apply.

Errors are passed through with the MCP's hint and are never retried here.
Commands: auth, start, status, feedback, list, delete.
"""
import os
import sys
import json
import shutil
import asyncio
import argparse
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent

TOOLS = {"auth": "auth_status", "start": "start_scan", "status": "get_scan_status",
         "feedback": "get_scan_feedback", "list": "list_scans", "delete": "delete_scan"}
# Browser launch + 2–5 s human delays per UI action; a start uploads a file.
TIMEOUT_S = {"start": 300, "delete": 240}
DEFAULT_TIMEOUT_S = 180
# The child gives up this much earlier on its own, so the MCP SDK can terminate the
# server it spawned (in its own session) before the outer hard kill ever fires.
INNER_MARGIN_S = 30


def _err(code: str, message: str, hint: str = "") -> dict:
    return {"ok": False, "error": {"code": code, "message": message, "hint": hint}}


def _last_json(text: str):
    for line in reversed((text or "").strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ── client half (pipeline env) ───────────────────────────────────────────────

def call(cmd: str, **args) -> dict:
    """Run one MCP tool via the MCP's uv env. Never raises; never retries."""
    if cmd not in TOOLS:
        return _err("invalid_input", f"unknown bi_scan command {cmd!r}",
                    f"use one of: {', '.join(TOOLS)}")
    import resume_agents
    mcp_dir = Path(os.path.expanduser(str(resume_agents.agent_cfg("bi_mcp_dir"))))
    uv = shutil.which("uv")
    if not mcp_dir.is_dir() or not uv:
        return _err("unavailable", f"Big Interview MCP not found at {mcp_dir}" if uv
                    else "uv not installed",
                    "Set agents.bi_mcp_dir in config.json to the Biginterview-MCP checkout.")
    timeout = TIMEOUT_S.get(cmd, DEFAULT_TIMEOUT_S)
    argv = [uv, "--directory", str(mcp_dir), "run", "python", str(HERE / "bi_scan.py"),
            cmd, "--json-args", json.dumps({k: v for k, v in args.items() if v is not None}),
            "--timeout", str(timeout - INNER_MARGIN_S)]
    # Own process group so a hung client is killed as a whole on the outer timeout.
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
    except OSError as e:
        return _err("unavailable", f"could not launch bi_scan via uv: {e}",
                    "Check that uv and the Biginterview-MCP env are installed.")
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        import claude_runner
        claude_runner._kill_group(proc)
        try:
            proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        return _err("timeout", f"bi_scan {cmd} exceeded {timeout}s and was killed",
                    "Not retried. A start may still have reached the site — check My Scans "
                    "(list) before scanning again.")
    env = _last_json(out)
    if env is None or "ok" not in env:
        tail = " ".join((err or out or "").strip().splitlines()[-3:])[:300]
        return _err("internal_error", f"bi_scan {cmd} returned no JSON envelope (rc={proc.returncode})",
                    tail or "Check the MCP install (uv sync in the Biginterview-MCP dir).")
    return env


# ── server half (MCP env) ────────────────────────────────────────────────────

def _unwrap(result) -> dict:
    """CallToolResult → the tool's envelope dict."""
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc["result"] if "ok" not in sc and isinstance(sc.get("result"), dict) else sc
    for c in getattr(result, "content", None) or []:
        obj = _last_json(getattr(c, "text", "") or "")
        if obj is not None:
            return obj
    return _err("internal_error", "MCP returned no parseable content",
                "Stop and report; see the MCP server log.")


async def _run_tool(tool: str, args: dict) -> dict:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=sys.executable, args=["-m", "resumeai_mcp.server"],
                                   env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return _unwrap(await session.call_tool(tool, args))


def main(argv=None, runner=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("cmd", choices=sorted(TOOLS))
    ap.add_argument("--json-args", default="{}")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S - INNER_MARGIN_S)
    a = ap.parse_args(argv)
    try:
        args = json.loads(a.json_args)
        if not isinstance(args, dict):
            raise ValueError("json-args must be an object")
    except ValueError as e:
        print(json.dumps(_err("invalid_input", f"bad --json-args: {e}")))
        return 2
    try:
        if runner is None:
            # wait_for cancels _run_tool on timeout; its context managers then close the
            # session and the SDK terminates the MCP server (and its browser).
            env = asyncio.run(asyncio.wait_for(_run_tool(TOOLS[a.cmd], args), a.timeout))
        else:
            env = runner(TOOLS[a.cmd], args)
    except (TimeoutError, asyncio.TimeoutError):  # distinct classes before Python 3.11
        env = _err("timeout", f"{a.cmd} exceeded {a.timeout:.0f}s; MCP server shut down",
                   "Not retried. A start may still have reached the site — check My Scans "
                   "(list) before scanning again.")
    except Exception as e:
        env = _err("internal_error", f"{type(e).__name__}: {e}",
                   "Is this running inside the Biginterview-MCP env (uv run)? Stop and report.")
    print(json.dumps(env))
    return 0


if __name__ == "__main__":
    # Running inside the MCP env: don't let this repo's modules shadow the MCP's deps.
    sys.path = [p for p in sys.path if Path(p or ".").resolve() != HERE]
    sys.exit(main())
