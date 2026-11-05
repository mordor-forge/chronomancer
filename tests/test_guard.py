from change_windows import guard
from change_windows.rules import build_ruleset

RS = build_ruleset()
KAPPLY = {"command": "kubectl apply -f deploy.yaml --token=abc123"}


class Exploding:
    def __getattr__(self, name):
        raise RuntimeError("store must not be touched")


def test_unmatched_call_never_touches_store():
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "enforce", store=Exploding())
    assert hook(tool_name="terminal", args={"command": "ls -la"}) is None
    assert hook(tool_name="read_file", args={"path": "/etc/hosts"}) is None


def test_block_outside_window_names_next_window(store):
    w, _ = store.propose(name="Argo upgrade", scope="k8s-prod", start_ts=5000, end_ts=9000, reason="r",
                         proposed_by="agent:t", now=0)
    store.decide(w.id, approve=True, by="owner:1", now=1)
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "enforce", store=store, clock=lambda: 1000)
    out = hook(tool_name="terminal", args=KAPPLY, task_id="t1", session_id="s1")
    assert out["action"] == "block"
    assert w.id in out["message"] and "window_propose" in out["message"]
    row = store.audit_tail(1)[0]
    assert row["verdict"] == "blocked" and "abc123" not in row["preview"] and row["task_id"] == "t1"


def test_allow_inside_window_and_audit(store):
    w, _ = store.propose(name="w", scope="k8s-prod", start_ts=0, end_ts=9000, reason="r", proposed_by="a", now=0)
    store.decide(w.id, approve=True, by="owner:1", now=1)
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "enforce", store=store, clock=lambda: 100)
    assert hook(tool_name="terminal", args=KAPPLY) is None
    assert store.audit_tail(1)[0]["verdict"] == "allowed"


def test_every_matched_scope_needs_a_window(store):
    w, _ = store.propose(name="w", scope="k8s-prod", start_ts=0, end_ts=9000, reason="r", proposed_by="a", now=0)
    store.decide(w.id, approve=True, by="owner:1", now=1)
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "enforce", store=store, clock=lambda: 100)
    out = hook(tool_name="terminal", args={"command": "kubectl apply -f x && tofu apply"})
    assert out["action"] == "block" and "infra" in out["message"]


def test_warn_mode_allows_and_records(store):
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "warn", store=store, clock=lambda: 100)
    assert hook(tool_name="terminal", args=KAPPLY) is None
    assert store.audit_tail(1)[0]["verdict"] == "warned"


def test_store_failure_fails_closed():
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "enforce", store=Exploding())
    out = hook(tool_name="terminal", args=KAPPLY)
    assert out["action"] == "block" and "store failed" in out["message"]


def test_self_protect_blocks_even_inside_any_window(store):
    w, _ = store.propose(name="w", scope="k8s-prod", start_ts=0, end_ts=9000, reason="r", proposed_by="a", now=0)
    store.decide(w.id, approve=True, by="owner:1", now=1)
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "enforce", store=store, clock=lambda: 100)
    out = hook(tool_name="terminal", args={"command": "hermes change-windows approve W1"})
    assert out["action"] == "block" and "no window can open" in out["message"]


def test_off_mode_allows():
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "off", store=Exploding())
    assert hook(tool_name="terminal", args=KAPPLY) is None



def test_self_protect_blocks_in_warn_mode(store):
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "warn", store=store, clock=lambda: 100)
    out = hook(tool_name="terminal", args={"command": "hermes plugins disable change-windows"})
    assert out["action"] == "block"


def test_redaction_cases():
    synthetic = "s3cr3t-synthetic value"
    cases = [
        'kubectl apply --password="prefix\\" s3cr3t-synthetic value" -f x',
        'echo "{\\"password\\":\\"s3cr3t-synthetic value\\"}" | kubectl apply -f -',
        "helm upgrade app ./chart --set key=s3cr3t-synthetic",
        "mysql -p s3cr3t-synthetic -e 'select 1'",
        f'kubectl create secret generic x --from-literal=password="{synthetic}"',
        "curl -H 'Authorization: Bearer s3cr3t-synthetic-token' https://x",  # gitleaks:allow (synthetic)
        f'echo {{"password": "{synthetic}"}} | kubectl apply -f -',
        f"helm upgrade app ./c --set db.password='{synthetic}'",
        "kubectl apply -f x --token ghp_" + "A" * 36,
    ]
    for c in cases:
        out = guard.redact(c)
        assert "s3cr3t" not in out and "synthetic value" not in out and "AAAAAAAA" not in out, out


def test_redaction_is_bounded_in_time():
    import time as _t
    start = _t.monotonic()
    guard.redact("kubectl apply " + "a" * 200_000)
    assert _t.monotonic() - start < 0.5


def test_default_audit_stores_no_command_text(store):
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "enforce", store=store, clock=lambda: 100)
    hook(tool_name="terminal", args={"command": "TOKEN=s3cr3t kubectl apply -f x"})
    row = store.audit_tail(1)[0]
    assert "s3cr3t" not in row["preview"] and "kubectl" not in row["preview"] and row["rule_ids"]


def test_opt_in_command_preview_is_masked(store):
    hook = guard.make_hook(get_rules=lambda: RS, get_mode=lambda: "enforce", store=store, clock=lambda: 100,
                           command_preview=lambda: True)
    hook(tool_name="terminal", args={"command": "kubectl apply -f x --token=s3cr3t"})
    row = store.audit_tail(1)[0]
    assert "kubectl apply" in row["preview"] and "s3cr3t" not in row["preview"]


def test_preview_never_stores_code_or_non_terminal_values():
    assert "subprocess" not in guard.preview("execute_code", {"code": "import subprocess; kubectl"})
    assert "secretvalue" not in guard.preview("mcp__k__apply", {"manifest": "secretvalue"})



def test_long_opt_in_preview_is_omitted_not_cut():
    cmd = 'kubectl apply --password="prefix s3cr3t-synthetic ' + "x" * 3000 + '" -f x'
    out = guard.preview("terminal", {"command": cmd}, include_command=True)
    assert "s3cr3t" not in out and "too long" in out
