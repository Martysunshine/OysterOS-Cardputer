"""No secret is ever stored, returned or audited: the backend redacts every summary again, whatever the
hook did. The fake secrets below are obviously made up."""

from __future__ import annotations

import pytest
from oyster.claude.service import redact_summary

# Assembled at run time so that no source line looks like a real token to the repository's secret scan.
JWT = ".".join(["eyJ" + "hbGciOiJIUzI1NiJ9", "eyJ" + "zdWIiOiIxMjM0In0", "c2lnbmF0dXJl"])
PEM_BEGIN = "-----BEGIN " + "PRIVATE KEY-----"
PEM_END = "-----END " + "PRIVATE KEY-----"

# (text, fragments that must not survive redaction)
SECRETS = [
    ("curl -H 'Authorization: Bearer abcdefghijklmnop1234' https://x.test", ["abcdefghijklmnop1234"]),
    ('curl -H "Authorization: Basic dXNlcjpwYXNzd29yZA==" https://x.test', ["dXNlcjpwYXNzd29yZA"]),
    ("export API_KEY=abc123fakevalue", ["abc123fakevalue"]),
    ("run --password hunter2hunter2 --user bob", ["hunter2hunter2"]),
    ("run --token=ghTokenValue99", ["ghTokenValue99"]),
    ("mysql -u root --passwd s3cr3tpw", ["s3cr3tpw"]),
    ('cmd secret="quoted value with spaces"', ["quoted value with spaces"]),
    ("cmd client_secret: fake0a1b2c3d", ["fake0a1b2c3d"]),
    ("git clone https://user:pa55w0rd@example.com/repo.git", ["pa55w0rd"]),
    ("echo ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4", ["ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4"]),
    ("echo github_pat_" + "11ABCDEFG0abcdefghij_1234567890", ["11ABCDEFG0abcdefghij_1234567890"]),
    ("echo AKIA" + "ABCDEFGHIJKLMNOP", ["AKIAABCDEFGHIJKLMNOP"]),
    ("echo sk-" + "proj-abcdefghijklmnopqrstuv", ["sk-proj-abcdefghijklmnopqrstuv"]),
    ("echo gsk_" + "abcdefghijklmnopqrstuvwxyz", ["gsk_abcdefghijklmnopqrstuvwxyz"]),
    (f"echo {JWT}", ["eyJhbGciOiJIUzI1NiJ9", "c2lnbmF0dXJl"]),
    ("echo 0123456789abcdef0123456789abcdef", ["0123456789abcdef0123456789abcdef"]),
    (
        "echo dGhpcyBpcyBhIGZha2UgYmFzZTY0IHNlY3JldCB2YWx1ZTEyMw",
        ["dGhpcyBpcyBhIGZha2UgYmFzZTY0IHNlY3JldCB2YWx1ZTEyMw"],
    ),
    (f"{PEM_BEGIN}\nMIIEvQIBADANBgkqhkiG\n{PEM_END}", ["MIIEvQIBADANBgkqhkiG"]),
    ("cookie: session=abcdef123456", ["abcdef123456"]),
    ("X-Api-Key: fake-zzTop-42", ["fake-zzTop-42"]),
]


@pytest.mark.parametrize("text,secrets", SECRETS, ids=[t[0][:30] for t in SECRETS])
def test_redact_summary_removes_credentials(text, secrets):
    out = redact_summary(text)
    for secret in secrets:
        assert secret not in out, out


def test_ordinary_text_is_kept_readable():
    for text in ("npm test", "git status --short", "pytest tests/claude -q", "ls -la /usr/local/bin"):
        assert redact_summary(text) == text


def test_output_is_one_clean_line_within_the_bound():
    out = redact_summary("line one\n\tline two\r\n\x00\x07 end " + "z" * 300, 60)
    assert "\n" not in out and "\t" not in out and "\x00" not in out and "\x07" not in out
    assert len(out) == 60 and out.endswith("...")


def test_environment_secrets_are_removed_even_without_a_pattern(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_TOKEN", "plainwordsecretvalue")
    from oyster.ai.redact import env_secrets

    out = redact_summary("echo plainwordsecretvalue done", extra_secrets=env_secrets())
    assert "plainwordsecretvalue" not in out


@pytest.mark.parametrize("text,secrets", SECRETS[:6], ids=[t[0][:20] for t in SECRETS[:6]])
def test_nothing_secret_is_stored_listed_or_audited(kit, text, secrets):
    w = kit.hook(summary=text[:160])
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    kit.decide(kit.dev_a, item["approval_id"], item["nonce"], "deny")
    w.result()
    everything = [
        kit.client.get("/v1/claude/approvals", headers=kit.dev_a).text,
        kit.client.get("/v1/claude/audit", headers=kit.owner, params={"limit": 50}).text,
        str(item),
        str(kit.audit_rows()),
    ]
    with kit.runtime.store.tx() as conn:
        everything.append(str(conn.execute("SELECT * FROM approvals").fetchall()))
        everything.append(str(conn.execute("SELECT * FROM nonces").fetchall()))
    # the on-disk files too: nothing is hiding in the write-ahead log
    for f in kit.tmp_path.glob("claude.db*"):
        everything.append(f.read_bytes().decode("latin-1"))
    blob = "\n".join(everything)
    for secret in secrets:
        assert secret not in blob


def test_the_owner_token_and_device_tokens_never_appear_in_responses_or_rows(kit):
    from _claude_support import ADMIN

    w = kit.hook(summary="echo hi")
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    kit.decide(kit.dev_a, item["approval_id"], item["nonce"], "allow")
    w.result()
    token = kit.tokens["dev-a"]
    texts = [
        kit.client.get("/v1/claude/audit", headers=kit.owner).text,
        kit.client.get("/v1/claude/approvals", headers=kit.dev_a).text,
        str(kit.audit_rows()),
    ]
    with kit.runtime.store.tx() as conn:
        texts.append(str(conn.execute("SELECT * FROM approvals").fetchall()))
    for f in kit.tmp_path.glob("claude.db*"):
        texts.append(f.read_bytes().decode("latin-1"))
    assert all(ADMIN not in t and token not in t for t in texts)


def test_the_nonce_is_never_in_the_audit_trail_or_the_owner_view(kit):
    w = kit.hook()
    kit.wait_for_pending()
    item = kit.pending_item(kit.dev_a)
    kit.decide(kit.dev_a, item["approval_id"], item["nonce"], "allow")
    w.result()
    assert item["nonce"] not in kit.client.get("/v1/claude/audit", headers=kit.owner).text
    assert item["nonce"] not in str(kit.audit_rows())
