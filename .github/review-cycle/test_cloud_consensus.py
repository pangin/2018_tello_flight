from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import cloud_consensus as cc
import cloud_controller as c
import cloud_state as s
import consensus_evidence as evidence
import review_documents as documents
from test_cloud_state import config, feedback, pr, repo, BOT

NOW = "2026-10-01T00:05:00Z"
REQUESTED = "2026-10-01T00:00:00Z"
PROFILE = [{"name": "application-container-smoke", "workflow": ".github/workflows/review-cycle-checks.yml",
            "app_id": 15368, "app_slug": "github-actions"}]


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "workspace"; self.workspace.mkdir()
        self.git("init", "-q")
        (self.workspace / "README.md").write_text("# Scope\nPreserve behavior\n")
        (self.workspace / "app.js").write_text("const value = 1;\n")
        path = self.workspace / PROFILE[0]["workflow"]
        path.parent.mkdir(parents=True); path.write_text("name: approved-check\n")
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "approved")
        self.head = self.git("rev-parse", "HEAD").strip()
        self.feedback = feedback(head=self.head, base=self.head, rid=20, findings=False)
        self.pr = pr(head=self.head, base=self.head)
        self.pr["head"]["ref"] = "fix/example"; self.pr["base"]["ref"] = "main"
        self.pr.update(title="Preserve behavior", body="No hardware operations")
        self.config = config(review_documents=["README.md"], required_ci=PROFILE,
                             review_criteria=[{"id":"C1","source":"README.md","section":"Scope",
                                               "requirement":"Preserve behavior"}])
        self.ledger = s.new_ledger()
        self.snapshot = c.pin_documents(self.workspace, self.config, self.ledger, self.feedback, ["app.js"])
        record = self.ledger["pulls"]["pangin/example#7"]
        self.manifest = cc.pin_manifest(self.config, self.snapshot, record, self.pr)
        self.ledger = s.acknowledge_clean(self.ledger, self.feedback, now=NOW)
        record = self.record
        record["review_requests"] = {self.pair: {"head_sha":self.head,"base_sha":self.head,
            "document_fingerprint":self.snapshot["sha256"], "manifest_sha256":self.manifest["sha256"],
            "requested_at":REQUESTED,"status":"posted","comment_id":100}}
        self.codex = self.verdict("codex")
        self.reviews = [self.native_review(self.codex)]
        self.review_comments = []
        self.check = {"id":101,"name":PROFILE[0]["name"],"head_sha":self.head,
                      "app":{"id":15368,"slug":"github-actions"}, "status":"completed","conclusion":"success",
                      "details_url":"https://github.com/pangin/example/actions/runs/42/job/101"}
        self.run = {"id":42,"head_sha":self.head,"repository":repo(),"head_repository":repo(),
                    "path":PROFILE[0]["workflow"],"status":"completed","conclusion":"success","event":"pull_request",
                    "pull_requests":[{"number":7,"head":{"sha":self.head},"base":{"sha":self.head}}]}
        self.native_requests = []
        self.api = Mock(); self.api.request.side_effect = self.request; self.api.pages.side_effect = self.pages

    @property
    def record(self): return self.ledger["pulls"]["pangin/example#7"]

    @property
    def pair(self): return self.head + ":" + self.head

    def git(self, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=self.workspace,
                              check=True, capture_output=True, text=True).stdout

    def verdict(self, reviewer):
        value = cc.verdict_example(self.feedback, self.snapshot, self.manifest, reviewer)
        value.update(verdict="pass", uncertain=False)
        value["criteria"] = [{"id":"C1","status":"satisfied","reason":"Document and current code preserve behavior",
                              "evidence":[{"path":"README.md","revision":self.head,"start_line":1,"end_line":2},
                                          {"path":"app.js","revision":self.head,"start_line":1,"end_line":1}]}]
        return value

    def native_review(self, verdict):
        return {"id":20,"user":BOT,"commit_id":self.head,"state":"COMMENTED","submitted_at":NOW,
                "body":cc.VERDICT_MARKER+"\n```json\n"+json.dumps(verdict)+"\n```"}

    def request(self, route, method="GET", payload=None):
        if route.startswith("users/"): return BOT
        if route == "repos/pangin/example": return repo()
        if route == "repos/pangin/example/pulls/7": return deepcopy(self.pr)
        if "/check-runs?" in route: return {"check_runs":[deepcopy(self.check)]}
        if route == "repos/pangin/example/actions/runs/42": return deepcopy(self.run)
        if method in {"POST","PATCH"} and "/comments" in route:
            body = payload["body"]
            if body.startswith(c.MARKER):
                self.ledger = json.loads(body.split("```json\n",1)[1].split("\n```",1)[0]); return {"id":99}
            self.native_requests.append(body); return {"id":100}
        if method == "POST" and route.endswith("/labels"):
            return {"labels": payload.get("labels", [])}
        raise AssertionError((route,method))

    def pages(self, route):
        if route.endswith("/reviews"): return deepcopy(self.reviews)
        if route.endswith("/pulls/7/comments"): return deepcopy(self.review_comments)
        if route.endswith("/issues/7/comments"):
            return [{"id":99,"user":c.LEDGER_BOT,
                     "body":c.MARKER+"\n```json\n"+json.dumps(self.ledger)+"\n```"}]
        if route.endswith("/files"): return [{"filename":"app.js"}]
        raise AssertionError(route)

    def prepare(self):
        with patch.dict(os.environ, {"GITHUB_WORKSPACE":str(self.workspace),"GITHUB_RUN_ID":"123"}), \
             patch.object(c,"now",return_value=NOW), patch.object(c,"output") as output:
            cc.prepare(c,self.root,self.api,self.config,self.feedback,self.pr,self.reviews,BOT,
                       deepcopy(self.ledger),99,self.snapshot)
        return {call.args[0]:call.args[1] for call in output.call_args_list}

    def complete(self, verdict=None):
        with patch.dict(os.environ,{"CLAUDE_CONCLUSION":"success",
                                    "CLAUDE_STRUCTURED_OUTPUT":json.dumps(verdict or self.verdict("claude"))}), \
             patch.object(c,"now",return_value=NOW), patch.object(c,"test_container") as tests:
            c.validate_and_test(self.root,self.workspace,self.api)
        return tests

    def test_explicit_verdicts_actual_ci_and_tests_reach_consensus_without_push(self):
        outputs = self.prepare()
        self.assertEqual(outputs["run_claude"],"true")
        self.assertIn("--tools 'Read,Glob,Grep'",outputs["claude_args"])
        self.assertNotIn("Edit",outputs["claude_args"])
        self.assertEqual(self.record["state"],"verifying")
        self.assertEqual(c.load(self.root/"job.json")["mode"],"verification")
        tests = self.complete()
        tests.assert_called_once()
        self.assertEqual(self.record["state"],"complete")
        self.assertEqual(self.record["consensus_proof"]["run_id"],"123:1")
        self.assertEqual(c.load(self.root/"finished.json")["outcome"],"complete")
        self.assertEqual(self.git("rev-parse","HEAD").strip(),self.head)

    def test_unlimited_fixes_still_require_explicit_same_sha_consensus(self):
        self.config["max_rounds"] = None
        self.record["attempts"] = 100
        self.check.update(status="in_progress", conclusion=None)
        outputs = self.prepare()
        self.assertNotEqual(outputs.get("run_claude"), "true")
        self.assertNotEqual(self.record["state"], "complete")
        self.check.update(status="completed", conclusion="success")
        self.assertEqual(self.prepare()["run_claude"], "true")
        self.complete().assert_called_once()
        self.assertEqual(self.record["state"], "complete")
        self.assertEqual(self.record["attempts"], 100)

    def test_pending_enrollment_cannot_start_or_complete_final_model_assessment(self):
        self.config["readiness"] = "pending"
        with self.assertRaises(s.StateError):
            self.prepare()
        self.assertFalse((self.root / "job.json").exists())
        self.config["readiness"] = "ready"
        self.prepare()
        job = c.load(self.root / "job.json")
        job["config"]["readiness"] = "pending"
        c.save(self.root / "job.json", job)
        with patch.object(c, "test_container") as tests, self.assertRaises(s.StateError):
            cc.complete(c, self.root, self.workspace, self.api)
        tests.assert_not_called()
        self.assertNotEqual(self.record["state"], "complete")

    def test_no_marked_native_verdict_requests_once_without_inference(self):
        self.record["review_requests"] = {}
        self.reviews = []
        self.assertNotIn("run_claude",self.prepare())
        self.assertNotIn("run_claude",self.prepare())
        self.assertEqual(len(self.native_requests),1)
        self.assertTrue(self.native_requests[0].startswith(cc.REQUEST_MARKER))
        self.assertEqual(self.record["state"],"awaiting_verification")
        self.assertFalse((self.root/"job.json").exists())

    def test_interrupted_request_never_posts_or_infers_again(self):
        self.record["review_requests"][self.pair]["status"] = "posting"
        with self.assertRaises(s.StateError): self.prepare()
        self.assertEqual(self.native_requests,[])

    def test_ci_pending_or_spoofed_does_not_spend_final_model_attempt(self):
        for change in [{"status":"in_progress","conclusion":None},{"app":{"id":99,"slug":"github-actions"}}]:
            with self.subTest(change=change):
                before=deepcopy(self.check); self.check.update(change)
                self.assertNotIn("run_claude",self.prepare())
                self.assertNotIn("verification_attempts",self.record)
                self.assertFalse((self.root/"job.json").exists()); self.check=before

    def test_ci_workflow_or_repository_mismatch_cannot_pass(self):
        for change in [{"path":".github/workflows/other.yml"},{"head_repository":{"full_name":"someone/example"}},
                       {"head_sha":"a"*40},{"event":"push"}]:
            before=deepcopy(self.run); self.run.update(change)
            with self.subTest(change=change),self.assertRaises(s.StateError): self.prepare()
            self.assertFalse((self.root/"job.json").exists());self.run=before

    def test_ci_base_must_match_current_verdict_base(self):
        self.run["pull_requests"][0]["base"]["sha"] = "b"*40
        self.assertNotIn("run_claude",self.prepare())

    def test_native_uncertainty_or_malformed_marker_stops(self):
        for malformed in [False,True]:
            self.ledger["pulls"]["pangin/example#7"]["state"]="awaiting_verification"
            value=self.verdict("codex");value.update(verdict="needs_review",uncertain=True)
            self.reviews=[self.native_review(value)]
            if malformed:self.reviews[0]["body"]=cc.VERDICT_MARKER+"\n```json\ninvalid\n```"
            self.assertNotIn("run_claude",self.prepare())
            self.assertEqual(self.record["state"],"needs_user_input")

    def test_claude_uncertainty_never_runs_tests_or_completes(self):
        self.prepare();value=self.verdict("claude");value.update(verdict="needs_review",uncertain=True)
        tests=self.complete(value);tests.assert_not_called()
        self.assertEqual(self.record["state"],"needs_user_input")

    def test_readonly_assessment_changes_are_refused(self):
        self.prepare();(self.workspace/"app.js").write_text("changed\n")
        with self.assertRaises(s.StateError):self.complete()
        self.assertNotEqual(self.record["state"],"complete")

    def test_candidate_and_native_review_races_are_refused(self):
        self.prepare();self.pr["head"]["sha"]="c"*40
        with self.assertRaises(s.StateError):self.complete()
        self.pr["head"]["sha"]=self.head
        self.reviews[0]["body"]=self.reviews[0]["body"].replace("Document and current", "Changed assessment of")
        with self.assertRaises(s.StateError):self.complete()
        self.assertNotEqual(self.record["state"],"complete")

    def old_finding_review(self):
        return {"id": 10, "user": deepcopy(BOT), "commit_id": self.head, "state": "COMMENTED",
                "submitted_at": "2026-10-01T00:04:00Z", "body": "[P1] Finding against an older base"}

    def inline_finding(self, rid, *, created="2026-10-01T00:04:00Z", path="app.js", **changes):
        return {"id": 201, "pull_request_review_id": rid, "user": deepcopy(BOT),
                "commit_id": self.head, "original_commit_id": self.head, "line": 1,
                "created_at": created, "path": path, "body": "[P1] Check behavior", **changes}

    def test_fresh_bound_attestation_supersedes_older_same_head_findings(self):
        self.reviews.insert(0, self.old_finding_review())
        # Even a historical protected-path finding is superseded only by the
        # independently parsed exact-pair marked attestation, never by silence.
        self.review_comments = [self.inline_finding(10, path=".github/workflows/checks.yml")]
        original_reviews, original_comments = deepcopy(self.reviews), deepcopy(self.review_comments)
        self.prepare()
        self.complete().assert_called_once()
        self.assertEqual(self.record["state"], "complete")
        self.assertEqual(self.reviews, original_reviews)
        self.assertEqual(self.review_comments, original_comments)

    def test_same_time_and_later_priority_reviews_still_block_final_consensus(self):
        self.prepare()
        for submitted in [NOW, "2026-10-01T00:05:01Z"]:
            newer = {**self.old_finding_review(), "id": 21, "submitted_at": submitted}
            self.reviews = [self.native_review(self.codex), newer]
            with self.subTest(submitted=submitted), self.assertRaises(s.StateError):
                self.complete()
            self.assertNotEqual(self.record["state"], "complete")

    def test_current_attestation_priority_prose_itself_still_blocks(self):
        self.prepare()
        # This edit does not change the parsed JSON attestation, so the
        # independent fresh-finding scan must still detect its priority prose.
        self.reviews[0]["body"] = "[P1] Still unresolved\n" + self.reviews[0]["body"]
        with self.assertRaises(s.StateError):
            self.complete()
        self.assertNotEqual(self.record["state"], "complete")

    def test_new_protected_inline_finding_blocks_even_on_superseded_review(self):
        self.reviews.insert(0, self.old_finding_review())
        self.prepare()
        self.review_comments = [self.inline_finding(10, created=NOW, path=".github/workflows/checks.yml")]
        with self.assertRaises(s.ProtectedFindingError):
            self.complete()
        self.assertNotEqual(self.record["state"], "complete")

    def test_edited_old_inline_finding_blocks_without_reviving_old_summary(self):
        self.reviews.insert(0, self.old_finding_review())
        self.prepare()
        self.review_comments = [self.inline_finding(10, updated_at=NOW)]
        with self.assertRaisesRegex(s.StateError, "Native inline findings changed"):
            self.complete()
        self.assertNotEqual(self.record["state"], "complete")

    def test_inline_update_strictly_before_attestation_remains_superseded(self):
        self.reviews.insert(0, self.old_finding_review())
        self.review_comments = [self.inline_finding(10, updated_at="2026-10-01T00:04:59Z")]
        self.prepare()
        self.complete().assert_called_once()
        self.assertEqual(self.record["state"], "complete")

    def test_invalid_inline_update_timestamp_does_not_become_historical(self):
        self.reviews.insert(0, self.old_finding_review())
        self.prepare()
        for updated in ["invalid", "2026-10-01T00:03:59Z"]:
            self.review_comments = [self.inline_finding(10, updated_at=updated)]
            with self.subTest(updated=updated), self.assertRaises(s.StateError):
                self.complete()
            self.assertNotEqual(self.record["state"], "complete")

    def test_missing_or_edited_attestation_cannot_supersede_old_findings(self):
        self.reviews.insert(0, self.old_finding_review())
        self.prepare()
        original = deepcopy(self.reviews)
        for change in ["removed", "changed"]:
            self.reviews = deepcopy(original)
            if change == "removed":
                self.reviews.pop()
            else:
                self.reviews[-1]["body"] = self.reviews[-1]["body"].replace(
                    "Document and current", "Changed assessment of")
            with self.subTest(change=change), self.assertRaises(s.StateError):
                self.complete()
            self.assertNotEqual(self.record["state"], "complete")

    def test_malformed_current_bot_review_timestamp_never_means_superseded(self):
        self.prepare()
        self.reviews.insert(0, {**self.old_finding_review(), "submitted_at": "invalid"})
        with self.assertRaises((s.StateError, evidence.EvidenceError)):
            self.complete()
        self.assertNotEqual(self.record["state"], "complete")

    def test_failure_consumes_lease_and_prevents_implicit_second_assessment(self):
        self.prepare()
        c.record_finish(self.root,self.api,"failed","stopped")
        self.assertEqual(self.record["state"],"failed")
        self.assertEqual(self.record["verification_attempts"][self.head],1)
        with self.assertRaises(s.StateError):
            s.reserve_verification(self.ledger,self.feedback,run_id="retry",now=NOW)

    def test_candidate_ci_workflow_cannot_replace_approved_definition(self):
        (self.workspace/PROFILE[0]["workflow"]).write_text("name: weakened\n")
        self.git("add",".");self.git("-c","user.name=Test","-c","user.email=test@example.invalid","commit","-qm","weaken")
        f={**self.feedback,"head_sha":self.git("rev-parse","HEAD").strip()}
        with self.assertRaises(s.StateError):cc.trusted_ci_files(self.workspace,f,self.snapshot,PROFILE)

    def test_manifest_and_ci_profile_are_immutable_across_config_changes(self):
        altered=deepcopy(self.config);altered["review_criteria"]=[];altered["required_ci"]=[]
        before=deepcopy(self.record)
        with self.assertRaises((s.StateError, evidence.EvidenceError)):
            cc.pin_manifest(altered,self.snapshot,self.record,self.pr)
        self.assertEqual(self.record,before)
        self.assertEqual(self.record["review_required_ci"],PROFILE)

    def test_bad_ci_profiles_and_oversized_review_requests_fail_closed(self):
        for profile in [[],[{**PROFILE[0],"app_id":True}],[{**PROFILE[0],"workflow":"../bad.yml"}],PROFILE*2]:
            with self.subTest(profile=profile),self.assertRaises(s.StateError):cc.validate_ci_profile(profile)
        with self.assertRaises(s.StateError):
            cc.request_body(self.feedback,self.snapshot,self.manifest,{"body":"x"*60000})


if __name__=="__main__":unittest.main()
