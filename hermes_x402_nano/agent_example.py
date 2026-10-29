"""One paid call through the Hermes agent loop.

    hermes-x402-nano-demo https://pursekeeper.dev/v1/hash --method POST --body hello

Hermes' own ``run_agent.AIAgent`` loads the ``x402-nano`` plugin (pip entry point), offers the
``nano_x402_pay`` tool to the model, runs the tool call and returns the answer. By default the
model is a scripted local stand-in (no LLM key needed) whose first turn calls the tool with the
URL; pass ``--model/--base-url`` (and set OPENAI_API_KEY) to let a real model decide instead.

Payment config comes from the environment: NANO_SEED (or NANO_PRIVATE_KEY), NANO_X402_ALLOWED_PAYTO,
optional NANO_X402_MAX_PER_CALL_XNO (default 0.001) and NANO_X402_MAX_TOTAL_XNO (default 0.01).
"""

import argparse
import json
import os
import sys
import tempfile


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", nargs="?", default="https://pursekeeper.dev/v1/hash")
    ap.add_argument("--method", default="POST")
    ap.add_argument("--body", default="hello from hermes-x402-nano")
    ap.add_argument("--content-type", default="text/plain")
    ap.add_argument("--model", default=None, help="real model name (default: scripted local model)")
    ap.add_argument("--base-url", default=None, help="OpenAI-compatible base URL for --model")
    a = ap.parse_args(argv)

    # Hermes reads config from HERMES_HOME. Use a throwaway one that enables only this plugin,
    # unless the caller already set HERMES_HOME (then run `hermes plugins enable x402-nano` there).
    if "HERMES_HOME" not in os.environ:
        home = tempfile.mkdtemp(prefix="hermes-x402-nano-")
        with open(os.path.join(home, "config.yaml"), "w") as f:
            f.write("plugins:\n  enabled:\n    - x402-nano\n")
        os.environ["HERMES_HOME"] = home

    from run_agent import AIAgent  # hermes-agent

    tool_args = {"url": a.url, "method": a.method}
    if a.body is not None:
        tool_args["body"] = a.body
        tool_args["content_type"] = a.content_type

    scripted = None
    if a.model:
        base_url, api_key, model = a.base_url, os.environ.get("OPENAI_API_KEY", ""), a.model
    else:
        from .scripted_model import ScriptedModel
        scripted = ScriptedModel(tool_args)
        base_url, api_key, model = scripted.start(), "scripted", "scripted"

    try:
        agent = AIAgent(
            base_url=base_url,
            api_key=api_key,
            provider="custom",
            model=model,
            enabled_toolsets=["x402-nano"],
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=4,
            tool_delay=0,
            save_trajectories=False,
        )
        prompt = (f"Fetch {a.url} with the nano_x402_pay tool (method {a.method}). "
                  "Call it once and report the status, amount paid and block hash.")
        result = agent.run_conversation(prompt)
    finally:
        if scripted:
            scripted.stop()

    final = result.get("final_response") if isinstance(result, dict) else result
    print(final)
    tool_results = [m.get("content") for m in (result.get("messages", []) if isinstance(result, dict) else [])
                    if m.get("role") == "tool"]
    ok = False
    for t in tool_results:
        try:
            ok = ok or bool(json.loads(t).get("ok"))
        except Exception:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
