#!/usr/bin/env python3
"""Reusable Phase 4 hacker-fixer attacks, parametrized by task_id, for any
task in this suite. Runs against a FRESH container per attack (mirrors
qc_task.py's container-per-check-group pattern).

Usage:
    python tools/phase4_attack.py <task_id> [canary_collection]

Attacks:
  1. Partial-completion: kill gold.py partway through its measured runtime,
     verifier must FAIL.
  2. Canary/scope violation: mutate one row of a collection outside
     blast_radius after a correct gold run, verifier must FAIL.
  3. Retry/flake: run the verifier 5x on identical post-gold state, must be
     5/5 identical PASS.

Exits 0 iff all three attacks behave correctly (FAIL, FAIL, 5x PASS).
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


def run(script, url, timeout=180):
    return subprocess.run(
        [sys.executable, script],
        env=dict(os.environ, STACKHOUSE_API_URL=url),
        capture_output=True, text=True, timeout=timeout,
    )


def main():
    task_id = sys.argv[1]
    task_dir = os.path.join(ROOT, "tasks", task_id)
    gold = os.path.join(task_dir, "gold.py")
    verifier = os.path.join(task_dir, "verifier.py")
    with open(os.path.join(task_dir, "task.json"), encoding="utf-8") as f:
        task = json.load(f)
    blast = set(task.get("blast_radius", []))
    # pick a canary collection: any commonly-seeded one NOT in this task's blast radius
    candidates = ["matters", "clients", "contacts", "deadlines", "tasks", "time_entries"]
    canary_coll = sys.argv[2] if len(sys.argv) > 2 else next((c for c in candidates if c not in blast), None)
    if canary_coll is None:
        print("SKIP canary attack: no safe canary collection found outside blast_radius")

    ok = True

    # ---- Attack 1: partial-completion -------------------------------------
    env = StackhouseEnv(app_slug="law_firm_software", task_id=task_id)
    try:
        env.reset()
        url = f"http://127.0.0.1:{env.host_baas_port}"
        t0 = time.time()
        proc = subprocess.Popen([sys.executable, gold], env=dict(os.environ, STACKHOUSE_API_URL=url),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(0.6)  # kill early regardless of the task's full runtime
        proc.terminate()
        proc.wait(timeout=15)
        r = run(verifier, url)
        passed = r.returncode != 0
        print(f"[1] partial-completion: {'FAIL (correct)' if passed else 'PASS (WRONG -- verifier accepted a partial run)'}: {r.stdout.strip()}")
        ok = ok and passed
    finally:
        env.close()

    # ---- Attack 2: canary/scope violation ----------------------------------
    if canary_coll:
        env = StackhouseEnv(app_slug="law_firm_software", task_id=task_id)
        try:
            env.reset()
            url = f"http://127.0.0.1:{env.host_baas_port}"
            r = run(gold, url, timeout=300)
            if r.returncode != 0:
                print("[2] canary: SKIPPED (gold.py failed):", r.stdout[-500:], r.stderr[-500:])
                ok = False
            else:
                r = run(verifier, url)
                if r.returncode != 0:
                    print("[2] canary: SKIPPED (post-gold verify didn't PASS):", r.stdout.strip())
                    ok = False
                else:
                    import urllib.request
                    token_req = urllib.request.Request(
                        url + "/v1/auth/login",
                        data=json.dumps({"email": task["verifier_email"], "password": task["verifier_password"]}).encode(),
                        headers={"Content-Type": "application/json"}, method="POST",
                    )
                    tok = json.loads(urllib.request.urlopen(token_req, timeout=30).read())["data"]["access_token"]
                    q_req = urllib.request.Request(url + f"/v1/query/{canary_coll}?limit=1", headers={"Authorization": f"Bearer {tok}"})
                    row = json.loads(urllib.request.urlopen(q_req, timeout=30).read())["data"][0]
                    field = "status" if "status" in row else next(iter(k for k in row if k not in ("id", "created_at", "updated_at")))
                    upd_req = urllib.request.Request(
                        url + f"/v1/update/{canary_coll}/{row['id']}",
                        data=json.dumps({field: "REDTEAM_CANARY_PROBE"}).encode(),
                        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"}, method="POST",
                    )
                    urllib.request.urlopen(upd_req, timeout=30)
                    r = run(verifier, url)
                    passed = r.returncode != 0
                    print(f"[2] canary/scope violation ({canary_coll}): {'FAIL (correct)' if passed else 'PASS (WRONG -- canary not enforced)'}: {r.stdout.strip()}")
                    ok = ok and passed
        finally:
            env.close()

    # ---- Attack 3: retry/flake ---------------------------------------------
    env = StackhouseEnv(app_slug="law_firm_software", task_id=task_id)
    try:
        env.reset()
        url = f"http://127.0.0.1:{env.host_baas_port}"
        r = run(gold, url, timeout=300)
        results = []
        for _ in range(5):
            r = run(verifier, url)
            results.append(r.returncode == 0)
        all_pass = all(results)
        print(f"[3] retry/flake x5: {'5/5 identical PASS (correct)' if all_pass else f'INCONSISTENT {results} (WRONG)'}")
        ok = ok and all_pass
    finally:
        env.close()

    print(f"\n{task_id}: PHASE4 {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
