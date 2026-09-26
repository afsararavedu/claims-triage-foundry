from claims_triage.memory import LongTermMemory, SessionMemory


def test_long_term_memory_persists_across_instances(settings):
    path = settings.memory_dir / "ltm.json"
    m1 = LongTermMemory(path, settings.seed_memory_path)
    assert m1.get_claim("C-1804")["policy_number"] == "POL-5521"  # seeded history
    m1.record_outcome({"claim_id": "C-9999", "policy_number": "POL-5521", "final_action": "auto_approve"})
    m2 = LongTermMemory(path, settings.seed_memory_path)  # "a separate run"
    assert m2.get_claim("c9999")["final_action"] == "auto_approve"


def test_corrupt_memory_file_is_recovered(settings):
    path = settings.memory_dir / "ltm.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ broken")
    m = LongTermMemory(path, settings.seed_memory_path)
    assert m.stats["claims_history"] == 2
    assert path.with_suffix(".corrupt.json").exists()


def test_session_round_trip(tmp_path):
    s = SessionMemory()
    s.add("user", "hello")
    s.focus_claim_id = "C-2031"
    s.agent_threads["adjuster_briefing"] = "thread_123"
    s.save(tmp_path)
    loaded = SessionMemory.load(tmp_path, s.session_id)
    assert loaded.focus_claim_id == "C-2031" and loaded.turns[0].content == "hello"
    assert loaded.agent_threads == {"adjuster_briefing": "thread_123"}
