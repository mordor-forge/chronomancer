import pytest

from change_windows.rules import SELF_SCOPE, build_ruleset

RS = build_ruleset()


def ids(tool, **args):
    return sorted(r.id for r in RS.match(tool, args))


@pytest.mark.parametrize("cmd", [
    "kubectl apply -f deploy.yaml",
    "echo start && KUBECONFIG=/x kubectl -n web apply -k overlays/prod | tee out",
    "kubectl rollout restart deploy/api",
    "oc delete pod x",
    "kubectl \\\n  scale deploy/api --replicas=0",
])
def test_k8s_writes_match(cmd):
    assert "k8s-write" in ids("terminal", command=cmd)


@pytest.mark.parametrize("cmd", [
    "kubectl get pods -A",
    "kubectl get pods | grep apply",
    "kubectl describe deploy api; echo delete",
    "kubectl logs api",
])
def test_k8s_reads_do_not_match(cmd):
    assert "k8s-write" not in ids("terminal", command=cmd)


@pytest.mark.parametrize("cmd,rule", [
    ("helm upgrade --install app ./chart", "helm-write"),
    ("tofu apply -auto-approve", "iac-apply"),
    ("terraform destroy", "iac-apply"),
    ("argocd app sync web", "argocd-write"),
    ("git push origin main", "git-push-protected"),
    ("git push --force origin feature", "git-push-protected"),
    ("gh pr merge 42 --squash", "pr-merge"),
])
def test_other_writes(cmd, rule):
    assert rule in ids("terminal", command=cmd)


@pytest.mark.parametrize("cmd", ["git push origin feature/x", "helm list", "tofu plan", "gh pr view 42"])
def test_other_reads(cmd):
    assert ids("terminal", command=cmd) == []


def test_execute_code_matches_binary_token_not_verb():
    code = "import subprocess\nsubprocess.run(['kubectl','apply','-f','x'])"
    assert ids("execute_code", code=code) == ["code-k8s"]


def test_self_protect_cannot_be_disabled():
    rs = build_ruleset(disabled=["self-protect", "k8s-write"])
    hits = [r for r in rs.match("terminal", {"command": "sqlite3 ~/.hermes/plugin-data/change-windows/data.db"})]
    assert [r.scope for r in hits] == [SELF_SCOPE]
    assert not rs.match("terminal", {"command": "kubectl apply -f x"})


def test_bad_extra_rule_is_reported_and_defaults_stay():
    rs = build_ruleset([{"id": "bad", "scope": "s", "tools": ["terminal"], "match": "("}, {"id": "x"}])
    assert len(rs.errors) == 2
    assert rs.match("terminal", {"command": "kubectl apply -f x"})


def test_mcp_rule_by_tool_name():
    rs = build_ruleset([{"id": "mcp-k8s", "scope": "k8s-prod", "tools": ["mcp__kubernetes__*"],
                         "match": "", "field": "*"}])
    assert [r.id for r in rs.match("mcp__kubernetes__pods_delete", {"name": "x"})] == ["mcp-k8s"]
    assert not rs.match("mcp__linear__list_issues", {})
