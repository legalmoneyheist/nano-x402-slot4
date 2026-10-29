"""The plugin inside Hermes' own agent loop (run_agent.AIAgent), offline.

A scripted OpenAI-compatible model on 127.0.0.1 makes the tool call; the seller is the mocked 402.
"""

import json
import os

import pytest

import hermes_x402_nano.plugin as plugin
from hermes_x402_nano.scripted_model import ScriptedModel

from hermes_x402_nano.tests.helpers import PAY_TO, TEST_SEED

URL = "https://seller.test/v1/hash"


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    (tmp_path / "config.yaml").write_text("plugins:\n  enabled:\n    - x402-nano\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("NANO_X402_ALLOWED_PAYTO", PAY_TO)
    return tmp_path


def run_agent_once(tool_args):
    from hermes_cli import plugins as hp
    hp.discover_plugins(force=True) if "force" in hp.discover_plugins.__code__.co_varnames else hp.discover_plugins()
    from run_agent import AIAgent
    model = ScriptedModel(tool_args)
    base = model.start()
    try:
        agent = AIAgent(base_url=base, api_key="scripted", provider="custom", model="scripted",
                        enabled_toolsets=["x402-nano"], quiet_mode=True, skip_context_files=True,
                        skip_memory=True, max_iterations=4, tool_delay=0)
        result = agent.run_conversation("Fetch the resource with nano_x402_pay once.")
    finally:
        model.stop()
    tools = [json.loads(m["content"]) for m in result["messages"] if m.get("role") == "tool"]
    return result, tools, model


def test_entry_point_is_declared():
    from importlib.metadata import entry_points
    eps = {e.name: e.value for e in entry_points(group="hermes_agent.plugins")}
    assert eps.get("x402-nano") == "hermes_x402_nano"


def test_register_uses_hermes_context():
    calls = []

    class Ctx:
        def register_tool(self, **kw):
            calls.append(kw)
    plugin.register(Ctx())
    assert calls[0]["name"] == "nano_x402_pay" and calls[0]["toolset"] == "x402-nano"
    assert calls[0]["schema"]["parameters"]["required"] == ["url"]


def test_agent_loop_pays_once(hermes_home, seller, signer, make_client):
    seller.x402(URL)
    client = make_client()
    plugin.set_client(client)
    try:
        result, tools, model = run_agent_once({"url": URL})
    finally:
        plugin.set_client(None)
    offered = [t["function"]["name"] for req in model.requests for t in req.get("tools") or []]
    assert "nano_x402_pay" in offered
    assert len(tools) == 1 and tools[0]["paid"] and tools[0]["ok"]
    assert tools[0]["amount_xno"] == "0.001"
    assert signer.count == 1 and len(seller.paid_calls()) == 1
    assert tools[0]["block_hash"] in result["final_response"]


def test_agent_loop_refusal_reaches_model(hermes_home, seller, signer, make_client):
    seller.x402(URL)
    plugin.set_client(make_client(allowed=()))
    try:
        _, tools, _ = run_agent_once({"url": URL})
    finally:
        plugin.set_client(None)
    assert tools[0]["refused"] == "pay_to_not_allowed" and signer.count == 0


def test_tool_output_never_contains_key_material(hermes_home, seller, monkeypatch, capfd, caplog):
    """Env signer end to end: the seed and private key appear in no tool output, log or stream."""
    import logging
    import nanopy
    from hermes_x402_nano import NanoX402Client, Policy
    from hermes_x402_nano.signer import AccountState
    from hermes_x402_nano.tests.helpers import FakeSession, FRONTIER, BALANCE, REP

    caplog.set_level(logging.DEBUG)
    seed = TEST_SEED[:-1] + "7"
    sk = nanopy.deterministic_key(seed, 0)
    monkeypatch.setenv("NANO_SEED", seed)
    seller.x402(URL)
    client = NanoX402Client(Policy.from_env(), session=FakeSession(seller),
                            account_info=lambda a: AccountState(FRONTIER, BALANCE, REP),
                            work=lambda r, t: "1" * 16)
    plugin.set_client(client)
    try:
        result, tools, model = run_agent_once({"url": URL})
    finally:
        plugin.set_client(None)
    assert tools[0]["paid"]
    out, err = capfd.readouterr()
    blob = json.dumps(result, default=str) + json.dumps(model.requests) + out + err + caplog.text
    blob += repr(client.signer) + str(client.signer)
    for secret in (seed, sk):
        assert secret.lower() not in blob.lower()
    with pytest.raises(TypeError):
        import pickle
        pickle.dumps(client.signer)


def test_handler_rejects_missing_url():
    out = json.loads(plugin.handle_pay({}))
    assert out["refused"] == "bad_arguments"
