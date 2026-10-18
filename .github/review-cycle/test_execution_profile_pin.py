"""Execution-baseline drift tests with in-memory fixtures only; no model/network."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import Mock

import cloud_consensus as consensus
import cloud_state as state
from test_cloud_state import BASE, HEAD, NOW, config, feedback


def snapshot():
    documents = []
    for path, content in [("README.md", "# Scope\nPreserve behavior.\n"),
                          ("AGENTS.md", "Preserve safety boundaries.\n")]:
        documents.append({"path": path, "sha256": hashlib.sha256(content.encode()).hexdigest(),
                          "content": content})
    value = {"schema_version": 1, "revision": BASE, "documents": documents}
    value["sha256"] = hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return value


class ExecutionProfilePinTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = snapshot()
        self.config = config(review_documents=["README.md"], required_ci=[{
            "name": "application-tests", "workflow": ".github/workflows/checks.yml",
            "app_id": 15368, "app_slug": "github-actions"}], review_criteria=[{
                "id": "C1", "source": "README.md", "section": "Scope",
                "requirement": "Preserve behavior"}])
        self.pr = {"title": "Preserve behavior", "body": "No expanded scope"}
        self.record = {"state": "ready", "attempts": 4, "processed_review_ids": [10],
                       "processed_heads": [HEAD], "verification_attempts": {HEAD: 1},
                       "last_run_id": "prior-failed-run"}
        self.manifest = consensus.pin_manifest(self.config, self.snapshot, self.record, self.pr)

    def assert_drift_rejected(self, config):
        before = deepcopy(self.record)
        # Even a fully valid replacement profile is not an approved baseline.
        consensus.validate_execution_profile(config, self.snapshot)
        with self.assertRaises(state.StateError):
            consensus.pin_manifest(config, self.snapshot, self.record, self.pr)
        self.assertEqual(self.record, before)

    def test_new_baseline_pins_all_execution_dependencies_without_aliasing(self):
        pin = self.record["review_execution_profile"]
        self.assertEqual(set(pin), {"schema_version", "owner", "model", "test_image", "test_commands",
                                    "required_ci", "review_documents", "criterion_manifest"})
        self.assertEqual(pin["owner"], "pangin")
        self.assertEqual(pin["model"], "opus[1m]")
        for key in ("test_image", "test_commands", "required_ci", "review_documents"):
            self.assertEqual(pin[key], self.config[key])
        self.assertEqual(pin["criterion_manifest"], self.manifest)
        self.config["test_commands"][0].append("changed")
        self.config["required_ci"][0]["name"] = "changed"
        self.config["review_criteria"][0]["requirement"] = "changed"
        self.assertNotIn("changed", pin["test_commands"][0])
        self.assertEqual(pin["required_ci"][0]["name"], "application-tests")
        self.assertEqual(pin["criterion_manifest"]["criteria"][0]["requirement"], "Preserve behavior")

    def test_unchanged_profile_is_noop_and_preserves_budgets(self):
        before = deepcopy(self.record)
        self.assertEqual(consensus.pin_manifest(deepcopy(self.config), self.snapshot,
                                               self.record, self.pr), self.manifest)
        self.assertEqual(self.record, before)

    def test_valid_digest_pinned_image_change_is_rejected(self):
        changed = deepcopy(self.config)
        changed["test_image"] = "ghcr.io/pangin/other-runtime@sha256:" + "e" * 64
        self.assert_drift_rejected(changed)

    def test_valid_test_argv_change_is_rejected(self):
        changed = deepcopy(self.config)
        changed["test_commands"] = [["python3", "-m", "unittest", "tests.test_other"]]
        self.assert_drift_rejected(changed)

    def test_valid_required_ci_changes_are_rejected(self):
        for field, value in [("name", "weakened-tests"), ("workflow", ".github/workflows/other.yml")]:
            with self.subTest(field=field):
                changed = deepcopy(self.config)
                changed["required_ci"][0][field] = value
                self.assert_drift_rejected(changed)

    def test_mandatory_paths_change_even_when_snapshot_already_contains_it_is_rejected(self):
        changed = deepcopy(self.config)
        changed["review_documents"].append("AGENTS.md")
        self.assert_drift_rejected(changed)

    def test_valid_criterion_requirement_change_is_rejected(self):
        changed = deepcopy(self.config)
        changed["review_criteria"][0]["requirement"] = "Allow changed behavior"
        self.assert_drift_rejected(changed)

    def test_wrong_owner_or_nonofficial_model_cannot_create_or_reenter_baseline(self):
        for changes in [{"owner": "BaseLabs"}, {"model": "local-qwen"}]:
            for record in [{}, self.record]:
                with self.subTest(changes=changes, existing=bool(record)):
                    before = deepcopy(record)
                    with self.assertRaises(state.StateError):
                        consensus.pin_manifest({**self.config, **changes}, self.snapshot, record, self.pr)
                    self.assertEqual(record, before)

    def test_legacy_manifest_without_profile_never_migrates_implicitly(self):
        self.record.pop("review_execution_profile")
        before = deepcopy(self.record)
        with self.assertRaisesRegex(state.StateError, "approve a fresh baseline explicitly"):
            consensus.pin_manifest(self.config, self.snapshot, self.record, self.pr)
        self.assertEqual(self.record, before)

    def test_final_prepare_cannot_bypass_pin_before_request_or_attempt(self):
        changed = deepcopy(self.config)
        changed["test_commands"] = [["python3", "-m", "unittest", "tests.test_other"]]
        ledger = {"schema_version": 1, "pulls": {"pangin/example#7": self.record}}
        before = deepcopy(ledger)
        controller, api = Mock(), Mock()
        with self.assertRaises(state.StateError):
            consensus.prepare(controller, None, api, changed, feedback(findings=False), self.pr,
                              [], None, ledger, 99, self.snapshot)
        api.request.assert_not_called()
        api.pages.assert_not_called()
        controller.ledger_write.assert_not_called()
        controller.output.assert_not_called()
        controller.save.assert_not_called()
        self.assertEqual(ledger, before)

    def test_partial_profile_without_manifest_is_not_a_new_baseline(self):
        self.record.pop("review_manifest")
        before = deepcopy(self.record)
        with self.assertRaisesRegex(state.StateError, "Incomplete review baseline"):
            consensus.pin_manifest(self.config, self.snapshot, self.record, self.pr)
        self.assertEqual(self.record, before)

    def test_mutating_stored_profile_or_parallel_ci_cannot_bypass_pin(self):
        for change in [lambda record: record["review_execution_profile"].update(schema_version=True),
                       lambda record: record["review_execution_profile"].update(extra="unapproved"),
                       lambda record: record["review_execution_profile"]["test_commands"][0].append("changed"),
                       lambda record: record["review_required_ci"][0].update(name="changed")]:
            record = deepcopy(self.record)
            change(record)
            before = deepcopy(record)
            with self.subTest(record=record), self.assertRaises(state.StateError):
                consensus.pin_manifest(self.config, self.snapshot, record, self.pr)
            self.assertEqual(record, before)

    def test_explicit_approved_reset_rebuilds_profile_and_archives_old_without_budget_reset(self):
        request_id = "human-" + "a" * 20
        self.record["baseline_refresh_approval"] = {
            "schema_version": 1, "request_id": request_id, "head_sha": HEAD,
            "original_base_sha": "c" * 40, "target_revision": BASE,
            "source_review_ids": [10], "approved_at": NOW, "status": "pending"}
        self.record["human_response"] = {"option_id": "update-docs", "request_id": request_id,
                                         "approved_revision": BASE}
        old = deepcopy(self.record["review_execution_profile"])
        fresh = state.consume_baseline_refresh(self.record, feedback(rid=11, findings=False),
                                               trusted_revision=BASE, now=NOW)
        self.assertNotIn("review_execution_profile", fresh)
        self.assertEqual(fresh["baseline_history"][0]["baseline"]["review_execution_profile"], old)
        changed = deepcopy(self.config)
        changed["test_commands"] = [["python3", "-m", "unittest", "tests.test_other"]]
        consensus.pin_manifest(changed, self.snapshot, fresh, self.pr)
        self.assertEqual(fresh["review_execution_profile"]["test_commands"], changed["test_commands"])
        for key in ("attempts", "processed_heads", "processed_review_ids", "verification_attempts", "last_run_id"):
            self.assertEqual(fresh[key], self.record[key])


if __name__ == "__main__":
    unittest.main()
