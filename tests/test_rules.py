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
    ("git push --force origin feature", "git-push"),
    ("gh pr merge 42 --squash", "pr-merge"),
])
def test_other_writes(cmd, rule):
    assert rule in ids("terminal", command=cmd)


@pytest.mark.parametrize("cmd", ["git status", "git log --oneline", "helm list", "tofu plan", "gh pr view 42"])
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



@pytest.mark.parametrize("cmd", [
    "git push",
    "git push origin main",
    "git push -u origin feature/x",
    "git push origin feature && git push origin main",
    "echo 'git push origin feature'; git push origin main",
    "git push --all origin",
    "git push origin HEAD",
    "git push origin +feature:feature",
    "git push origin :old-branch",
])
def test_every_git_push_needs_a_window(cmd):
    assert ids("terminal", command=cmd) == ["git-push"]


def test_unless_is_evaluated_per_segment():
    rs = build_ruleset([{"id": "push-main", "scope": "s", "tools": ["terminal"],
                         "match": r"\bgit\b[^\n|;&]*?\bpush\b", "unless": r"\bpush\s+origin\s+feature\b"}],
                       include_defaults=False)
    assert not rs.match("terminal", {"command": "git push origin feature"})
    assert rs.match("terminal", {"command": "git push origin feature && git push origin main"})
    assert rs.match("terminal", {"command": "git push origin main; git push origin feature"})


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
    "subprocess.run([\"git\"," + " " * 300 + "\"push\", \"origin\", \"main\"])",
])
def test_code_git(code):
    assert "code-git" in ids("execute_code", code=code)


@pytest.mark.parametrize("tool,args", [
    ("terminal", {"command": "hermes 'change-windows' approve W1"}),
    ("terminal", {"command": "hermes change-windows reset"}),
    ("terminal", {"command": "hermes change-windows --force reset"}),
    ("terminal", {"command": "hermes change-windows list-reset"}),
    ("terminal", {"command": "hermes plugins disable change-windows"}),
    ("terminal", {"command": "hermes config set plugins.entries.change-windows.settings.mode off"}),
    ("write_file", {"path": "/home/u/.hermes/config.yaml", "content": "plugins:\n  entries:\n    change-windows: {}"}),
    ("patch", {"path": "/home/u/.hermes/profiles/p/config.yaml", "old_string": "change-windows:", "new_string": "x:"}),
    ("terminal", {"command": "sqlite3 ~/.hermes/plugin-data/change-windows/data.db 'delete from audit'"}),
])
def test_self_protect_variants(tool, args):
    assert "self-protect" in ids(tool, **args)


def test_self_protect_does_not_block_work_on_the_repo():
    assert ids("terminal", command="cd ~/work/hermes-change-windows && git status") == []
    assert ids("read_file", path="/home/u/.hermes/plugin-data/change-windows/data.db") == []
    assert ids("terminal", command="hermes change-windows list") == []
    assert ids("write_file", path="/work/hermes-change-windows/tests/fixtures/config.yaml",
               content="plugins: {entries: {change-windows: {}}}") == []


def test_mcp_rule_by_tool_name_patterns():
    rs = build_ruleset([{"id": "mcp-k8s-write", "scope": "k8s-prod",
                         "tools": ["mcp__kubernetes__*delete*", "mcp__kubernetes__*create*",
                                   "mcp__kubernetes__*apply*", "mcp__kubernetes__*scale*"]}])
    assert [r.id for r in rs.match("mcp__kubernetes__resources_delete", {"name": "x"})] == ["mcp-k8s-write"]
    assert not rs.match("mcp__kubernetes__pods_list", {})



def test_unless_keeps_a_multiline_match():
    rs = build_ruleset([{"id": "custom-git", "scope": "s", "tools": ["execute_code"], "field": "code",
                         "match": r"\bgit\b[\s\S]*\bpush\b", "unless": r"--dry-run"}], include_defaults=False)
    code = 'subprocess.run(["git",\n    "push", "origin", "main"])'
    assert [r.id for r in rs.match("execute_code", {"code": code})] == ["custom-git"]
    assert not rs.match("execute_code", {"code": code.replace('"main"', '"main", "--dry-run"')})


def test_code_git_is_linear_on_many_git_tokens():
    import time as _t
    start = _t.monotonic()
    assert ids("execute_code", code="# " + "git " * 50_000) == []
    assert _t.monotonic() - start < 0.5


@pytest.mark.parametrize("raw", [
    {"id": "b", "scope": "s", "tools": ["terminal"], "match": 123},
    {"id": "b", "scope": "s", "tools": ["terminal"], "match": ["kubectl", "apply"]},
    {"id": "b", "scope": "s", "tools": ["terminal"], "match": True},
    {"id": "b", "scope": "s", "tools": ["terminal"], "match": "x", "unless": 5},
    {"id": "b", "scope": "s", "tools": 5},
    {"id": "b", "scope": "s", "tools": ["terminal", 7]},
    {"id": "b", "scope": "s", "tools": {"terminal": True}},
])
def test_wrongly_typed_extra_rule_is_skipped_and_defaults_stay(raw):
    rs = build_ruleset([raw])
    assert len(rs.errors) == 1 and "b" not in [r.id for r in rs.rules]
    assert rs.match("terminal", {"command": "kubectl apply -f x"})
    assert not rs.match("terminal", {"command": "ls -la"})
