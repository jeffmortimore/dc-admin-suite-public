"""Verification suite for Image Description Generator.

Run it from the suite root with the same Python that launches the suite:

    python3 modules/_verify_image_describer.py

The leading underscore keeps this file out of the module scanner, so it can
live beside what it tests without ever appearing on the Main Menu.

What is covered, and why each one is here rather than left to a real run:

  · usage parsing per provider, from captured SSE fixtures — the whole
    release rests on it, and a provider's shape cannot be checked by eye;
  · the endpoint round-trip, shell normalize() -> save -> module read,
    because THREE separate whitelists rebuild an endpoint dict and a field
    missing from any one of them is dropped in silence;
  · early-stop detection per provider, which is the defect the 78-image
    Jesuits run of 2026-09-02 exposed: seven replies ended mid-sentence and
    every one was saved and reported as a success;
  · reasoning-trace accounting, which is why that defect took two days to
    diagnose — Gemini bills thinking as output and counts it against the
    reply ceiling, but reports it outside candidatesTokenCount, so the
    report showed headroom that did not exist;
  · both branches of the prompt migration, including a whitespace-only
    difference, because the comparison is byte-exact by design;
  · the two quality flags against a run whose median is the point;
  · the client drift check between the two AI modules.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import dc_image_describer as M          # noqa: E402
import dc_ocr_toolkit as OCR            # noqa: E402
from app import ai_endpoints as SHELL   # noqa: E402


# ---------------------------------------------------------------------------
# A fake urlopen that replays a captured SSE stream.
# ---------------------------------------------------------------------------
class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class StreamPatch(object):
    """Replay `lines` as the body of the next request, capturing the body
    that was sent so the request builders can be asserted on too."""

    def __init__(self, lines):
        self.payload = "".join(line + "\n" for line in lines).encode("utf-8")
        self.sent = None

    def __enter__(self):
        self._real = M.urllib.request.urlopen

        def fake(req, timeout=None):
            self.sent = json.loads(req.data.decode("utf-8"))
            return FakeResponse(self.payload)

        M.urllib.request.urlopen = fake
        OCR.urllib.request.urlopen = fake
        return self

    def __exit__(self, *a):
        M.urllib.request.urlopen = self._real
        OCR.urllib.request.urlopen = self._real
        return False


def ep(kind, **kw):
    base = {"name": kind, "kind": kind, "url": "https://example.invalid",
            "model": "test-model", "api_key": "k", "prompt_cache": False}
    base.update(kw)
    return base


REPLY = ("--- ALT TEXT ---\nA printed title page.\n\n"
         "--- TRANSCRIPTION ---\nLVX ET VERITAS\n\n"
         "--- NOTES ---\nUncertain: None.\n")


# ---------------------------------------------------------------------------
# 1. Usage capture, per provider, from captured stream shapes
# ---------------------------------------------------------------------------
class UsageParsing(unittest.TestCase):

    def test_anthropic(self):
        lines = [
            'data: {"type":"message_start","message":{"usage":'
            '{"input_tokens":1540,"cache_creation_input_tokens":0,'
            '"cache_read_input_tokens":700}}}',
            'data: {"type":"content_block_delta","delta":'
            '{"type":"text_delta","text":' + json.dumps(REPLY) + '}}',
            'data: {"type":"message_delta","usage":{"output_tokens":1266}}',
            'data: {"type":"message_stop"}',
        ]
        with StreamPatch(lines):
            text, usage = M.ai_generate(ep("anthropic"), "p", b"img")
        self.assertEqual(usage, {"in": 1540, "out": 1266, "think": 0, "cache_read": 700})
        self.assertIn("LVX ET VERITAS", text)

    def test_anthropic_cache_write_counts_as_input(self):
        lines = [
            'data: {"type":"message_start","message":{"usage":'
            '{"input_tokens":40,"cache_creation_input_tokens":860,'
            '"cache_read_input_tokens":0}}}',
            'data: {"type":"content_block_delta","delta":{"text":"x"}}',
            'data: {"type":"message_delta","usage":{"output_tokens":10}}',
            'data: {"type":"message_stop"}',
        ]
        with StreamPatch(lines):
            _t, usage = M.ai_generate(ep("anthropic"), "p", b"img")
        self.assertEqual(usage["in"], 900)
        self.assertEqual(usage["cache_read"], 0)

    def test_gemini_last_chunk_wins(self):
        lines = [
            'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]}}],'
            '"usageMetadata":{"promptTokenCount":1500,'
            '"candidatesTokenCount":12}}',
            'data: {"candidates":[{"content":{"parts":[{"text":" done"}]}}],'
            '"usageMetadata":{"promptTokenCount":1548,'
            '"candidatesTokenCount":1048}}',
        ]
        with StreamPatch(lines):
            text, usage = M.ai_generate(ep("gemini"), "p", b"img")
        self.assertEqual(usage, {"in": 1548, "out": 1048, "think": 0, "cache_read": 0})
        self.assertEqual(text, "partial done")

    def test_gemini_cached_tokens_are_taken_out_of_input(self):
        lines = [
            'data: {"candidates":[{"content":{"parts":[{"text":"x"}]}}],'
            '"usageMetadata":{"promptTokenCount":1548,'
            '"candidatesTokenCount":100,"cachedContentTokenCount":700}}',
        ]
        with StreamPatch(lines):
            _t, usage = M.ai_generate(ep("gemini"), "p", b"img")
        self.assertEqual(usage, {"in": 848, "out": 100, "think": 0, "cache_read": 700})

    def test_openai(self):
        lines = [
            'data: {"choices":[{"delta":{"content":"hello"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":1600,'
            '"completion_tokens":900,'
            '"prompt_tokens_details":{"cached_tokens":100}}}',
            'data: [DONE]',
        ]
        with StreamPatch(lines):
            text, usage = M.ai_generate(ep("openai"), "p", b"img")
        self.assertEqual(usage, {"in": 1500, "out": 900, "think": 0, "cache_read": 100})
        self.assertEqual(text, "hello")

    def test_provider_says_nothing(self):
        """No usage on the stream must be zeros, never a missing key."""
        lines = [
            'data: {"candidates":[{"content":{"parts":[{"text":"x"}]}}]}',
        ]
        with StreamPatch(lines):
            _t, usage = M.ai_generate(ep("gemini"), "p", b"img")
        self.assertEqual(usage, {"in": 0, "out": 0, "think": 0, "cache_read": 0})

    def test_non_streaming_ping_reports_zeros(self):
        body = json.dumps({"content": [{"type": "text", "text": "OK"}]})
        with StreamPatch([body]) as sp:
            # the non-streaming path reads the whole body, not SSE lines
            sp.payload = body.encode("utf-8")
            text, usage = M.ai_generate(ep("anthropic"), "p", stream=False)
        self.assertEqual(text, "OK")
        self.assertEqual(usage, {"in": 0, "out": 0, "think": 0, "cache_read": 0})

    def test_retry_wrapper_passes_usage_through(self):
        lines = [
            'data: {"candidates":[{"content":{"parts":[{"text":"x"}]}}],'
            '"usageMetadata":{"promptTokenCount":10,'
            '"candidatesTokenCount":2}}',
        ]
        with StreamPatch(lines):
            text, usage, tries = M.ai_generate_with_retry(
                ep("gemini"), "p", b"img", "image/png", 2,
                lambda s: None, lambda m: None)
        self.assertEqual((text, tries), ("x", 0))
        self.assertEqual(usage["out"], 2)


# ---------------------------------------------------------------------------
# 2. Request builders — max_tokens, usage opt-in, cache ordering
# ---------------------------------------------------------------------------
class RequestBuilders(unittest.TestCase):

    def test_gemini_sets_an_output_ceiling(self):
        _u, _h, body = M.build_gemini_request(ep("gemini"), "p", b"i",
                                              max_tokens=300)
        self.assertEqual(body["generationConfig"]["maxOutputTokens"], 300)

    def test_gemini_defaults_when_none_supplied(self):
        _u, _h, body = M.build_gemini_request(ep("gemini"), "p", b"i")
        self.assertEqual(body["generationConfig"]["maxOutputTokens"],
                         M.DEFAULT_MAX_TOKENS)

    def test_openai_must_ask_for_usage(self):
        _u, _h, body = M.build_openai_request(ep("openai"), "p", b"i",
                                              stream=True, max_tokens=300)
        self.assertEqual(body["stream_options"], {"include_usage": True})
        self.assertEqual(body["max_tokens"], 300)

    def test_anthropic_puts_the_image_first_by_default(self):
        _u, _h, body = M.build_anthropic_request(ep("anthropic"), "p", b"i")
        blocks = body["messages"][0]["content"]
        self.assertEqual([b["type"] for b in blocks], ["image", "text"])
        self.assertNotIn("cache_control", blocks[1])

    def test_anthropic_cache_flag_reorders_and_marks(self):
        _u, _h, body = M.build_anthropic_request(
            ep("anthropic", prompt_cache=True), "p", b"i")
        blocks = body["messages"][0]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text", "image"])
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})

    def test_profile_ceiling_reaches_the_wire(self):
        lines = ['data: {"candidates":[{"content":{"parts":'
                 '[{"text":"x"}]}}]}']
        entry = {"path": __file__, "name": "x.py", "rel": "x.py",
                 "ctx": "", "article": "", "title": "", "size": 1,
                 "admin_url": "", "sidecar": ""}
        profile = {"name": "Alt text only", "prompt": "p",
                   "max_alt_chars": 125, "max_tokens": 300}
        real_prepare = M.prepare_image
        M.prepare_image = lambda p, d, h: (b"img", "image/png", "")
        try:
            with StreamPatch(lines) as sp:
                M.describe_one(entry, ep("gemini"), profile, 2000, 0, False,
                               lambda s: None, lambda m: None)
            self.assertEqual(
                sp.sent["generationConfig"]["maxOutputTokens"], 300)
        finally:
            M.prepare_image = real_prepare


# ---------------------------------------------------------------------------
# 3. Price round-trip: Settings -> file -> module -> report cell
# ---------------------------------------------------------------------------
class RegistryRoundTrip(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._real_dir = SHELL.settings_mod.CONFIG_DIR
        SHELL.settings_mod.CONFIG_DIR = self.tmp

    def tearDown(self):
        SHELL.settings_mod.CONFIG_DIR = self._real_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_every_field_survives_all_three_whitelists(self):
        """normalize() and backfill_ai_config() in BOTH modules each rebuild
        an endpoint dict from named keys. A field missing from any one of
        them is dropped in silence, so this is tested end to end."""
        cand = {"endpoints": [dict(ep("anthropic"), name="Opus",
                                   prompt_cache=True)],
                "default": "Opus"}
        self.assertEqual(SHELL.validate(cand), "")
        SHELL.save(cand)
        raw = json.loads((self.tmp / "ai_endpoints.json")
                         .read_text(encoding="utf-8"))
        self.assertTrue(raw["endpoints"][0]["prompt_cache"])
        for mod in (M, OCR):
            got = mod.backfill_ai_config(raw)["endpoints"][0]
            self.assertTrue(got["prompt_cache"], mod.__name__)
            self.assertEqual(got["model"], "test-model", mod.__name__)

    def test_cost_estimation_is_gone(self):
        """Removed at Jeff's request once the model choice was settled: the
        run reports tokens, and nothing anywhere prices them."""
        for mod in (M, OCR, SHELL):
            self.assertFalse(hasattr(mod, "endpoint_cost"), mod.__name__)
            self.assertFalse(hasattr(mod, "CACHE_READ_RATE"), mod.__name__)
        saved = SHELL.normalize({"endpoints": [
            dict(ep("gemini"), name="G", price_in=0.3, price_out=2.5)],
            "default": "G"})["endpoints"][0]
        self.assertNotIn("price_in", saved)
        self.assertNotIn("price_out", saved)
        self.assertNotIn("Est. Cost ($)", M.REPORT_HEADERS)

    def test_token_columns_are_kept(self):
        for col in ("In Tok", "Out Tok", "Cached Tok"):
            self.assertIn(col, M.REPORT_HEADERS)


# ---------------------------------------------------------------------------
# 4. Prompt migration — both branches
# ---------------------------------------------------------------------------
class PromptMigration(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        M.PROFILE_PATH = self.tmp / "image_describer.json"
        M._set_prompt_notice("")

    def tearDown(self):
        M.PROFILE_PATH = None
        M._set_prompt_notice("")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, prompt):
        M.PROFILE_PATH.write_text(json.dumps({
            "profiles": [{"name": "Archival", "description": "d",
                          "prompt": prompt, "max_alt_chars": 125}],
            "default": "Archival"}), encoding="utf-8")

    def test_untouched_v12_profile_is_upgraded_and_saved(self):
        self._write(M.ARCHIVAL_PROMPT_V12)
        cfg = M.load_profiles()
        self.assertEqual(cfg["profiles"][0]["prompt"], M.ARCHIVAL_PROMPT)
        self.assertEqual(M.take_prompt_notice(), "")
        on_disk = json.loads(M.PROFILE_PATH.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["profiles"][0]["prompt"], M.ARCHIVAL_PROMPT)

    def test_one_character_of_difference_is_left_alone(self):
        edited = M.ARCHIVAL_PROMPT_V12 + "!"
        self._write(edited)
        cfg = M.load_profiles()
        self.assertEqual(cfg["profiles"][0]["prompt"], edited)
        self.assertEqual(M.take_prompt_notice(), M.STALE_PROMPT_NOTICE)

    def test_whitespace_only_difference_is_left_alone(self):
        edited = M.ARCHIVAL_PROMPT_V12.replace("\n\n--- NOTES ---",
                                               "\n\n \n--- NOTES ---")
        self.assertNotEqual(edited, M.ARCHIVAL_PROMPT_V12)
        self._write(edited)
        cfg = M.load_profiles()
        self.assertEqual(cfg["profiles"][0]["prompt"], edited)
        self.assertEqual(M.take_prompt_notice(), M.STALE_PROMPT_NOTICE)

    def test_an_already_current_profile_says_nothing(self):
        self._write(M.ARCHIVAL_PROMPT)
        M.load_profiles()
        self.assertEqual(M.take_prompt_notice(), "")

    def test_the_notice_is_handed_over_once(self):
        self._write(M.ARCHIVAL_PROMPT_V12 + "!")
        M.load_profiles()
        self.assertTrue(M.take_prompt_notice())
        self.assertEqual(M.take_prompt_notice(), "")

    def test_a_posted_registry_is_never_rewritten(self):
        """POST /api/profiles normalises through the same function, and must
        not swap a prompt out from under a save in progress."""
        cand = M.backfill_profiles({
            "profiles": [{"name": "Archival", "prompt":
                          M.ARCHIVAL_PROMPT_V12, "max_alt_chars": 125}],
            "default": "Archival"})
        self.assertEqual(cand["profiles"][0]["prompt"],
                         M.ARCHIVAL_PROMPT_V12)

    def test_the_frozen_prompt_is_not_the_new_one(self):
        self.assertNotEqual(M.ARCHIVAL_PROMPT_V12, M.ARCHIVAL_PROMPT)
        for phrase in ("word-by-word glossary", "you did not transcribe",
                       "Possible identification:"):
            self.assertIn(phrase, M.ARCHIVAL_PROMPT)
            self.assertNotIn(phrase, M.ARCHIVAL_PROMPT_V12)


# ---------------------------------------------------------------------------
# 5. Profiles: the reply ceiling
# ---------------------------------------------------------------------------
class ProfileCeiling(unittest.TestCase):

    def test_factory_profiles_carry_their_own_ceiling(self):
        by_name = {p["name"]: p for p in M.FACTORY_PROFILES}
        self.assertEqual(by_name["Archival"]["max_tokens"], 8000)
        self.assertEqual(by_name["Alt text only"]["max_tokens"], 300)

    def test_an_older_profile_is_backfilled(self):
        cfg = M.backfill_profiles({"profiles": [
            {"name": "Old", "prompt": "x" * 50 + "--- ALT TEXT ---",
             "max_alt_chars": 125}], "default": "Old"})
        self.assertEqual(cfg["profiles"][0]["max_tokens"],
                         M.DEFAULT_MAX_TOKENS)

    def test_a_factory_profile_is_backfilled_to_its_own_ceiling(self):
        """An untouched "Alt text only" saved before v1.3 must come back
        with 300, not the generic 4000 — that ceiling is the point of C4."""
        cfg = M.backfill_profiles({"profiles": [
            {"name": "Alt text only", "prompt": "--- ALT TEXT ---" + "z" * 40,
             "max_alt_chars": 125},
            {"name": "Mine", "prompt": "--- ALT TEXT ---" + "z" * 40,
             "max_alt_chars": 125}], "default": "Mine"})
        by_name = {p["name"]: p for p in cfg["profiles"]}
        self.assertEqual(by_name["Alt text only"]["max_tokens"], 300)
        self.assertEqual(by_name["Mine"]["max_tokens"], M.DEFAULT_MAX_TOKENS)

    def test_out_of_range_ceiling_is_refused(self):
        cfg = {"profiles": [{"name": "X", "prompt": "--- ALT TEXT ---" + "y" * 40,
                             "max_alt_chars": 125, "max_tokens": 9}],
               "default": "X"}
        self.assertIn("between 64 and 32000", M.validate_profiles(cfg))


# ---------------------------------------------------------------------------
# 6. Quality flags — median-relative, not absolute
# ---------------------------------------------------------------------------
def row(order, tchars, state="listed", status="Described", notes=True):
    return {"order": order, "tchars": tchars, "uncertain_state": state,
            "status": status, "has_notes": notes, "flags": ""}


class QualityFlags(unittest.TestCase):

    def test_a_uniformly_short_run_flags_nothing(self):
        """A folder of blank endpapers must not light up every row."""
        rows = [row(i, 40 + i) for i in range(1, 11)]
        n_short, _n_clean, _n_cut, median = M.apply_quality_flags(rows)
        self.assertEqual(n_short, 0)
        self.assertTrue(median)
        self.assertEqual([r["flags"] for r in rows], [""] * 10)

    def test_one_truncated_page_in_a_dense_run_flags(self):
        rows = [row(i, 1900) for i in range(1, 10)]
        rows.append(row(10, 300))          # 16% of the median
        n_short, _n, _c, _m = M.apply_quality_flags(rows)
        self.assertEqual(n_short, 1)
        self.assertIn("SHORT TRANSCRIPTION", rows[-1]["flags"])
        self.assertEqual(rows[0]["flags"], "")

    def test_a_row_just_above_the_ratio_is_left_alone(self):
        rows = [row(i, 1000) for i in range(1, 10)]
        rows.append(row(10, 401))          # 40.1% of the median
        n_short, _n, _c, _m = M.apply_quality_flags(rows)
        self.assertEqual(n_short, 0)

    def test_clean_read_on_a_long_transcription_is_flagged(self):
        rows = [row(1, 1800, state="none"), row(2, 1800, state="listed")]
        _n, n_clean, _c, _m = M.apply_quality_flags(rows)
        self.assertEqual(n_clean, 1)
        self.assertIn("CLAIMS CLEAN READ", rows[0]["flags"])
        self.assertEqual(rows[1]["flags"], "")

    def test_a_short_clean_read_is_not_suspicious(self):
        rows = [row(1, 120, state="none")] + [row(i, 120) for i in (2, 3)]
        _n, n_clean, _c, _m = M.apply_quality_flags(rows)
        self.assertEqual(n_clean, 0)

    def test_skipped_and_failed_rows_are_not_judged(self):
        rows = [row(1, 1800), row(2, 0, status="Skipped (sidecar exists)"),
                row(3, 0, status="Failed: boom")]
        M.apply_quality_flags(rows)
        self.assertEqual(rows[1]["flags"], "")
        self.assertEqual(rows[2]["flags"], "")

    def test_a_reply_that_never_wrote_its_notes_is_flagged(self):
        """The seven-page failure from the 2026-09-02 corpus run. These are
        the LONGEST rows, so nothing median-relative sees them, and they
        have no "Uncertain: None." to be suspicious of."""
        rows = [row(i, 1800) for i in range(1, 8)]
        rows.append(row(8, 6230, state="absent", notes=False))
        _n, _c, n_cut, _m = M.apply_quality_flags(rows)
        self.assertEqual(n_cut, 1)
        self.assertIn("INCOMPLETE REPLY", rows[-1]["flags"])
        self.assertNotIn("SHORT TRANSCRIPTION", rows[-1]["flags"])

    def test_notes_without_an_uncertain_line_is_also_incomplete(self):
        rows = [row(1, 1800), row(2, 6843, state="absent", notes=True)]
        _n, _c, n_cut, _m = M.apply_quality_flags(rows)
        self.assertEqual(n_cut, 1)
        self.assertIn("INCOMPLETE REPLY", rows[1]["flags"])

    def test_an_alt_text_only_row_is_never_called_incomplete(self):
        """No transcription means no NOTES is expected, so the structural
        flag must not fire on the Alt-text-only profile."""
        rows = [row(i, 0, state="absent", notes=False) for i in (1, 2, 3)]
        _n, _c, n_cut, _m = M.apply_quality_flags(rows)
        self.assertEqual(n_cut, 0)
        self.assertEqual([r["flags"] for r in rows], ["", "", ""])

    def test_uncertain_state_tells_none_from_absent(self):
        self.assertEqual(M.uncertain_state({"NOTES": "Uncertain: None."}),
                         "none")
        self.assertEqual(M.uncertain_state({"NOTES": "Uncertain: the date."}),
                         "listed")
        self.assertEqual(M.uncertain_state({"NOTES": "Title: X"}), "absent")


# ---------------------------------------------------------------------------
# 7. validate_start
# ---------------------------------------------------------------------------
class ValidateStart(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.img = self.tmp / "a.jpg"
        self.img.write_bytes(b"\xff\xd8\xff")
        self.model = [{"path": str(self.img), "name": "a.jpg", "rel": "a.jpg",
                       "ctx": "", "article": "", "title": "", "size": 3,
                       "admin_url": "", "sidecar": ""}]
        self.tools = {"openpyxl": True, "pymupdf": True}
        self.ai = {"endpoints": [ep("gemini", name="G")], "default": "G"}
        self.profiles = {"profiles": [dict(M.FACTORY_PROFILES[0])],
                         "default": "Archival"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def req(self, **kw):
        base = {"paths": [str(self.img)], "dest": "beside",
                "out_dir": str(self.tmp), "endpoint": "G",
                "profile": "Archival"}
        base.update(kw)
        return base

    def run_it(self, **kw):
        return M.validate_start(self.req(**kw), self.model, self.tools,
                                self.ai, self.profiles)

    def test_defaults_are_accepted(self):
        msg, cfg, _e, _p = self.run_it()
        self.assertEqual(msg, "")
        self.assertEqual(cfg["concurrency"], 1)
        self.assertEqual(cfg["max_tokens"], 8000)

    def test_concurrency_is_range_checked(self):
        self.assertIn("between 1 and 4", self.run_it(concurrency=5)[0])
        self.assertIn("between 1 and 4", self.run_it(concurrency=0)[0])
        self.assertEqual(self.run_it(concurrency=4)[0], "")

    def test_a_request_may_override_the_reply_ceiling(self):
        msg, cfg, _e, _p = self.run_it(max_tokens=512)
        self.assertEqual(msg, "")
        self.assertEqual(cfg["max_tokens"], 512)

    def test_the_reply_ceiling_is_range_checked(self):
        self.assertIn("between 64 and 32000", self.run_it(max_tokens=1)[0])
        self.assertIn("between 64 and 32000",
                      self.run_it(max_tokens=99999)[0])
        self.assertEqual(self.run_it(max_tokens=12000)[0], "")

    def test_a_broken_profile_ceiling_is_caught_before_the_run(self):
        self.profiles["profiles"][0]["max_tokens"] = 999999
        self.assertIn("Manage profiles", self.run_it()[0])


# ---------------------------------------------------------------------------
# 8. Report shape and the summary arithmetic
# ---------------------------------------------------------------------------
class ReportShape(unittest.TestCase):

    def test_headers_widths_and_row_values_agree(self):
        r = {k: "" for k in ("file", "folder", "ctx", "article", "title",
                             "pixels", "size", "alt", "alt_chars", "over",
                             "sections", "uncertain", "sidecar", "status",
                             "time", "admin_url", "flags")}
        r.update({"order": 1, "retries": 0, "in_tok": 5, "out_tok": 6,
                  "think_tok": 2, "cache_tok": 0, "tchars": 12})
        values = M._row_values(r)
        self.assertEqual(len(values), len(M.REPORT_HEADERS))
        self.assertEqual(len(M.REPORT_WIDTHS), len(M.REPORT_HEADERS))

    def test_the_admin_url_hyperlink_column_did_not_move(self):
        self.assertEqual(M.REPORT_HEADERS[M.REPORT_LINK_COLS[0] - 1],
                         "Admin Record URL")

    def test_a_report_writes_and_reopens(self):
        import openpyxl
        tmp = Path(tempfile.mkdtemp())
        try:
            path = tmp / "r.xlsx"
            r = {k: "" for k in ("file", "folder", "ctx", "article", "title",
                                 "pixels", "size", "alt", "alt_chars",
                                 "over", "sections", "uncertain", "sidecar",
                                 "status", "time", "admin_url")}
            r.update({"order": 1, "retries": 0, "in_tok": 1540,
                      "out_tok": 1048, "think_tok": 400, "cache_tok": 0,
                      "tchars": 1870, "flags": "CLAIMS CLEAN READ"})
            M.write_report(str(path), [M._row_values(r)],
                           [("Total output tokens", "81,744")], "1F4E79")
            wb = openpyxl.load_workbook(str(path))
            head = [c.value for c in wb["Images"][1]]
            self.assertEqual(head, M.REPORT_HEADERS)
            self.assertEqual(wb["Images"][2][len(M.REPORT_HEADERS) - 1]
                             .value, "CLAIMS CLEAN READ")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7b. Reasoning-trace accounting
# ---------------------------------------------------------------------------
class ThinkingTokens(unittest.TestCase):
    """The measurement whose absence turned a one-glance diagnosis into a
    two-day one. Gemini bills the reasoning trace as output and counts it
    against maxOutputTokens, but reports it outside candidatesTokenCount."""

    def test_gemini_output_includes_the_reasoning_trace(self):
        usage = M.normalize_usage("gemini", {
            "promptTokenCount": 1673, "candidatesTokenCount": 2180,
            "thoughtsTokenCount": 1800})
        self.assertEqual(usage["out"], 3980)     # 2,180 + 1,800
        self.assertEqual(usage["think"], 1800)
        # the arithmetic behind gallery157_1008: visible text well under the
        # ceiling, total output all but touching it
        self.assertLess(2180, 4000)
        self.assertGreater(usage["out"], 3900)

    def test_gemini_without_a_reasoning_figure_is_unchanged(self):
        usage = M.normalize_usage("gemini", {"promptTokenCount": 100,
                                             "candidatesTokenCount": 50})
        self.assertEqual((usage["out"], usage["think"]), (50, 0))

    def test_openai_reasoning_is_a_subset_not_an_addition(self):
        usage = M.normalize_usage("openai", {
            "prompt_tokens": 100, "completion_tokens": 900,
            "completion_tokens_details": {"reasoning_tokens": 400}})
        self.assertEqual(usage["out"], 900)      # NOT 1,300
        self.assertEqual(usage["think"], 400)

    def test_anthropic_reports_no_separate_figure(self):
        usage = M.normalize_usage("anthropic", {"input_tokens": 10,
                                                "output_tokens": 50})
        self.assertEqual((usage["out"], usage["think"]), (50, 0))

    def test_every_provider_returns_the_same_four_keys(self):
        for kind in M.AI_KINDS:
            self.assertEqual(set(M.normalize_usage(kind, None)),
                             {"in", "out", "think", "cache_read"})

    def test_it_reaches_the_report_row(self):
        self.assertIn("Think Tok", M.REPORT_HEADERS)
        self.assertEqual(
            M.REPORT_HEADERS.index("Think Tok"),
            M.REPORT_HEADERS.index("Out Tok") + 1)
        self.assertEqual(M.REPORT_HEADERS[M.REPORT_LINK_COLS[0] - 1],
                         "Admin Record URL")

    def test_it_survives_a_whole_streamed_request(self):
        lines = ['data: {"candidates":[{"content":{"parts":[{"text":"x"}]},'
                 '"finishReason":"STOP"}],"usageMetadata":'
                 '{"promptTokenCount":1673,"candidatesTokenCount":900,'
                 '"thoughtsTokenCount":1200}}']
        with StreamPatch(lines):
            _t, usage = M.ai_generate(ep("gemini"), "p", b"img")
        self.assertEqual(usage["think"], 1200)
        self.assertEqual(usage["out"], 2100)


# ---------------------------------------------------------------------------
# 8b. Early-stop detection — the 2026-09-02 defect
# ---------------------------------------------------------------------------
class EarlyStop(unittest.TestCase):
    """A provider can close the stream tidily AFTER giving up part-way. The
    v1.1 guard only caught connections that drop, so seven pages of the
    Jesuits corpus were saved mid-sentence and reported as successes."""

    def test_gemini_recitation_stop_is_refused(self):
        lines = ['data: {"candidates":[{"content":{"parts":'
                 '[{"text":"Petronii Arbitri Satyrici"}]},'
                 '"finishReason":"RECITATION"}],'
                 '"usageMetadata":{"promptTokenCount":1673,'
                 '"candidatesTokenCount":2218}}']
        with StreamPatch(lines):
            with self.assertRaises(M.AIError) as cm:
                M.ai_generate(ep("gemini"), "p", b"img")
        self.assertIn("stopped generating early", str(cm.exception))
        self.assertIn("recitation", str(cm.exception).lower())

    def test_gemini_max_tokens_stop_is_refused(self):
        lines = ['data: {"candidates":[{"content":{"parts":[{"text":"x"}]},'
                 '"finishReason":"MAX_TOKENS"}]}']
        with StreamPatch(lines):
            self.assertRaises(M.AIError, M.ai_generate,
                              ep("gemini"), "p", b"img")

    def test_gemini_normal_stop_is_kept(self):
        lines = ['data: {"candidates":[{"content":{"parts":'
                 '[{"text":"' + REPLY.replace("\n", "\\n") + '"}]},'
                 '"finishReason":"STOP"}]}']
        with StreamPatch(lines):
            text, _u = M.ai_generate(ep("gemini"), "p", b"img")
        self.assertIn("LVX ET VERITAS", text)

    def test_anthropic_max_tokens_stop_is_refused(self):
        lines = [
            'data: {"type":"message_start","message":{"usage":'
            '{"input_tokens":10}}}',
            'data: {"type":"content_block_delta","delta":{"text":"x"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":'
            '"max_tokens"},"usage":{"output_tokens":4000}}',
            'data: {"type":"message_stop"}',
        ]
        with StreamPatch(lines):
            self.assertRaises(M.AIError, M.ai_generate,
                              ep("anthropic"), "p", b"img")

    def test_anthropic_end_turn_is_kept(self):
        lines = [
            'data: {"type":"message_start","message":{"usage":'
            '{"input_tokens":10}}}',
            'data: {"type":"content_block_delta","delta":{"text":"done"}}',
            'data: {"type":"message_delta","delta":{"stop_reason":'
            '"end_turn"},"usage":{"output_tokens":2}}',
            'data: {"type":"message_stop"}',
        ]
        with StreamPatch(lines):
            text, usage = M.ai_generate(ep("anthropic"), "p", b"img")
        self.assertEqual(text, "done")
        self.assertEqual(usage["out"], 2)

    def test_openai_length_stop_is_refused(self):
        lines = ['data: {"choices":[{"delta":{"content":"x"},'
                 '"finish_reason":"length"}]}',
                 'data: [DONE]']
        with StreamPatch(lines):
            self.assertRaises(M.AIError, M.ai_generate,
                              ep("openai"), "p", b"img")

    def test_a_silent_provider_still_passes(self):
        """No finish reason at all must not become a failure — that is the
        ordinary shape of a Gemini chunk mid-stream."""
        lines = ['data: {"candidates":[{"content":{"parts":'
                 '[{"text":"x"}]}}]}']
        with StreamPatch(lines):
            text, _u = M.ai_generate(ep("gemini"), "p", b"img")
        self.assertEqual(text, "x")

    def test_an_early_stop_is_not_retried(self):
        """Deterministic: retrying a length or recitation stop four times
        only spends the backoff. The row fails with the reason named."""
        lines = ['data: {"candidates":[{"content":{"parts":[{"text":"x"}]},'
                 '"finishReason":"SAFETY"}]}']
        slept = []
        with StreamPatch(lines):
            with self.assertRaises(M.AIError):
                M.ai_generate_with_retry(
                    ep("gemini"), "p", b"img", "image/png", 4,
                    slept.append, lambda m: None)
        self.assertEqual(slept, [])

    def test_both_modules_agree_on_what_a_normal_stop_is(self):
        self.assertEqual(M._NORMAL_STOP, OCR._NORMAL_STOP)
        for kind, obj in (
                ("gemini", {"candidates": [{"finishReason": "RECITATION"}]}),
                ("anthropic", {"type": "message_delta",
                               "delta": {"stop_reason": "max_tokens"}}),
                ("openai", {"choices": [{"finish_reason": "length"}]}),
                ("gemini", {"candidates": [{}]})):
            self.assertEqual(M.stop_reason(kind, obj),
                             OCR.stop_reason(kind, obj))

    def test_the_ocr_client_refuses_an_early_stop_too(self):
        lines = ['data: {"candidates":[{"content":{"parts":[{"text":"x"}]},'
                 '"finishReason":"MAX_TOKENS"}]}']
        with StreamPatch(lines):
            self.assertRaises(OCR.AIError, OCR.ai_generate,
                              ep("gemini"), "p", b"img")


# ---------------------------------------------------------------------------
# 9. Client drift between the two AI modules
# ---------------------------------------------------------------------------
class ClientDrift(unittest.TestCase):

    def test_both_modules_declare_the_same_client(self):
        self.assertEqual(M.AI_CLIENT_VERSION, "1.4")
        self.assertEqual(M.AI_CLIENT_VERSION, OCR.AI_CLIENT_VERSION)

    def test_the_usage_normaliser_is_the_same_in_both(self):
        for kind, payload in (
                ("anthropic", {"input_tokens": 5, "output_tokens": 6,
                               "cache_read_input_tokens": 7}),
                ("gemini", {"promptTokenCount": 5,
                            "candidatesTokenCount": 6}),
                ("openai", {"prompt_tokens": 5, "completion_tokens": 6,
                            "completion_tokens_details":
                                {"reasoning_tokens": 2}}),
                ("gemini", {"candidatesTokenCount": 5,
                            "thoughtsTokenCount": 9}),
                ("openai", None)):
            self.assertEqual(M.normalize_usage(kind, payload),
                             OCR.normalize_usage(kind, payload))

    def test_the_ocr_client_returns_usage_too(self):
        lines = ['data: {"candidates":[{"content":{"parts":'
                 '[{"text":"x"}]}}],"usageMetadata":'
                 '{"promptTokenCount":9,"candidatesTokenCount":3}}']
        with StreamPatch(lines):
            text, usage = OCR.ai_generate(ep("gemini"), "p", b"img")
        self.assertEqual(text, "x")
        self.assertEqual(usage, {"in": 9, "out": 3, "think": 0, "cache_read": 0})

    def test_alt_usage_goes_to_the_sink_not_the_return(self):
        """The OCR module's usage boundary, pinned from this side.

        Renamed at v1.9. As `..._still_return_plain_text` this asserted only
        half the contract — that the return type had not changed — which was
        true both before usage was captured at all and after. A refactor
        could have dropped the capture entirely and left it green.

        The asymmetry it guards is deliberate: ai_ocr_page returns usage to
        an AI-aware caller, while ai_alt_text keeps a bare string and writes
        into the ai_ctx sink, so tag_pdf and the docx/pptx processors never
        have to know about tokens. Both halves are asserted here."""
        lines = ['data: {"candidates":[{"content":{"parts":'
                 '[{"text":"A stone bridge."}]}}],"usageMetadata":'
                 '{"promptTokenCount":11,"candidatesTokenCount":7}}']
        sink = {}
        with StreamPatch(lines):
            alt = OCR.ai_alt_text(ep("gemini"), b"img", "image/png",
                                  retries=0, sink=sink)
        self.assertEqual(alt, "A stone bridge.")
        self.assertIsInstance(alt, str)
        self.assertEqual(sink["usage"], {"in": 11, "out": 7, "think": 0,
                                         "cache_read": 0})


# ---------------------------------------------------------------------------
# 10. Provider-aware sizing is offered, not applied
# ---------------------------------------------------------------------------
class RecommendedSizing(unittest.TestCase):

    def test_the_shipped_default_is_unchanged(self):
        self.assertEqual(M.DEFAULT_MAX_DIM, 2000)

    def test_every_endpoint_kind_has_a_recommendation(self):
        for kind in M.AI_KINDS:
            self.assertIn(kind, M.RECOMMENDED_MAX_DIM)
        self.assertEqual(M.RECOMMENDED_MAX_DIM["gemini"], 1536)

    def test_the_recommendation_reaches_the_page(self):
        html = M.build_page({"branding": {}}).decode("utf-8")
        self.assertNotIn("__RECDIM__", html)
        self.assertIn('"gemini": 1536', html.replace("'", '"'))


# ---------------------------------------------------------------------------
# 11. Duplicate profile (2026-10-02): copy a profile, then edit it
# ---------------------------------------------------------------------------
PROFILE_JS_HARNESS = r"""
const els = {};
function mk(id){ return {id:id, value:'', checked:false, textContent:'',
  style:{display:''}, hidden:false, listeners:{}, children:[],
  addEventListener(t,f){ this.listeners[t]=f; }, focus(){}, select(){},
  append(...c){ this.children.push(...c); }, className:'' }; }
function $(id){ return els[id] || (els[id] = mk(id)); }
const document = {activeElement:null, createElement(t){ return mk(t); }};
let saved = [], said = [];
let refuse = '';
async function api(path, body){
  if (refuse) return {error: refuse};
  saved.push(JSON.parse(JSON.stringify(body)));
  return {ok:true, config: body}; }
function showOk(m){ said.push('ok:'+m); } function showErr(m){ said.push('err:'+m); }
function clearErr(){} function closeModal(){ said.push('closed'); }
function confirm(){ return true; }
let profIndex=-1, lastFocus=null;
const LONG = 'Describe the image. --- ALT TEXT --- then the alt text, briefly.';
let profCfg = {profiles:[
  {name:'Archival', description:'full', prompt:LONG, max_alt_chars:125, max_tokens:8000},
  {name:'archival (COPY)', description:'taken', prompt:LONG, max_alt_chars:125, max_tokens:300}],
  'default':'Archival'};
__SECTION__
(async function(){
  const out = {};
  openProf(0);
  out.dupShownForAnExisting = $('p_dup').style.display;
  $('p_prompt').value = LONG + ' EDITED';
  $('p_default').checked = true;
  $('p_dup').listeners.click();
  out.name = $('p_name').value;
  out.defaultBox = $('p_default').checked;
  out.title = $('ptitle').textContent;
  out.deleteShown = $('p_delete').style.display;
  out.savedBeforeSave = saved.length;
  await $('p_save').listeners.click();
  const c = saved[saved.length - 1];
  out.count = c.profiles.length;
  out.original = c.profiles[0].prompt === LONG;
  out.copy = c.profiles[2];
  out.def = c['default'];
  /* Delete asks first (2026-10-02). Keep saves nothing; Delete saves the
     list without that profile. */
  openProf(1);
  const before = saved.length;
  $('p_delete').listeners.click();
  out.askShown = !$('p_delask').hidden;
  out.savedOnFirstClick = saved.length - before;
  $('p_delno').listeners.click();
  out.askHiddenAfterKeep = $('p_delask').hidden;
  out.savedAfterKeep = saved.length - before;
  $('p_delete').listeners.click();
  await $('p_delyes').listeners.click();
  out.afterDelete = saved.length > before ?
    saved[saved.length - 1].profiles.map(p => p.name) : null;
  /* Asked and not answered, then another profile opened: the question
     must not carry over. (Answering it first would hide it anyway, which
     is how the first version of this check passed with the line gone.) */
  openProf(1);
  $('p_delete').listeners.click();
  openProf(0);
  out.askHiddenOnOpen = $('p_delask').hidden;
  /* A Save the module refuses answers IN the dialog, which stays open;
     the page callout behind it is not used (2026-10-02). */
  said.length = 0;
  openProf(0);
  $('pback').style.display = 'flex';
  refuse = "Profile 'Archival': the prompt is too short to be useful.";
  await $('p_save').listeners.click();
  out.dialogErr = $('p_err').hidden ? '' : $('p_err').textContent;
  out.calloutUsed = said.filter(m => m.indexOf('err:') === 0).length;
  out.closedOnRefusal = said.indexOf('closed') >= 0;
  refuse = '';
  openProf(0);
  out.errClearedOnOpen = $('p_err').hidden;
  /* Shown again first: a stub left at 'none' by the click above would
     hide the very thing this asks (found by planting). */
  $('p_dup').style.display = '';
  openProf(-1);
  out.dupShownForANew = $('p_dup').style.display;
  console.log(JSON.stringify(out));
})();
"""


class ProfileDuplicate(unittest.TestCase):
    """Driven in node: the page's own profile code against a stub DOM.

    The section between the page's "profiles" and "endpoints" markers is
    run as it is served. A copy takes the form's current values, gets the
    first free name ignoring case (the module refuses a duplicate name
    case-insensitively), is never the default, and leaves the profile it
    came from as it was saved.
    """

    def setUp(self):
        self.node = shutil.which("node")
        if not self.node:
            self.skipTest("node is not on PATH")

    def run_page(self):
        import re
        import subprocess
        html = M.build_page({"branding": {}}).decode("utf-8")
        script = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        a = script.index("/* ---- profiles ---")
        b = script.index("/* ---- endpoints ---", a)
        js = PROFILE_JS_HARNESS.replace("__SECTION__", script[a:b])
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(js)
            tmp = fh.name
        try:
            done = subprocess.run([self.node, tmp], capture_output=True,
                                  text=True, encoding="utf-8",
                                  errors="replace", timeout=30)
        finally:
            os.unlink(tmp)
        self.assertEqual(done.returncode, 0, done.stderr[-800:])
        return json.loads(done.stdout.strip().splitlines()[-1])

    def test_a_copy_is_a_new_profile_with_a_free_name(self):
        out = self.run_page()
        self.assertEqual(out["dupShownForAnExisting"], "")
        self.assertEqual(out["dupShownForANew"], "none")
        self.assertEqual(out["name"], "Archival (copy 2)")
        self.assertFalse(out["defaultBox"])
        self.assertIn("not saved yet", out["title"])
        self.assertEqual(out["deleteShown"], "none")
        self.assertEqual(out["savedBeforeSave"], 0)

    def test_saving_the_copy_keeps_the_original_and_the_default(self):
        out = self.run_page()
        self.assertEqual(out["count"], 3)
        self.assertTrue(out["original"], "the original's prompt changed")
        self.assertEqual(out["copy"]["name"], "Archival (copy 2)")
        self.assertTrue(out["copy"]["prompt"].endswith(" EDITED"))
        self.assertEqual(out["copy"]["max_tokens"], 8000)
        self.assertEqual(out["def"], "Archival")

    def test_delete_asks_first_and_keep_keeps(self):
        out = self.run_page()
        self.assertTrue(out["askShown"], "Delete did not ask")
        self.assertEqual(out["savedOnFirstClick"], 0)
        self.assertTrue(out["askHiddenAfterKeep"])
        self.assertEqual(out["savedAfterKeep"], 0)
        self.assertEqual(out["afterDelete"], ["Archival", "Archival (copy 2)"])
        self.assertTrue(out["askHiddenOnOpen"])

    def test_a_refused_save_answers_inside_the_dialog(self):
        out = self.run_page()
        self.assertIn("too short", out["dialogErr"])
        self.assertEqual(out["calloutUsed"], 0)
        self.assertFalse(out["closedOnRefusal"])
        self.assertTrue(out["errClearedOnOpen"])

    def test_the_module_accepts_what_the_page_saves(self):
        cand = M.backfill_profiles({"profiles": [
            dict(p) for p in M.FACTORY_PROFILES] + [
            dict(M.FACTORY_PROFILES[0], name=M.FACTORY_PROFILES[0]["name"]
                 + " (copy)")], "default": "Archival"})
        self.assertEqual(M.validate_profiles(cand), "")
        cand["profiles"][-1]["name"] = cand["profiles"][0]["name"].upper()
        self.assertIn("Duplicate profile name", M.validate_profiles(cand))


if __name__ == "__main__":
    # Say which version is under test, read from the module rather than from
    # this file's name. Nothing here hard-codes a version.
    print("Image Description Generator v{} — verification".format(M.MANIFEST["version"]))
    unittest.main(verbosity=2)
