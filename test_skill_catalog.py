import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import MagicMock

from skill_catalog import MAX_RESOURCE_BYTES, SkillCatalog, SkillError, _read_limited


def write_skill(root, name, description, extra_files=None):
    folder = Path(root, name)
    folder.mkdir(parents=True)
    folder.joinpath("SKILL.md").write_text(
        f'---\nname: {name}\ndescription: "{description}"\n---\n\n# {name}\n\nاتبع المطلوب.\n',
        encoding="utf-8",
    )
    for relative, content in (extra_files or {}).items():
        target = folder / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return folder


class SkillCatalogTests(unittest.TestCase):
    def test_bounded_reader_never_requests_the_whole_file(self):
        stream = MagicMock()
        stream.__enter__.return_value = stream
        stream.read.return_value = b"safe"
        path = MagicMock()
        path.open.return_value = stream

        self.assertEqual(_read_limited(path, MAX_RESOURCE_BYTES, "too large"), b"safe")
        stream.read.assert_called_once_with(MAX_RESOURCE_BYTES + 1)

        stream.read.return_value = b"x" * (MAX_RESOURCE_BYTES + 1)
        with self.assertRaisesRegex(SkillError, "too large"):
            _read_limited(path, MAX_RESOURCE_BYTES, "too large")

    def test_discovers_nested_pack_parses_metadata_and_searches(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills = Path(temp_dir, "repo", "skills")
            write_skill(skills, "beta-skill", "تحليل البيانات")
            write_skill(skills, "alpha-skill", "واجهات عربية")

            catalog = SkillCatalog.from_root(temp_dir)

            self.assertEqual(catalog.root, skills.resolve())
            self.assertEqual(
                [skill.name for skill in catalog.skills],
                ["alpha-skill", "beta-skill"],
            )
            self.assertEqual([skill.name for skill in catalog.search("بيانات")], ["beta-skill"])
            self.assertEqual(catalog.search("missing"), ())
            self.assertEqual(len(catalog.fingerprint), 64)

    def test_discovers_direct_child_skill_folder(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills = Path(temp_dir, "scientific-skills")
            write_skill(skills, "demo-skill", "مهارة علمية")
            Path(temp_dir, "docs").mkdir()
            Path(temp_dir, "docs", "readme.md").write_text("x", encoding="utf-8")

            catalog = SkillCatalog.from_root(temp_dir)

            self.assertEqual(catalog.root, skills.resolve())
            self.assertEqual([skill.name for skill in catalog.skills], ["demo-skill"])

    def test_ambiguous_pack_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            write_skill(Path(temp_dir, "skills-a"), "one", "أول")
            write_skill(Path(temp_dir, "skills-b"), "two", "ثانٍ")
            with self.assertRaises(SkillError):
                SkillCatalog.from_root(temp_dir)

    def test_env_override_changes_the_default_pack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills = Path(temp_dir, "pack", "skills")
            write_skill(skills, "env-skill", "بديل")
            with unittest.mock.patch.dict(
                os.environ, {"LOCAL_AGENT_SKILLS_ROOT": str(Path(temp_dir, "pack"))}
            ):
                catalog = SkillCatalog.default()
            self.assertEqual(catalog.root, skills.resolve())
            self.assertEqual([skill.name for skill in catalog.skills], ["env-skill"])

    def test_dispatch_lists_and_reads_only_bounded_skill_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills = Path(temp_dir, "skills")
            write_skill(
                skills,
                "demo-skill",
                "وصف تجريبي",
                {"references/guide.md": "مرجع آمن", "scripts/helper.py": "print('never run')"},
            )
            catalog = SkillCatalog.from_root(skills)

            listing = catalog.dispatch("list_available_skills", {"query": "تجريبي"})
            self.assertEqual(listing["count"], 1)
            self.assertEqual(listing["skills"][0]["name"], "demo-skill")
            self.assertEqual(listing["trust"], "untrusted_skill_metadata")

            full_listing = catalog.dispatch("list_available_skills", {"query": ""})
            self.assertEqual(full_listing["count"], 1)
            self.assertEqual(
                full_listing["skills"][0]["description"], "وصف تجريبي"
            )

            write_skill(skills, "other-skill", "وصف" + " طويل" * 40)
            catalog = SkillCatalog.from_root(skills)
            compact = catalog.dispatch("list_available_skills", {"query": ""})
            self.assertEqual(compact["count"], 2)
            other = next(
                entry for entry in compact["skills"] if entry["name"] == "other-skill"
            )
            self.assertLessEqual(len(other["description"]), 161)
            self.assertTrue(other["description"].endswith("…"))

            skill = catalog.dispatch("read_skill", {"name": "demo-skill"})
            self.assertIn("اتبع المطلوب", skill["instructions"])
            self.assertIn("references/guide.md", skill["resources"])
            self.assertIn("scripts/helper.py", skill["resources"])
            self.assertEqual(skill["trust"], "untrusted_skill_instructions")
            self.assertIn("لا تشغّل", skill["policy"])

            resource = catalog.dispatch(
                "read_skill_resource",
                {"name": "demo-skill", "path": "references/guide.md"},
            )
            self.assertEqual(resource["content"], "مرجع آمن")
            self.assertEqual(resource["trust"], "untrusted_skill_resource")

            names = {schema["function"]["name"] for schema in catalog.tool_schemas()}
            self.assertEqual(
                names,
                {"list_available_skills", "read_skill", "read_skill_resource"},
            )

    def test_rejects_traversal_binary_oversize_and_malformed_skills(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills = Path(temp_dir, "skills")
            folder = write_skill(skills, "demo-skill", "وصف")
            folder.joinpath("binary.bin").write_bytes(b"\x00\x01")
            folder.joinpath("huge.md").write_text("x" * 131_000, encoding="utf-8")
            catalog = SkillCatalog.from_root(skills)

            invalid_calls = [
                ("read_skill", {"name": "missing"}),
                ("read_skill", {"name": "demo-skill", "extra": True}),
                ("read_skill_resource", {"name": "demo-skill", "path": "../SKILL.md"}),
                ("read_skill_resource", {"name": "demo-skill", "path": "binary.bin"}),
                ("read_skill_resource", {"name": "demo-skill", "path": "huge.md"}),
                ("list_available_skills", {"query": 1}),
                ("unknown", {}),
            ]
            for tool_name, arguments in invalid_calls:
                with self.subTest(tool=tool_name, arguments=arguments), self.assertRaises(SkillError):
                    catalog.dispatch(tool_name, arguments)

            broken = Path(temp_dir, "broken", "skills", "bad")
            broken.mkdir(parents=True)
            broken.joinpath("SKILL.md").write_text("# missing frontmatter", encoding="utf-8")
            with self.assertRaises(SkillError):
                SkillCatalog.from_root(broken.parent)

    def test_rejects_duplicate_names_and_missing_pack(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            skills = Path(temp_dir, "skills")
            first = write_skill(skills, "first", "one")
            second = write_skill(skills, "second", "two")
            second.joinpath("SKILL.md").write_text(
                first.joinpath("SKILL.md").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaises(SkillError):
                SkillCatalog.from_root(skills)
        with self.assertRaises(SkillError):
            SkillCatalog.from_root(Path(tempfile.gettempdir()) / "definitely-missing-skills")

    def test_oversized_instructions_are_cut_on_a_safe_boundary(self):
        from skill_catalog import MAX_INSTRUCTIONS_BYTES, _bounded_instructions

        text = "أب" * (MAX_INSTRUCTIONS_BYTES + 500)
        cut = _bounded_instructions(text)
        self.assertLess(len(cut.encode("utf-8")), MAX_INSTRUCTIONS_BYTES + 400)
        self.assertIn("اقتُطعت", cut)
        json.dumps(cut)
        self.assertEqual(_bounded_instructions("قصير"), "قصير")

        with tempfile.TemporaryDirectory() as temp_dir:
            skills = Path(temp_dir, "skills")
            folder = write_skill(skills, "huge-skill", "ضخم")
            folder.joinpath("SKILL.md").write_text(
                "---\nname: huge-skill\ndescription: ضخم\n---\n\n"
                + "نص طويل " * (MAX_INSTRUCTIONS_BYTES // 6),
                encoding="utf-8",
            )
            catalog = SkillCatalog.from_root(skills)
            payload = json.dumps(
                catalog.dispatch("read_skill", {"name": "huge-skill"}),
                ensure_ascii=False,
            ).encode("utf-8")
            self.assertLess(len(payload), MAX_INSTRUCTIONS_BYTES + 2_000)
            json.loads(payload)

    def test_tool_schema_is_json_serializable(self):
        json.dumps(SkillCatalog.tool_schemas())


if __name__ == "__main__":
    unittest.main()
