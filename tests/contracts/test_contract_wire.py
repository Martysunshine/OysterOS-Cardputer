"""Wire shapes produced by the API match the published examples exactly."""

from __future__ import annotations

import json

from oyster.api.runs import run_view
from oyster.contracts import Run
from oyster.evidence.validate import EXAMPLES_DIR, schema_errors
from oyster.settings import RunBudgets, Settings
from oyster.store import RunRec


def _settings(tmp_path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        bind_host="127.0.0.1",
        https_port=8443,
        http_port=8000,
        admin_token="x",
        tls_cert=tmp_path / "c",
        tls_key=tmp_path / "k",
        ai_provider="fake",
        ai_base_url="",
        ai_model="",
        ai_api_key="",
        ai_timeout_s=5,
        ai_max_input_tokens=12000,
        ai_max_output_tokens=1500,
        ai_price_input_per_mtok=None,
        ai_price_output_per_mtok=None,
        budgets=RunBudgets(),
    )


def test_run_view_reproduces_the_run_example(tmp_path):
    ex = json.loads((EXAMPLES_DIR / "run.succeeded.json").read_text(encoding="utf-8"))
    rec = RunRec(
        run_id=ex["run_id"],
        session_id=ex["session_id"],
        question=ex["question"],
        status=ex["status"],
        provider=ex["provider"],
        created_at=0.0,
        updated_at=0.0,
        actions_used=ex["budget"]["actions_used"],
        provider_requests_used=ex["budget"]["provider_requests_used"],
        elapsed_s=ex["budget"]["elapsed_s"],
        steps=ex["steps"],
        answer=ex["answer"],
        pending_action=ex["pending_action"],
        input_tokens=ex["usage"]["input_tokens"],
        output_tokens=ex["usage"]["output_tokens"],
        error=ex["error"],
    )
    view = run_view(rec, _settings(tmp_path))
    assert view == ex
    assert schema_errors("run.schema.json", view) == []
    # the contracts model accepts the API's own wire shape and dumps it back unchanged
    assert Run.model_validate(view).model_dump(mode="json", exclude_unset=True) == view
