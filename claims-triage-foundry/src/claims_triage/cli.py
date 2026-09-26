"""Command-line entry point.

    python -m claims_triage demo                       # scripted end-to-end walkthrough (good for screen share)
    python -m claims_triage chat [--session ID]        # interactive multi-turn chat with the supervisor
    python -m claims_triage triage data/claims.json    # one triage cycle
    python -m claims_triage ask "why was C2031 flagged?" --session ID
    python -m claims_triage agents                     # print agents, tools and JSON schemas
    python -m claims_triage reset-memory               # restore long-term memory from the seed file
    python -m claims_triage foundry-cleanup            # delete agents/vector store created in Foundry

Global flags: --backend mock|foundry, --hitl interactive|auto, --otel-console, --today YYYY-MM-DD
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .agents import AGENT_SPECS
from .config import Settings
from .observability import configure_console_otel, configure_foundry_tracing, configure_logging

BANNER = """
+--------------------------------------------------------------+
|  Claims Triage Assistant - multi-agent (Azure AI Foundry)    |
|  backend: {backend:<8} hitl: {hitl:<12} session: {session:<12}|
|  type 'help' for examples, 'exit' to quit                    |
+--------------------------------------------------------------+"""

DEMO_SCRIPT = [
    "triage data/claims.json",
    "why was claim C2031 flagged?",
    "what documents do we need for it?",
    "show the history for POL-5521",
    "what is the rule for claims right after policy inception?",
    "remember for POL-9981: policyholder says the jewelry was listed on a scheduled-items rider",
    "summary",
    "why was C-2034 flagged?",
]


def _settings(args: argparse.Namespace) -> Settings:
    if args.backend:
        os.environ["TRIAGE_BACKEND"] = args.backend
    if args.today:
        os.environ["TRIAGE_TODAY"] = args.today
    s = Settings()
    s.validate()
    return s


def _setup_observability(args: argparse.Namespace, s: Settings) -> None:
    configure_logging(s.log_level)
    if args.otel_console:
        configure_console_otel()
    if s.backend == "foundry" and s.enable_foundry_tracing:
        from azure.identity import DefaultAzureCredential

        configure_foundry_tracing(s.project_endpoint, DefaultAzureCredential())


def _supervisor(args, s, hitl: str | None = None, script=None):
    from .orchestration import HumanGate, Supervisor

    mode = hitl or args.hitl
    gate = HumanGate(mode, script=script)
    return Supervisor(s, gate=gate, session_id=getattr(args, "session", None))


def cmd_chat(args, s) -> int:
    sup = _supervisor(args, s)
    print(BANNER.format(backend=s.backend, hitl=args.hitl, session=sup.session.session_id))
    try:
        while True:
            try:
                text = input("\nhandler> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            if text.lower() in {"exit", "quit", "q"}:
                break
            print("\nassistant> " + sup.handle(text))
    finally:
        sup.close()
        print(f"\nSession saved. Resume with: python -m claims_triage chat --session {sup.session.session_id}")
    return 0


def cmd_demo(args, s) -> int:
    sup = _supervisor(args, s, hitl=args.hitl if args.hitl != "interactive" else "auto")
    print(BANNER.format(backend=s.backend, hitl=sup.gate.mode, session=sup.session.session_id))
    for text in DEMO_SCRIPT:
        print(f"\n{'=' * 70}\nhandler> {text}")
        print("\nassistant> " + sup.handle(text))
    sup.close()
    print(f"\nDemo complete. Session {sup.session.session_id} saved; long-term memory at "
          f"{s.memory_dir / 'long_term_memory.json'}")
    return 0


def cmd_triage(args, s) -> int:
    sup = _supervisor(args, s)
    print(sup.handle(f"triage {args.source}"))
    sup.close()
    return 0 if "complete" in (sup.session.turns[-1].content if sup.session.turns else "") else 1


def cmd_ask(args, s) -> int:
    sup = _supervisor(args, s)
    print(sup.handle(args.question))
    sup.close()
    print(f"(session {sup.session.session_id})", file=sys.stderr)
    return 0


def cmd_agents(args, s) -> int:
    from .tools import build_registry

    reg = build_registry()
    for spec in AGENT_SPECS.values():
        print(f"\n## {spec.display_name}  (name={spec.name}, file_search={spec.uses_file_search})")
        print(spec.instructions.strip())
        for t in reg.for_agent(spec.name):
            print(f"\n  tool: {t.name}\n  " + json.dumps(t.parameters, indent=2).replace("\n", "\n  "))
    return 0


def cmd_reset_memory(args, s) -> int:
    from .memory import LongTermMemory

    ltm = LongTermMemory(s.memory_dir / "long_term_memory.json", s.seed_memory_path)
    ltm.reset()
    print(f"Long-term memory reset from seed: {ltm.stats}")
    return 0


def cmd_foundry_cleanup(args, s) -> int:
    from .backends.foundry_backend import FoundryBackend
    from .tools import build_registry

    if s.backend != "foundry":
        print("Set TRIAGE_BACKEND=foundry (or: python -m claims_triage --backend foundry foundry-cleanup) to clean up Foundry resources.")
        return 1
    FoundryBackend(build_registry(), s).teardown(delete_remote=True)
    print("Deleted agents, vector store and uploaded knowledge files.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="claims_triage", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backend", choices=["mock", "foundry"], help="override TRIAGE_BACKEND")
    parser.add_argument("--hitl", choices=["interactive", "auto"], default="interactive")
    parser.add_argument("--otel-console", action="store_true", help="also print OpenTelemetry spans")
    parser.add_argument("--today", help="freeze 'today' (YYYY-MM-DD) for date checks")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("chat"); p.add_argument("--session")
    sub.add_parser("demo")
    p = sub.add_parser("triage"); p.add_argument("source", nargs="?", default="data/claims.json"); p.add_argument("--session")
    p = sub.add_parser("ask"); p.add_argument("question"); p.add_argument("--session")
    sub.add_parser("agents")
    sub.add_parser("reset-memory")
    sub.add_parser("foundry-cleanup")
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: never crash on a non-ASCII character
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    try:
        s = _settings(args)
    except ValueError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    _setup_observability(args, s)
    handlers = {"chat": cmd_chat, "demo": cmd_demo, "triage": cmd_triage, "ask": cmd_ask, "agents": cmd_agents,
                "reset-memory": cmd_reset_memory, "foundry-cleanup": cmd_foundry_cleanup}
    try:
        return handlers[args.cmd](args, s)
    except Exception as exc:
        if s.backend != "foundry":
            raise
        name = type(exc).__name__
        hint = ("Sign in with `az login` (or VS Code Azure sign-in) and make sure your account has the "
                "'Azure AI User' role on the Foundry project." if "Authentication" in name or "Credential" in str(exc)
                else "Check AZURE_AI_PROJECT_ENDPOINT and AZURE_AI_MODEL_DEPLOYMENT in .env.")
        print(f"\nAzure AI Foundry error ({name}): {str(exc).splitlines()[0]}\n{hint}\n"
              "Tip: run with --backend mock to use the offline mode.", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
