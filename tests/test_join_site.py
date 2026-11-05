"""Where a join disagreement is reported: the location named after
the message is the edited side when the check can tell which side was edited,
and the message says so; without that knowledge it never presents one side of
a tie as the culprit.

Run: python -m unittest discover -s tests -v   (stdlib only)
"""

from __future__ import annotations

import subprocess
import unittest

from test_engine_review import Fixture, blocks, link

REGEX = "V = \"([^\"]+)\""
FILES = {"web/api.ts": 'export const V = "v1";\n',
         "server/main.go": 'package main\nconst V = "v1"\n'}
JOIN = {"id": "v", "name": "v", "link": "web-api", "points": [
    {"path": "web/api.ts", "side": "web", "regex": REGEX},
    {"path": "server/main.go", "side": "api", "regex": REGEX}]}


class JoinSite(Fixture):
    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), "-c", "user.email=t@example.invalid",
                        "-c", "user.name=t", *args], check=True, capture_output=True)

    def committed(self, files: dict = FILES, the_join: dict = JOIN) -> None:
        self.make(files, {"blocks": blocks(), "links": [link()], "joins": [the_join]})
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "base")

    def problem(self, *extra: str) -> tuple[str, str | None, int | None]:
        if extra:
            from crossweft import engine
            result = engine.evaluate(engine.load_config(self.root), changed=extra[0])
        else:
            result = self.evaluate()
        joins = [p for p in result.new if p.category == "joins"]
        self.assertEqual(len(joins), 1, [p.message for p in result.new])
        return joins[0].message, joins[0].path, joins[0].line

    def test_the_edited_side_is_named_whichever_it_is(self) -> None:
        # the first point (web) is the edited one: the old code always pointed at the
        # second, so the location sent the reader to the file they did not touch
        self.committed()
        self.write("web/api.ts", 'export const V = "v2";\n')
        message, path, line = self.problem()
        self.assertEqual((path, line), ("web/api.ts", 1))
        self.assertIn("edited side: web@web/api.ts:1", message)
        self.assertIn("api@server/main.go:2", message)

        self.write("web/api.ts", FILES["web/api.ts"])
        self.write("server/main.go", 'package main\nconst V = "v2"\n')
        message, path, line = self.problem()
        self.assertEqual((path, line), ("server/main.go", 2))
        self.assertIn("edited side: api@server/main.go:2", message)

    def test_changed_scope_decides_after_a_commit(self) -> None:
        self.committed()
        self.write("web/api.ts", 'export const V = "v2";\n')
        self.git("commit", "-q", "-am", "edit web")
        message, path, _ = self.problem("HEAD~1")
        self.assertEqual(path, "web/api.ts")
        self.assertIn("edited side: web@web/api.ts:1", message)

    def test_both_sides_edited_names_every_site(self) -> None:
        self.committed()
        self.write("web/api.ts", 'export const V = "v2";\n')
        self.write("server/main.go", 'package main\nconst V = "v3"\n')
        message, _, _ = self.problem()
        self.assertIn("none is marked as edited", message)
        self.assertIn("web@web/api.ts:1, api@server/main.go:2", message)

    def test_outside_git_a_tie_is_not_presented_as_the_culprit(self) -> None:
        self.make({"web/api.ts": 'export const V = "v2";\n', "server/main.go": FILES["server/main.go"]},
                  {"blocks": blocks(), "links": [link()], "joins": [JOIN]})
        message, _, _ = self.problem()
        self.assertIn("only one of them -- check every site", message)
        self.assertNotIn("edited side", message)

    def test_a_lone_drifted_copy_among_agreeing_sites_is_named(self) -> None:
        the_join = {**JOIN, "points": JOIN["points"] + [
            {"path": "server/copy.go", "side": "api", "regex": REGEX}]}
        files = {**FILES, "server/copy.go": 'package main\nconst V = "v9"\n'}
        self.make(files, {"blocks": blocks(), "links": [link()], "joins": [the_join]})
        message, path, _ = self.problem()
        self.assertEqual(path, "server/copy.go")
        self.assertIn("differs from the value the other 2 sites share", message)


if __name__ == "__main__":
    unittest.main()
