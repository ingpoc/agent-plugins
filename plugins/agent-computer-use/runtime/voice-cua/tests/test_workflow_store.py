"""Workflow persistence, confinement, and repair-before-retry behavior."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
from voice_cua import workflow_store as store

INDEX = "---\nname: samantha-workflow\ndescription: Samantha task workflows.\n---\n\n- Create or repair a Samantha workflow → [Workflow authoring](references/authoring.md)\n"


class WorkflowStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="samantha-workflow-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / "samantha-workflow"
        (self.root / "references").mkdir(parents=True)
        (self.root / "SKILL.md").write_text(INDEX)
        (self.root / "references/authoring.md").write_text("# Workflow authoring\n")
        patcher = patch.object(store, "WORKFLOW_ROOT", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        store.reset_session()
        self.addCleanup(store.reset_session)

    def save(self, **kw):
        return store.save_workflow({"slug": "open-music", "title": "Open Music", "trigger": "Play a song in Music",
                                    "content": "# Open Music\n\nOpen Music, select the song, verify playback.\n", **kw})

    def test_create_routes_to_reference_and_read_is_lazy(self):
        self.assertEqual(store.index_context(), INDEX)
        result = self.save()
        self.assertTrue(result["ok"])
        self.assertNotIn("verify playback", store.index_context())
        self.assertIn("[Open Music](references/open-music.md)", store.index_context())
        loaded = store.read_workflow({"slug": "open-music"})
        self.assertEqual(loaded["revision"], result["revision"])
        self.assertIn("verify playback", loaded["content"])
        for path in (self.root / "SKILL.md", self.root / "references/open-music.md", self.root / ".lock"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_update_replaces_trigger_and_rejects_stale_revision(self):
        first = self.save()
        changed = self.save(expected_revision=first["revision"], trigger="Listen to music", content="# Corrected\nVerify the player state.\n")
        self.assertTrue(changed["ok"])
        self.assertIn("Listen to music", store.index_context())
        self.assertNotIn("Play a song", store.index_context())
        self.assertEqual(self.save(expected_revision=first["revision"])["error_type"], "workflow_revision_conflict")
        self.assertEqual(self.save()["error_type"], "workflow_revision_conflict")
        self.assertIn("Corrected", store.read_workflow("open-music")["content"])

    def test_no_traversal_owner_overwrite_or_multiline_trigger(self):
        for slug in ("../escape", "/tmp/escape", "a/b", "UPPER", "a" * 65, "authoring"):
            with self.subTest(slug=slug):
                self.assertFalse(self.save(slug=slug)["ok"])
        self.assertFalse(self.save(trigger="one\ntwo")["ok"])
        self.assertFalse(self.save(title="link](elsewhere)")["ok"])
        self.assertFalse((self.root.parent / "escape.md").exists())

    def test_oversized_inputs_and_files_are_refused_without_writes(self):
        self.assertFalse(self.save(content="x" * (store.DOCUMENT_MAX + 1))["ok"])
        self.assertFalse(self.save(content="🪐" * store.DOCUMENT_MAX)["ok"])
        self.assertEqual((self.root / "SKILL.md").read_text(), INDEX)
        (self.root / "references/large.md").write_text("x" * (store.DOCUMENT_MAX + 1))
        self.assertFalse(store.read_workflow("large")["ok"])
        (self.root / "SKILL.md").write_text("x" * (store.INDEX_MAX + 1))
        self.assertEqual(store.index_context(), "")

    def test_symlinks_at_root_ancestor_reference_index_and_lock_are_refused(self):
        outside = self.root.parent / "outside"
        outside.mkdir()
        target = outside / "untouched.md"
        target.write_text("untouched")
        for name in ("references/open-music.md", "SKILL.md", ".lock"):
            path = self.root / name
            previous = path.read_bytes() if path.exists() else None
            path.unlink(missing_ok=True)
            path.symlink_to(target)
            self.assertFalse(self.save()["ok"], name)
            path.unlink()
            if previous is not None:
                path.write_bytes(previous)
        link = self.root.parent / "linked"
        link.symlink_to(self.root, target_is_directory=True)
        with patch.object(store, "WORKFLOW_ROOT", link):
            self.assertFalse(self.save()["ok"])
        with patch.object(store, "WORKFLOW_ROOT", link / "nested"):
            self.assertFalse(self.save()["ok"])
        references = self.root / "references"
        backup = self.root / "refs-backup"
        references.rename(backup)
        references.symlink_to(outside, target_is_directory=True)
        self.assertFalse(self.save()["ok"])
        references.unlink()
        backup.rename(references)
        self.assertEqual(target.read_text(), "untouched")

    def test_failed_index_write_rolls_back_document_and_index(self):
        first = self.save()
        old_doc = (self.root / "references/open-music.md").read_text()
        old_index = (self.root / "SKILL.md").read_text()
        original = store._write
        failed = False

        def write(name, text):
            nonlocal failed
            if name == "SKILL.md" and not failed:
                failed = True
                raise OSError("disk failure")
            return original(name, text)

        with patch.object(store, "_write", write):
            result = self.save(expected_revision=first["revision"], content="# Changed\n")
        self.assertFalse(result["ok"])
        self.assertEqual((self.root / "references/open-music.md").read_text(), old_doc)
        self.assertEqual((self.root / "SKILL.md").read_text(), old_index)
        self.assertFalse((self.root / ".transaction.json").exists())

    def test_interrupted_transaction_recovers_on_next_read(self):
        first = self.save()
        old_doc = (self.root / "references/open-music.md").read_text()
        old_index = (self.root / "SKILL.md").read_text()
        (self.root / ".transaction.json").write_text(json.dumps({"slug": "open-music", "document": old_doc, "index": old_index}))
        (self.root / "references/open-music.md").write_text("partial update")
        self.assertEqual(store.read_workflow("open-music")["revision"], first["revision"])
        self.assertEqual((self.root / "references/open-music.md").read_text(), old_doc)
        self.assertFalse((self.root / ".transaction.json").exists())

    def test_malformed_recovery_does_not_delete_an_existing_reference(self):
        self.save()
        path = self.root / "references/open-music.md"
        previous = path.read_text()
        (self.root / ".transaction.json").write_text(json.dumps({"slug": "open-music", "document": None, "index": 123}))
        self.assertFalse(store.read_workflow("open-music")["ok"])
        self.assertEqual(path.read_text(), previous)
        self.assertEqual((self.root / "SKILL.md").read_text().count("references/open-music.md"), 1)

    def test_crashed_creation_removes_unindexed_reference_on_recovery(self):
        path = self.root / "references/open-music.md"
        path.write_text("uncommitted creation")
        (self.root / ".transaction.json").write_text(json.dumps({"slug": "open-music", "document": None, "index": INDEX}))
        self.assertEqual(store.index_context(), INDEX)
        self.assertFalse(path.exists())

    def test_failure_blocks_workaround_until_owner_content_changes(self):
        first = self.save()
        store.read_workflow("open-music")
        store.note_failure("cua_act", {"ok": False, "error_type": "expect_unverified"})
        blocked = store.mutation_guard("cua_act", {"app": "Other App"})
        self.assertEqual(blocked["error_type"], "workflow_repair_required")
        self.assertIsNone(store.mutation_guard("cua_state"))
        self.assertIsNone(store.mutation_guard("comet_end"))
        self.save(slug="other", title="Other", trigger="A different task")
        store.read_workflow("other")
        self.assertIsNotNone(store.mutation_guard("comet_begin"))
        self.assertFalse(self.save(expected_revision=first["revision"])["ok"])
        corrected = self.save(expected_revision=first["revision"], content="# Open Music\nStop if playback cannot be verified.\n")
        self.assertTrue(corrected["ok"])
        self.assertIsNone(store.mutation_guard("cua_act"))

    def test_new_engagement_does_not_attribute_unrelated_failure_to_completed_workflow(self):
        self.save()
        store.read_workflow("open-music")
        store.note_failure("cua_act", {"ok": True, "verified": True})
        store.begin_engagement()
        store.note_failure("cua_act", {"ok": False, "error_type": "expect_unverified"})
        self.assertIsNone(store.mutation_guard("cua_act"))
        self.assertFalse(store.read_workflow("open-music")["repair_required"])

    def test_new_engagement_cannot_bypass_pending_workflow_repair(self):
        self.save()
        store.read_workflow("open-music")
        store.note_failure("cua_act", {"ok": False, "error_type": "expect_unverified"})
        store.begin_engagement()
        self.assertEqual(store.mutation_guard("cua_act")["workflow"], "open-music")
        self.assertTrue(store.read_workflow("open-music")["repair_required"])

    def test_volume_read_remains_available_while_volume_writes_require_repair(self):
        self.save()
        store.read_workflow("open-music")
        store.note_failure("system_volume", {"ok": False})
        self.assertIsNone(store.mutation_guard("system_volume", {"direction": "read"}))
        self.assertIsNone(store.mutation_guard("system_volume", {}))
        for direction in ("up", "down"):
            self.assertEqual(store.mutation_guard("system_volume", {"direction": direction})["error_type"], "workflow_repair_required")

    def test_approval_and_user_denial_are_not_workflow_defects(self):
        self.save()
        store.read_workflow("open-music")
        for result in ({"ok": False, "needs_confirmation": True}, {"ok": False, "error": "user denied"}):
            store.note_failure("cua_act", result)
            self.assertIsNone(store.mutation_guard("cua_act"))
        store.note_failure("cua_act", {"ok": False})
        store.reset_session()
        self.assertIsNone(store.mutation_guard("cua_act"))


if __name__ == "__main__":
    unittest.main()
