#!/usr/bin/env python3
"""
QC battery for one task (RL_ENV_FACTORY_PROMPT Phase 2/3):

  1. fresh container (via StackhouseEnv.reset, incl. nonce injection)
  2. no-op baseline          -> verifier must FAIL on pristine state
  3. random-action baseline  -> 10 valid-API junk actions, verifier must FAIL
  4. gold.py                 -> verifier must PASS
  5. verifier rerun          -> PASS again (deterministic/idempotent)
  6. lazy-hardcode baseline  -> for ops_reports tasks: plausible static row(s)
     with a stale batch_code; verifier must FAIL

Writes qc_results/<task_id>.json and prints a one-line summary.

Usage:
    python tools/qc_task.py <task_id> [--skip-baselines] [--skip-gold]
"""
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from env import StackhouseEnv  # noqa: E402

RESULTS_DIR = os.path.join(ROOT, "qc_results")


def run_script(script, stackhouse_url, timeout=180):
    env_vars = dict(os.environ, STACKHOUSE_API_URL=stackhouse_url)
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, script], env=env_vars, capture_output=True, text=True, timeout=timeout
    )
    return {
        "exit": proc.returncode,
        "stdout_tail": (proc.stdout or "").strip().splitlines()[-3:],
        "stderr_tail": (proc.stderr or "").strip().splitlines()[-3:],
        "seconds": round(time.time() - t0, 1),
    }


def random_actions(env, n=10):
    """Valid-API junk traffic: logins, reads, and pushes to a scratch collection."""
    actions = [
        ("GET", "/v1/tables"),
        ("GET", "/v1/query/matters?limit=5"),
        ("GET", "/v1/query/clients?limit=5"),
        ("POST", "/v1/push/junk_probe", {"junk": True, "n": 1}),
        ("GET", "/v1/query/time_entries?limit=3"),
        ("GET", "/v1/query/invoices?limit=3"),
        ("POST", "/v1/push/junk_probe", {"junk": "two"}),
        ("GET", "/v1/query/grant_opportunities?limit=3"),
        ("GET", "/v1/query/ops_meta?limit=2"),
        ("GET", "/health"),
    ]
    for method, endpoint, *payload in actions[:n]:
        env._http(method, endpoint, payload[0] if payload else None,
                  token=env._login("verifier"))


def lazy_hardcode(env, task_dir, task):
    """Plausible-but-underived writes: report rows with a stale batch code and
    made-up numbers; for non-report tasks, a single junk status flip attempt."""
    token = env._login("verifier")
    spec = task.get("success_criteria", {}).get("summary", "")
    if "ops_reports" in spec or "ops_reports" in json.dumps(task.get("blast_radius", [])):
        env._http("POST", "/v1/push/ops_reports", {
            "report": "hardcoded", "batch_code": "EP-DEADBEEF", "total": 10, "value": 42,
        }, token=token)
    else:
        # generic lazy attempt: set a plausible flag on the first row of the
        # first blast collection (values not derived from data)
        blast = task.get("blast_radius", [])
        if blast:
            coll = blast[0]
            status, body = env._http("GET", f"/v1/query/{coll}?limit=1", token=token)
            rows = body.get("data", []) if status == 200 else []
            if rows:
                env._http("POST", f"/v1/update/{coll}/{rows[0]['id']}",
                          {"status": "overdue", "flag": "hardcoded"}, token=token)


def main():
    task_id = sys.argv[1]
    skip_baselines = "--skip-baselines" in sys.argv
    skip_gold = "--skip-gold" in sys.argv

    task_dir = os.path.join(ROOT, "tasks", task_id)
    with open(os.path.join(task_dir, "task.json"), encoding="utf-8") as f:
        task = json.load(f)
    verifier = os.path.join(task_dir, "verifier.py")
    gold = os.path.join(task_dir, "gold.py")
    gold_alt = os.path.join(task_dir, "gold_alt.py")

    result = {"task_id": task_id, "runs": {}}

    # --- Container 1: baselines on progressively dirtier state (all must FAIL)
    if not skip_baselines:
        env = StackhouseEnv(app_slug="law_firm_software", task_id=task_id)
        try:
            t0 = time.time()
            obs, info = env.reset()
            result["reset_seconds"] = round(time.time() - t0, 1)
            result["nonce"] = info.get("nonce")
            url = f"http://127.0.0.1:{env.host_baas_port}"
            result["runs"]["noop"] = run_script(verifier, url)
            random_actions(env)
            result["runs"]["random"] = run_script(verifier, url)
            lazy_hardcode(env, task_dir, task)
            result["runs"]["hardcode"] = run_script(verifier, url)
        finally:
            env.close()

    # --- Container 2: fresh state -> gold -> verifier PASS (x2 for idempotency)
    # v2: idempotency is required of EVERY task (hardmode), so gold always
    # runs twice and the verifier must PASS after each run with identical
    # state; the measured gold step count is parsed from gold's output and
    # asserted against the task's declared budget.
    if not skip_gold:
        env = StackhouseEnv(app_slug="law_firm_software", task_id=task_id)
        try:
            env.reset()
            url = f"http://127.0.0.1:{env.host_baas_port}"
            result["runs"]["gold"] = run_script(gold, url)
            result["runs"]["verify_after_gold"] = run_script(verifier, url)
            result["runs"]["verify_idempotent"] = run_script(verifier, url)
            result["runs"]["gold_second_run"] = run_script(gold, url)
            result["runs"]["verify_after_second_gold"] = run_script(verifier, url)
            # Measured gold step count -> the budget assertion the QC harness
            # never enforced before (FACTORY Phase 5 known gap).
            import re
            tail = "\n".join(result["runs"]["gold"].get("stdout_tail", []))
            m = re.search(r"gold done in (\d+) API calls", tail)
            if m:
                result["measured_gold_steps"] = int(m.group(1))
        finally:
            env.close()

    # --- Container 3: independent second gold solution, must also PASS
    if not skip_gold and os.path.exists(gold_alt):
        env = StackhouseEnv(app_slug="law_firm_software", task_id=task_id)
        try:
            env.reset()
            url = f"http://127.0.0.1:{env.host_baas_port}"
            result["runs"]["gold_alt"] = run_script(gold_alt, url)
            result["runs"]["verify_after_gold_alt"] = run_script(verifier, url)
        finally:
            env.close()

    def ok(name, expect):
        run = result["runs"].get(name)
        if run is None:
            return None
        return (run["exit"] == 0) == expect

    result["verdicts"] = {
        "noop_fail": ok("noop", False),
        "random_fail": ok("random", False),
        "hardcode_fail": ok("hardcode", False),
        "gold_pass": ok("verify_after_gold", True),
        "idempotent_pass": ok("verify_idempotent", True),
        "gold_second_run_pass": ok("verify_after_second_gold", True),
        "gold_alt_pass": ok("verify_after_gold_alt", True),
    }
    # Budget assertion: measured gold steps must fit the declared budget. If
    # par_steps was left null by the codegen session, backfill it from the
    # measurement and derive max_steps = ceil(1.5x) — but never let a task
    # ship with a budget its own gold cannot finish inside.
    measured = result.get("measured_gold_steps")
    if measured:
        par = task.get("par_steps")
        max_steps = task.get("max_steps")
        if par is None:
            task["par_steps"] = measured
            task["max_steps"] = int(measured * 1.5) + 1
            task.pop("_par_steps_note", None)
            with open(os.path.join(task_dir, "task.json"), "w", encoding="utf-8") as f:
                json.dump(task, f, indent=2)
            result["par_steps_set_from_measurement"] = measured
            result["max_steps_set"] = task["max_steps"]
            par, max_steps = measured, task["max_steps"]
        result["verdicts"]["budget_fits"] = (measured <= (max_steps or 0)) if max_steps else None
    result["pass"] = all(v for v in result["verdicts"].values() if v is not None)

    os.makedirs(RESULTS_DIR, exist_ok=True)
    with open(os.path.join(RESULTS_DIR, f"{task_id}.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"{task_id}: {'PASS' if result.get('pass') else 'FAIL'} {result.get('verdicts')}")
    return 0 if result.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main())
