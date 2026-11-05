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
    ("git push origin main", "git-push"),
    ("git push --force origin feature", "git-push-force"),
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



@pytest.mark.parametrize("cmd,expected", [
    ("git push", ["git-push"]),
    ("git push origin", ["git-push"]),
    ("git push origin main", ["git-push"]),
    ("git push origin feature:main", ["git-push"]),
    ("git push -u origin feature/x", []),
    ("git push origin feature/x && echo ok", []),
    ("git push --force origin feature/x", ["git-push-force"]),
    ("git push origin :old-branch", ["git-push-force"]),
])
def test_git_push_matrix(cmd, expected):
    assert ids("terminal", command=cmd) == sorted(expected)


@pytest.mark.parametrize("cmd", [
    "\"kubectl\" 'apply' -f x.yaml",
    "k\\ubectl apply -f x.yaml",
])
def test_quoted_commands_still_match(cmd):
    assert "k8s-write" in ids("terminal", command=cmd)


def test_kubectl_exec_is_its_own_rule():
    assert ids("terminal", command="kubectl exec -it api -- touch /data/x") == ["k8s-exec"]
    rs = build_ruleset(disabled=["k8s-exec"])
    assert not rs.match("terminal", {"command": "kubectl exec api -- cat /etc/hostname"})


@pytest.mark.parametrize("code", [
    "subprocess.run(['git','push','origin','main'])",
    "os.system('gh pr merge 42 --squash')",
])
def test_code_git(code):
    assert "code-git" in ids("execute_code", code=code)


@pytest.mark.parametrize("tool,args", [
    ("terminal", {"command": "hermes 'change-windows' approve W1"}),
    ("terminal", {"command": "hermes plugins disable change-windows"}),
    ("terminal", {"command": "hermes config set plugins.entries.change-windows.settings.mode off"}),
    ("write_file", {"path": "/home/u/.hermes/config.yaml", "content": "plugins:\n  entries:\n    change-windows: {}"}),
    ("terminal", {"command": "sqlite3 ~/.hermes/plugin-data/change-windows/data.db 'delete from audit'"}),
])
def test_self_protect_variants(tool, args):
    assert "self-protect" in ids(tool, **args)


def test_self_protect_does_not_block_work_on_the_repo():
    assert ids("terminal", command="cd ~/work/hermes-change-windows && git status") == []
    assert ids("read_file", path="/home/u/.hermes/plugin-data/change-windows/data.db") == []


def test_mcp_rule_by_tool_name_patterns():
    rs = build_ruleset([{"id": "mcp-k8s-write", "scope": "k8s-prod",
                         "tools": ["mcp__kubernetes__*delete*", "mcp__kubernetes__*create*",
                                   "mcp__kubernetes__*apply*", "mcp__kubernetes__*scale*"]}])
    assert [r.id for r in rs.match("mcp__kubernetes__resources_delete", {"name": "x"})] == ["mcp-k8s-write"]
    assert not rs.match("mcp__kubernetes__pods_list", {})
