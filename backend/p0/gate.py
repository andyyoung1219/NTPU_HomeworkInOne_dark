"""P0 live evidence is intentionally distinct from offline test success."""


def evaluate(events):
    def passed(name):
        return any(e.get("event") == name and e.get("passed") is True for e in events)

    trials = {e.get("trial") for e in events if e.get("event") == "ai_trial"}
    checks = {
        "P0-01_sources_and_permission": passed("sources_verified"),
        "P0-02_own_moodle": passed("moodle_login") and passed("moodle_courses"),
        "P0-03_real_assignment_attachment": passed("moodle_assignments") and passed("moodle_attachment"),
        "P0-04_ai_text_and_document": passed("ai_trial") and passed("ai_document"),
        "P0-05_bridge_10_trials_and_cancel": len(trials) >= 10 and passed("ai_cancel"),
        "P0-06_redacted_evidence": passed("evidence_policy_tested"),
    }
    return {"passed": all(checks.values()), "checks": checks}
