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
