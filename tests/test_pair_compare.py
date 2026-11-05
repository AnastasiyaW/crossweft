"""`compare: tokens` on a pair: reformatting a region keeps the
attestation, a changed token does not, and switching the mode asks for one
re-attestation with a message that says why.

Run: python -m unittest discover -s tests -v   (stdlib only)
"""

from __future__ import annotations

import unittest

from crossweft import engine
from test_engine_review import Fixture, blocks, link, run

GO = "package main\n// crossweft:begin r\nfunc round(x float64) float64 { return math.Floor(x*100+0.5) / 100 }\n// crossweft:end r\n"
TS = "// crossweft:begin r\nexport function round(x: number) { return Math.floor(x * 100 + 0.5) / 100 }\n// crossweft:end r\n"
GO_REFORMATTED = ("package main\n// crossweft:begin r\nfunc round(x float64) float64 {\n"
                  "\treturn math.Floor(x*100 + 0.5) / 100\n}\n// crossweft:end r\n")
GO_CHANGED = GO.replace("0.5", "0.49")


def pair(compare: str | None) -> dict:
    entry = {"id": "r", "name": "rounding", "link": "web-api",
             "regions": [{"path": "web/api.ts", "side": "web"},
                         {"path": "server/main.go", "side": "api"}]}
    if compare:
        entry["compare"] = compare
    return entry


class PairCompare(Fixture):
    def setup(self, compare: str | None) -> None:
        self.make({"web/api.ts": TS, "server/main.go": GO},
                  {"blocks": blocks(), "links": [link()], "pairs": [pair(compare)]})
        code, out = run(["--root", str(self.root), "attest", "r", "--reason", "read both"])
        self.assertEqual(code, 0, out)

    def pair_messages(self) -> list[str]:
        return [p.message for p in self.evaluate().new if p.category == "pairs"]

    def test_tokens_mode_ignores_reformatting(self) -> None:
        self.setup("tokens")
        self.write("server/main.go", GO_REFORMATTED)
        self.assertEqual(self.pair_messages(), [])

    def test_text_mode_still_flags_reformatting(self) -> None:
        self.setup(None)
        self.write("server/main.go", GO_REFORMATTED)
        self.assertEqual(len(self.pair_messages()), 1)

    def test_tokens_mode_flags_a_changed_token(self) -> None:
        self.setup("tokens")
        self.write("server/main.go", GO_CHANGED)
        messages = self.pair_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("server/main.go", messages[0])

    def test_switching_the_mode_says_why(self) -> None:
        self.setup(None)
        self.make({}, {"blocks": blocks(), "links": [link()], "pairs": [pair("tokens")]})
        messages = self.pair_messages()
        self.assertEqual(len(messages), 1)
        self.assertIn("now compares 'tokens'", messages[0])

    def test_an_unknown_mode_is_a_schema_error(self) -> None:
        self.make({"web/api.ts": TS, "server/main.go": GO},
                  {"blocks": blocks(), "links": [link()], "pairs": [pair("ast")]})
        code, output = self.check()
        self.assertEqual(code, 2, output)

    def test_tokens_only_sees_code_pulled_into_a_comment(self) -> None:
        for before, after in (("x = 1; // max\ny = 2;", "x = 1; // max y = 2;"),
                              ("LIMIT = 10  # max\nRETRIES = 3", "LIMIT = 10  # max RETRIES = 3")):
            self.assertNotEqual(engine.tokens_only(before), engine.tokens_only(after))

    def test_tokens_only_keeps_operators_apart(self) -> None:
        self.assertNotEqual(engine.tokens_only("i + ++j"), engine.tokens_only("i++ + j"))
        self.assertNotEqual(engine.tokens_only("a = b // c"), engine.tokens_only("a = b / / c"))

    def test_tokens_mode_refuses_indentation_languages(self) -> None:
        entry = pair("tokens")
        entry["regions"][1]["path"] = "server/main.py"
        self.make({"web/api.ts": TS, "server/main.py": "# crossweft:begin r\nx = 1\n# crossweft:end r\n"},
                  {"blocks": blocks(), "links": [link()], "pairs": [entry]})
        errors = self.evaluate().schema_errors
        self.assertTrue(any("cannot guard server/main.py" in e for e in errors), errors)

    def test_tokens_only_keeps_word_boundaries(self) -> None:
        self.assertEqual(engine.tokens_only("int  x =\n  f( a,  b )"), "int x=f(a,b)")
        self.assertNotEqual(engine.tokens_only("return x"), engine.tokens_only("returnx"))


if __name__ == "__main__":
    unittest.main()
