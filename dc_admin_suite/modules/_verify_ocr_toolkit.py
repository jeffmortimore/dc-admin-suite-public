"""Verification suite for OCR & Accessibility Toolkit.

Run it from the suite root with the same Python that launches the suite:

    python3 modules/_verify_ocr_toolkit.py

The leading underscore keeps this file out of the module scanner. That is
not cosmetic: CI asserts the scan finds exactly ten modules, so a name
without it fails the build with a confusing error instead of an obvious one.

What is covered, and why each one is here rather than left to a real run:

  · max_tokens reaching the request body, for all three providers. This is
    the actual defect of this release — the client accepted the parameter
    from v1.2 and no caller in this module ever passed one, so every request
    silently rode the 4,000 default. Nothing about that is visible by eye;
  · both usage routes, because the split is deliberate and asymmetric.
    ai_ocr_page returns usage; ai_alt_text must keep returning a bare string
    and write into the ai_ctx sink, so the tagger stays ignorant of tokens.
    A test that pins only one half would let a refactor drop the other;
  · page isolation, which is a data-loss fix rather than instrumentation:
    before v1.9 one refused page discarded every completed page in the file,
    and so did a Stop;
  · the systemic guards, because per-page isolation is right per page and
    wrong in aggregate — forty-five refusals is a dead key, not forty-five
    page problems;
  · NO TRANSCRIPTION and INCOMPLETE REPLY staying distinct, and the latter
    keeping the image module's exact meaning. Two modules whose reports are
    read side by side must not use one name for two things; that is the
    report-vocabulary form of the client drift the other suite guards;
  · the raster clamp, which changes the ordinary page and not just the
    pathological one, so it is worth proving it engages where intended;
  · the client drift check, unchanged and still green.
"""

import io
import json
import os
import sys
import tempfile
import threading
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import dc_ocr_toolkit as M              # noqa: E402
import dc_image_describer as IMG        # noqa: E402


# ---------------------------------------------------------------------------
# A fake urlopen that replays a captured SSE stream, capturing what was sent
# so the request builders can be asserted on too. Same shape as the image
# module's suite, deliberately.
# ---------------------------------------------------------------------------
class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False


class StreamPatch(object):
    def __init__(self, lines):
        self.payload = "".join(line + "\n" for line in lines).encode("utf-8")
        self.sent = None

    def __enter__(self):
        self._real = M.urllib.request.urlopen

        def fake(req, timeout=None):
            self.sent = json.loads(req.data.decode("utf-8"))
            return FakeResponse(self.payload)

        M.urllib.request.urlopen = fake
        return self

    def __exit__(self, *a):
        M.urllib.request.urlopen = self._real
        return False


def ep(kind, **kw):
    base = {"name": kind, "kind": kind, "url": "https://example.invalid",
            "model": "test-model", "api_key": "k", "prompt_cache": False}
    base.update(kw)
    return base


PAGE_TEXT = "LVX ET VERITAS\nab urbe condita\nUncertain: None."

GEMINI_LINES = ['data: {"candidates":[{"content":{"parts":[{"text":'
                + json.dumps(PAGE_TEXT) +
                '}]},"finishReason":"STOP"}],"usageMetadata":'
                '{"promptTokenCount":975,"candidatesTokenCount":600,'
                '"thoughtsTokenCount":400}}']


# ---------------------------------------------------------------------------
# 1. The reply ceiling reaches the wire
# ---------------------------------------------------------------------------
class RequestCeilings(unittest.TestCase):

    def test_gemini_builder_carries_the_ceiling(self):
        _u, _h, body = M.build_gemini_request(ep("gemini"), "p", b"i",
                                              max_tokens=M.AI_OCR_MAX_TOKENS)
        self.assertEqual(body["generationConfig"]["maxOutputTokens"],
                         M.AI_OCR_MAX_TOKENS)

    def test_openai_builder_carries_the_ceiling_and_asks_for_usage(self):
        _u, _h, body = M.build_openai_request(ep("openai"), "p", b"i",
                                              stream=True,
                                              max_tokens=M.AI_OCR_MAX_TOKENS)
        self.assertEqual(body["max_tokens"], M.AI_OCR_MAX_TOKENS)
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_anthropic_builder_carries_the_ceiling(self):
        _u, _h, body = M.build_anthropic_request(
            ep("anthropic"), "p", b"i", max_tokens=M.AI_OCR_MAX_TOKENS)
        self.assertEqual(body["max_tokens"], M.AI_OCR_MAX_TOKENS)

    def test_ocr_ceiling_reaches_the_wire(self):
        """The defect itself: no caller used to pass one."""
        with StreamPatch(GEMINI_LINES) as sp:
            M.ai_ocr_page(ep("gemini"), b"img", retries=0)
        self.assertEqual(sp.sent["generationConfig"]["maxOutputTokens"],
                         M.AI_OCR_MAX_TOKENS)

    def test_alt_ceiling_reaches_the_wire_and_differs(self):
        with StreamPatch(GEMINI_LINES) as sp:
            M.ai_alt_text(ep("gemini"), b"img", "image/png", retries=0)
        self.assertEqual(sp.sent["generationConfig"]["maxOutputTokens"],
                         M.AI_ALT_MAX_TOKENS)
        self.assertNotEqual(M.AI_ALT_MAX_TOKENS, M.AI_OCR_MAX_TOKENS)

    def test_the_ceiling_is_not_the_old_silent_default(self):
        self.assertEqual(M.AI_OCR_MAX_TOKENS, 8000)
        self.assertNotEqual(M.AI_OCR_MAX_TOKENS, M.DEFAULT_MAX_TOKENS)


# ---------------------------------------------------------------------------
# 2. Usage travels by two routes, and both are pinned
# ---------------------------------------------------------------------------
class UsageRoutes(unittest.TestCase):

    def test_ocr_page_returns_usage_by_value(self):
        with StreamPatch(GEMINI_LINES):
            text, usage, tries, stop = M.ai_ocr_page(ep("gemini"), b"i",
                                                     retries=0)
        self.assertIn("LVX ET VERITAS", text)
        # Gemini reports thinking outside candidatesTokenCount, so "out" is
        # the sum — that sum is what the ceiling applies to.
        self.assertEqual(usage, {"in": 975, "out": 1000, "think": 400,
                                 "cache_read": 0})
        self.assertEqual((tries, stop), (0, None))

    def test_alt_text_returns_a_bare_string_and_fills_the_sink(self):
        """The boundary: the tagger must never see a token."""
        sink = M.new_ai_ctx({"ai_retries": 0}, ep("gemini"))
        with StreamPatch(GEMINI_LINES):
            alt = M.ai_alt_text(ep("gemini"), b"i", "image/png", retries=0,
                                sink=sink)
        self.assertIsInstance(alt, str)
        self.assertEqual(sink["usage"]["out"], 1000)
        self.assertEqual(sink["usage"]["think"], 400)

    def test_a_provider_that_reports_nothing_gives_zeros_not_errors(self):
        lines = ['data: {"candidates":[{"content":{"parts":'
                 '[{"text":"x"}]},"finishReason":"STOP"}]}']
        with StreamPatch(lines):
            _t, usage, _r, _s = M.ai_ocr_page(ep("gemini"), b"i", retries=0)
        self.assertEqual(usage, {"in": 0, "out": 0, "think": 0,
                                 "cache_read": 0})

    def test_the_sink_accumulates_across_requests(self):
        sink = {}
        M.add_usage(sink, {"in": 5, "out": 6, "think": 2, "cache_read": 1})
        M.add_usage(sink, {"in": 5, "out": 4})
        self.assertEqual(sink["usage"], {"in": 10, "out": 10, "think": 2,
                                         "cache_read": 1})

    def test_an_empty_dict_sink_is_not_mistaken_for_no_sink(self):
        sink = {}
        M.add_usage(sink, {"in": 3})
        self.assertEqual(sink["usage"]["in"], 3)


# ---------------------------------------------------------------------------
# 3. The coverage line, read back as a tri-state
# ---------------------------------------------------------------------------
class UncertainParsing(unittest.TestCase):

    def test_named_zones_are_listed(self):
        got = M.parse_ocr_reply("Body text\nUncertain: foot of page faint.")
        self.assertEqual(got["state"], "listed")
        self.assertEqual(got["text"], "Body text")
        self.assertIn("faint", got["uncertain"])

    def test_an_explicit_none_is_a_claim(self):
        got = M.parse_ocr_reply("Body text\nUncertain: None.")
        self.assertEqual(got["state"], "none")
        self.assertEqual(got["uncertain"], "")

    def test_a_missing_line_is_absent_and_the_text_survives(self):
        """Tolerate means do not crash and do not discard — not do not
        notice. An absent line is the silent-truncation signal."""
        got = M.parse_ocr_reply("Body text that just stops mid-sen")
        self.assertEqual(got["state"], "absent")
        self.assertEqual(got["text"], "Body text that just stops mid-sen")

    def test_case_and_spacing_are_tolerated(self):
        self.assertEqual(M.parse_ocr_reply("t\nuncertain :  NONE")["state"],
                         "none")

    def test_a_malformed_empty_line_is_not_a_clean_read(self):
        self.assertEqual(M.parse_ocr_reply("t\nUncertain:")["state"],
                         "listed")

    def test_the_last_line_wins(self):
        """The word can appear inside a transcription; the declaration is
        the reply's final line."""
        got = M.parse_ocr_reply("quoting 'Uncertain: everything' here\n"
                                "Uncertain: None.")
        self.assertEqual(got["state"], "none")


# ---------------------------------------------------------------------------
# 4. Per-page triage flags
# ---------------------------------------------------------------------------
def page(num, chars, state="listed", refused=None):
    return {"page": num, "chars": chars, "state": state, "refused": refused}


class QualityFlags(unittest.TestCase):

    def test_a_uniformly_short_file_flags_nothing(self):
        """A volume of blank plates must not light up every page."""
        pages = [page(i, 40 + i) for i in range(1, 11)]
        flags, counts = M.page_flags(pages)
        self.assertEqual(flags, "")
        self.assertEqual(counts["SHORT PAGE"], 0)

    def test_one_truncated_page_in_a_dense_file_flags(self):
        pages = [page(i, 1900) for i in range(1, 10)] + [page(10, 300)]
        flags, counts = M.page_flags(pages)
        self.assertEqual(counts["SHORT PAGE"], 1)
        self.assertIn("SHORT PAGE p10", flags)

    def test_a_page_just_above_the_ratio_is_left_alone(self):
        pages = [page(i, 1000) for i in range(1, 10)] + [page(10, 401)]
        _f, counts = M.page_flags(pages)
        self.assertEqual(counts["SHORT PAGE"], 0)

    def test_the_median_is_this_file_not_the_run(self):
        dense = M.page_flags([page(i, 2000) for i in range(1, 6)]
                             + [page(6, 100)])[1]
        sparse = M.page_flags([page(i, 100) for i in range(1, 7)])[1]
        self.assertEqual(dense["SHORT PAGE"], 1)
        self.assertEqual(sparse["SHORT PAGE"], 0)

    def test_a_refused_page_is_named_with_its_reason(self):
        flags, counts = M.page_flags([page(1, 1200), page(2, 1200),
                                      page(31, 0, refused="max_tokens")])
        self.assertEqual(counts["NO TRANSCRIPTION"], 1)
        self.assertIn("NO TRANSCRIPTION p31 (max_tokens)", flags)

    def test_the_two_failure_flags_stay_distinct(self):
        """Different remedies: a named stop is deterministic, a missing
        coverage line often clears on a plain re-run."""
        flags, counts = M.page_flags([page(1, 900, refused="recitation"),
                                      page(2, 900, state="absent")])
        self.assertIn("NO TRANSCRIPTION p1 (recitation)", flags)
        self.assertIn("INCOMPLETE REPLY p2", flags)
        self.assertEqual(counts["NO TRANSCRIPTION"], 1)
        self.assertEqual(counts["INCOMPLETE REPLY"], 1)

    def test_a_refused_page_does_not_also_claim_incompleteness(self):
        _f, counts = M.page_flags([page(9, 0, state="absent",
                                        refused="max_tokens")])
        self.assertEqual(counts["INCOMPLETE REPLY"], 0)

    def test_a_page_with_no_text_flags_nothing(self):
        """[NO TEXT] and blank plates claim nothing, so nothing is doubted."""
        flags, counts = M.page_flags([page(1, 0, state="absent"),
                                      page(2, 0, state="absent")])
        self.assertEqual(flags, "")
        self.assertEqual(counts["INCOMPLETE REPLY"], 0)

    def test_claims_clean_read_needs_length_and_an_explicit_none(self):
        _f, counts = M.page_flags([page(1, 1200, state="none"),
                                   page(2, 900, state="none"),
                                   page(3, 1200, state="listed")])
        self.assertEqual(counts["CLAIMS CLEAN READ"], 1)

    def test_incomplete_reply_keeps_the_image_modules_meaning(self):
        """Report-vocabulary drift is the same hazard as client drift.

        The describer fires on `tchars and (not has_notes or
        uncertain_state == "absent")`. This module's reply has no NOTES
        section to be missing — it is a transcription plus one final line —
        so the clause reduces to its second half. Equivalence is asserted on
        the inputs the two formats share, not by textual identity."""
        cases = [(1500, "absent", True), (1500, "listed", False),
                 (1500, "none", False), (0, "absent", False)]
        for chars, state, expected in cases:
            mine = M.page_flags([page(1, chars, state=state)])[1]
            theirs = IMG.apply_quality_flags(
                [{"order": 1, "tchars": chars, "uncertain_state": state,
                  "status": "Described", "has_notes": True, "flags": ""}])[2]
            self.assertEqual(bool(mine["INCOMPLETE REPLY"]), expected)
            self.assertEqual(bool(theirs), expected,
                             "describer disagrees for %r/%r" % (chars, state))

    def test_flags_are_grouped_so_a_page_list_reads_as_one_flag(self):
        flags, _c = M.page_flags([page(1, 900, state="absent"),
                                  page(2, 900, state="absent")])
        self.assertEqual(flags, "INCOMPLETE REPLY p1, p2")


# ---------------------------------------------------------------------------
# 5. Page isolation and the systemic guards
# ---------------------------------------------------------------------------
class FakePage(object):
    def __init__(self):
        self.rect = types.SimpleNamespace(width=612.0, height=792.0)


class FakeDoc(object):
    def __init__(self, count):
        self.page_count = count
        self.closed = False

    def __getitem__(self, i):
        return FakePage()

    def close(self):
        self.closed = True


class PageIsolation(unittest.TestCase):
    """Before v1.9 the sidecar write sat after the loop, so one AIError on
    page 31 of 50 discarded thirty completed transcriptions — and a Stop did
    exactly the same thing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sidecar = os.path.join(self.tmp, "out_ai_text.txt")
        self._fitz = sys.modules.get("fitz")
        self._raster = M._raster_bytes
        self.pages = 5
        sys.modules["fitz"] = types.SimpleNamespace(
            open=lambda path: FakeDoc(self.pages),
            Matrix=lambda a, b: None, Pixmap=lambda *a: None, csRGB=None)
        M._raster_bytes = lambda pg: (b"img", "image/png")

    def tearDown(self):
        M._raster_bytes = self._raster
        if self._fitz is None:
            sys.modules.pop("fitz", None)
        else:
            sys.modules["fitz"] = self._fitz

    def _run(self, replies, sink=None, check=lambda: None, concurrency=1):
        calls = {"n": 0}

        def fake_page(endpoint, data, mime="image/png", retries=0):
            idx = calls["n"]
            calls["n"] += 1
            outcome = replies[idx] if idx < len(replies) else "ok"
            if isinstance(outcome, Exception):
                raise outcome
            # Well-formed by default: a reply without a coverage line is a
            # genuine INCOMPLETE REPLY, and a fixture that omits one would
            # be testing the flag rather than the thing under test.
            if "Uncertain:" not in outcome:
                outcome = outcome + "\nUncertain: None."
            return outcome, {"in": 10, "out": 20, "think": 5,
                             "cache_read": 0}, 0, None

        real = M.ai_ocr_page
        M.ai_ocr_page = fake_page
        try:
            M.ai_ocr_pdf_sidecar(ep("gemini"), "x.pdf", self.sidecar,
                                 lambda m: None, check, retries=0,
                                 sink=sink, concurrency=concurrency)
        finally:
            M.ai_ocr_page = real
        return Path(self.sidecar).read_text(encoding="utf-8")

    def _refusal(self, reason="max_tokens"):
        return M.AIError("The endpoint stopped generating early after 900 "
                         "characters (the provider gave the reason '%s'), "
                         "so the reply is incomplete" % reason)

    def test_a_refused_page_does_not_discard_the_others(self):
        sink = M.new_ai_ctx({"ai_retries": 0}, ep("gemini"))
        text = self._run(["p1", "p2", self._refusal(), "p4", "p5"],
                         sink=sink)
        for expected in ("p1", "p2", "p4", "p5"):
            self.assertIn(expected, text)
        self.assertIn("NO TRANSCRIPTION", text)
        self.assertIn("max_tokens", text)
        self.assertEqual(len(sink["pages"]), 5)

    def test_the_placeholder_marks_the_hole_where_it_is(self):
        text = self._run(["p1", self._refusal("recitation"), "p3"])
        block = text.split("\n\n")[1]
        self.assertTrue(block.startswith("--- Page 2 ---"))
        self.assertIn("recitation", block)

    def test_a_stop_still_writes_what_was_already_transcribed(self):
        state = {"n": 0}

        def check():
            state["n"] += 1
            if state["n"] > 3:
                raise M.StopRequested()

        with self.assertRaises(M.StopRequested):
            self._run(["p1", "p2", "p3"], check=check)
        text = Path(self.sidecar).read_text(encoding="utf-8")
        self.assertIn("p1", text)
        self.assertIn("p3", text)
        self.assertNotIn("NO TRANSCRIPTION", text)

    def test_a_stop_is_never_reported_as_a_flagged_page(self):
        """A bare `except Exception` would have turned Stop into a flag."""
        sink = M.new_ai_ctx({"ai_retries": 0}, ep("gemini"))
        state = {"n": 0}

        def check():
            state["n"] += 1
            if state["n"] > 2:
                raise M.StopRequested()

        with self.assertRaises(M.StopRequested):
            self._run(["p1", "p2"], sink=sink, check=check)
        self.assertEqual(M.page_flags(sink["pages"])[0], "")

    def test_a_401_aborts_the_run_rather_than_isolating_fifty_pages(self):
        err = M.AIError("unauthorized")
        err.status = 401
        sink = M.new_ai_ctx({"ai_retries": 0, "ai_fails": M.FailWindow()},
                            ep("gemini"))
        with self.assertRaises(M.SystemicAIFailure):
            self._run([err], sink=sink)

    def test_the_window_trips_on_sustained_transport_failure(self):
        self.pages = 12
        fails = [M.AIError("connection reset") for _ in range(12)]
        sink = M.new_ai_ctx({"ai_retries": 0, "ai_fails": M.FailWindow()},
                            ep("gemini"))
        with self.assertRaises(M.SystemicAIFailure):
            self._run(fails, sink=sink)

    def test_the_window_is_weaker_than_a_consecutive_counter(self):
        """F F F F S F F F F S F F: longest run of four, and it must still
        trip. A consecutive counter would miss this entirely."""
        window = M.FailWindow()
        seq = [1, 1, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1]
        tripped = [i for i, f in enumerate(seq) if window.record(bool(f))]
        self.assertTrue(tripped)
        runs, best = 0, 0
        for f in seq:
            runs = runs + 1 if f else 0
            best = max(best, runs)
        self.assertLess(best, window.limit)

    def test_content_stops_never_trip_the_window(self):
        """Deterministic and genuinely per-page — which is why the client
        does not retry them either."""
        self.pages = 40
        sink = M.new_ai_ctx({"ai_retries": 0, "ai_fails": M.FailWindow()},
                            ep("gemini"))
        text = self._run([self._refusal() for _ in range(40)], sink=sink)
        self.assertEqual(text.count("NO TRANSCRIPTION"), 40)

    def test_a_refused_page_sends_the_file_to_manual_not_to_meets(self):
        row = {}
        manual, review = [], []
        ctx = {"usage": M.new_usage(),
               "pages": [page(1, 900), page(2, 0, refused="max_tokens")]}
        M.apply_ai_results(row, ctx, manual, review)
        self.assertTrue(manual)
        self.assertEqual(M.classify_accessibility(manual, review),
                         M.STATUS_MANUAL)

    def test_a_refused_image_still_writes_a_sidecar(self):
        sidecar = os.path.join(self.tmp, "img_ai_text.txt")
        sink = M.new_ai_ctx({"ai_retries": 0}, ep("gemini"))
        real_bytes, real_page = M._image_send_bytes, M.ai_ocr_page
        M._image_send_bytes = lambda p: (b"img", "image/png")

        def boom(*a, **kw):
            raise self._refusal()

        M.ai_ocr_page = boom
        try:
            M.ai_ocr_image_sidecar(ep("gemini"), "x.jpg", sidecar,
                                   retries=0, sink=sink)
        finally:
            M._image_send_bytes, M.ai_ocr_page = real_bytes, real_page
        self.assertIn("NO TRANSCRIPTION",
                      Path(sidecar).read_text(encoding="utf-8"))
        self.assertEqual(sink["pages"][0]["refused"], "max_tokens")


# ---------------------------------------------------------------------------
# 6. Concurrency (C4)
# ---------------------------------------------------------------------------
class Concurrency(PageIsolation):

    def test_pages_are_written_in_page_order_not_completion_order(self):
        """A scrambled transcript is worse than a short one."""
        self.pages = 6
        order = []
        lock = threading.Lock()

        def fake_page(endpoint, data, mime="image/png", retries=0):
            with lock:
                order.append(len(order))
                n = len(order)
            if n == 1:
                import time
                time.sleep(0.15)        # first submitted, last to finish
            return ("body-%d" % n), {"in": 1, "out": 1, "think": 0,
                                     "cache_read": 0}, 0, None

        real = M.ai_ocr_page
        M.ai_ocr_page = fake_page
        try:
            M.ai_ocr_pdf_sidecar(ep("gemini"), "x.pdf", self.sidecar,
                                 lambda m: None, lambda: None, retries=0,
                                 sink=None, concurrency=4)
        finally:
            M.ai_ocr_page = real
        labels = [b.splitlines()[0] for b in
                  Path(self.sidecar).read_text(encoding="utf-8").split("\n\n")]
        self.assertEqual(labels, ["--- Page %d ---" % i
                                  for i in range(1, 7)])

    def test_one_page_raising_does_not_take_the_file_down(self):
        text = self._run(["p1", self._refusal(), "p3", "p4", "p5"],
                         concurrency=4)
        for expected in ("p1", "p3", "p4", "p5"):
            self.assertIn(expected, text)

    def test_stop_is_honoured_with_workers_in_flight(self):
        state = {"n": 0}
        lock = threading.Lock()

        def check():
            with lock:
                state["n"] += 1
                n = state["n"]
            if n > 2:
                raise M.StopRequested()

        with self.assertRaises(M.StopRequested):
            self._run(["p1", "p2", "p3", "p4", "p5"], check=check,
                      concurrency=4)
        self.assertTrue(os.path.exists(self.sidecar))

    def test_concurrency_one_never_enters_the_pool(self):
        entered = []
        real = M._run_page_pool
        M._run_page_pool = lambda *a, **k: entered.append(True)
        try:
            self._run(["p1", "p2"], concurrency=1)
        finally:
            M._run_page_pool = real
        self.assertEqual(entered, [])


# ---------------------------------------------------------------------------
# 7. The raster clamp
# ---------------------------------------------------------------------------
class RasterClamp(unittest.TestCase):

    def test_an_ordinary_letter_page_is_clamped(self):
        """Not only a guard on oversize scans: 150 dpi puts a letter page at
        1650 px, which is 3x2 Gemini tiles where 1536 is 2x2."""
        scale = M._clamp_scale(612, 792)
        self.assertAlmostEqual(792 * scale, M.AI_OCR_MAX_DIM, places=3)

    def test_a_small_page_keeps_the_dpi_floor(self):
        scale = M._clamp_scale(200, 300)
        self.assertAlmostEqual(scale, M.AI_OCR_DPI / 72.0, places=6)

    def test_the_clamp_never_upscales(self):
        self.assertLessEqual(M._clamp_scale(2000, 3000) * 3000,
                             M.AI_OCR_MAX_DIM + 1)

    def test_the_long_edge_governs_either_orientation(self):
        self.assertAlmostEqual(M._clamp_scale(792, 612),
                               M._clamp_scale(612, 792), places=9)


# ---------------------------------------------------------------------------
# 8. Alt text: reported, never truncated
# ---------------------------------------------------------------------------
class AltTextLimit(unittest.TestCase):

    def test_the_limit_matches_the_image_module(self):
        self.assertEqual(M.AI_ALT_MAX_CHARS, 125)

    def test_a_long_draft_is_flagged_and_not_cut(self):
        long_alt = "x" * 400
        row, manual, review = {}, [], []
        M.apply_ai_results(row, {"usage": M.new_usage(), "pages": [],
                                 "alt_chars": [110, len(long_alt)]},
                           manual, review)
        self.assertEqual(row["alt_chars"], 400)
        self.assertEqual(row["alt_over"], 1)
        self.assertTrue(review)

    def test_nothing_truncates_at_the_reported_limit(self):
        long_alt = "word " * 200
        lines = ['data: {"candidates":[{"content":{"parts":[{"text":'
                 + json.dumps(long_alt) + '}]},"finishReason":"STOP"}]}']
        with StreamPatch(lines):
            alt = M.ai_alt_text(ep("gemini"), b"i", "image/png", retries=0)
        self.assertGreater(len(alt), M.AI_ALT_MAX_CHARS)
        self.assertLessEqual(len(alt), M.AI_ALT_HARD_CAP)

    def test_the_hard_cap_is_far_above_the_reported_limit(self):
        """A cap near the old 500 would reintroduce the mid-word cut into
        /Alt that removing the truncation exists to prevent."""
        self.assertGreaterEqual(M.AI_ALT_HARD_CAP, 2000)

    def test_decorative_still_wins(self):
        lines = ['data: {"candidates":[{"content":{"parts":'
                 '[{"text":"DECORATIVE"}]},"finishReason":"STOP"}]}']
        with StreamPatch(lines):
            self.assertEqual(
                M.ai_alt_text(ep("gemini"), b"i", "image/png", retries=0), "")


# ---------------------------------------------------------------------------
# 9. Prompts
# ---------------------------------------------------------------------------
class Prompts(unittest.TestCase):

    def test_the_ocr_prompt_demands_a_coverage_declaration(self):
        self.assertIn("Uncertain:", M.OCR_PROMPT)
        self.assertIn("asserts that every legible character", M.OCR_PROMPT)

    def test_the_ocr_prompt_quarantines_inference(self):
        self.assertIn("not printed on the page", M.OCR_PROMPT)

    def test_the_ocr_prompt_keeps_the_no_text_sentinel(self):
        self.assertIn("[NO TEXT]", M.OCR_PROMPT)

    def test_the_alt_prompt_asks_for_the_shared_limit(self):
        self.assertIn("125", M.ALT_PROMPT)
        self.assertNotIn("200 characters", M.ALT_PROMPT)
        self.assertIn("DECORATIVE", M.ALT_PROMPT)


# ---------------------------------------------------------------------------
# 10. validate_start
# ---------------------------------------------------------------------------
def _model(tmp):
    path = os.path.join(tmp, "a.pdf")
    Path(path).write_bytes(b"%PDF-1.4\n")
    return [{"path": path, "rel": "a.pdf", "format": "pdf", "ctx": "",
             "article": "", "title": "", "state": "", "admin_url": ""}]


class ValidateStart(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.model = _model(self.tmp)
        self.tools = {"ocrmypdf": True, "tesseract": True,
                      "ghostscript": True, "pikepdf": True,
                      "pymupdf": True, "python-docx": True,
                      "python-pptx": True}
        self.ai = {"endpoints": [ep("gemini", name="G")], "default": "G"}

    def req(self, **kw):
        base = {"paths": [self.model[0]["path"]], "mode": "auto",
                "save": "copy", "out_dir": self.tmp, "engine": "ai",
                "endpoint": "G"}
        base.update(kw)
        return base

    def test_the_default_is_one_so_nothing_changes_unasked(self):
        msg, cfg, _ep = M.validate_start(self.req(), self.model, self.tools,
                                         self.ai)
        self.assertEqual(msg, "")
        self.assertEqual(cfg["ai_concurrency"], M.DEFAULT_AI_CONCURRENCY)

    def test_the_range_is_enforced_with_a_readable_message(self):
        for bad in (0, 5, -1):
            msg, cfg, _ep = M.validate_start(
                self.req(ai_concurrency=bad), self.model, self.tools,
                self.ai)
            self.assertIn("Concurrent AI requests", msg)
            self.assertIsNone(cfg)

    def test_the_top_of_the_range_is_accepted(self):
        msg, cfg, _ep = M.validate_start(
            self.req(ai_concurrency=M.MAX_AI_CONCURRENCY), self.model,
            self.tools, self.ai)
        self.assertEqual(msg, "")
        self.assertEqual(cfg["ai_concurrency"], M.MAX_AI_CONCURRENCY)

    def test_it_is_still_a_pure_function(self):
        before = json.dumps(self.ai, sort_keys=True)
        M.validate_start(self.req(), self.model, self.tools, self.ai)
        self.assertEqual(json.dumps(self.ai, sort_keys=True), before)


# ---------------------------------------------------------------------------
# 11. Report shape
# ---------------------------------------------------------------------------
def _row(**kw):
    row = dict((k, "") for k in (
        "file", "format", "ctx", "article", "title", "state", "pages",
        "text_before", "text_after", "ocr", "tagged", "doc_title",
        "language", "images", "alt_missing", "accessibility", "next",
        "output", "status", "time", "admin_url"))
    row.update({"order": 1, "alt_drafted": 0, "in_tok": 975, "out_tok": 1000,
                "think_tok": 400, "cache_tok": 0, "ai_pages": 5,
                "alt_chars": 118, "alt_over": "",
                "flags": "SHORT PAGE p3"})
    row.update(kw)
    return row


class ReportShape(unittest.TestCase):

    def test_headers_widths_and_row_values_agree(self):
        """Nothing in the module ties the three together — _finish_sheet's
        zip truncates in silence — so this is the guard."""
        values = M._row_values(_row())
        self.assertEqual(len(values), len(M.REPORT_HEADERS))
        self.assertEqual(len(M.REPORT_WIDTHS), len(M.REPORT_HEADERS))

    def test_the_admin_url_hyperlink_column_did_not_move(self):
        self.assertEqual(M.REPORT_HEADERS[M.REPORT_LINK_COLS[0] - 1],
                         "Admin Record URL")

    def test_the_new_columns_are_appended_after_it(self):
        for name in ("In Tok", "Out Tok", "Think Tok", "Cached Tok",
                     "AI Pages", "Alt Over Limit", "Flags"):
            self.assertGreater(M.REPORT_HEADERS.index(name),
                               M.REPORT_LINK_COLS[0] - 1)

    def test_a_zero_token_count_renders_blank_not_zero(self):
        """An unreported figure must not read as a measured nought."""
        values = M._row_values(_row(in_tok=0, out_tok=0, think_tok=0))
        self.assertEqual(values[M.REPORT_HEADERS.index("In Tok")], "")

    def test_a_real_workbook_round_trips(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not installed")
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "r.xlsx")
        M.write_report(path, [M._row_values(_row())],
                       [("Run", "now"), ("Total output tokens", "1,000")],
                       "0F2B5B", extra_note=M.AI_REVIEW_NOTE)
        book = openpyxl.load_workbook(path)
        sheet = book["Files"]
        self.assertEqual([c.value for c in sheet[1]], M.REPORT_HEADERS)
        self.assertEqual(sheet.cell(row=2, column=len(M.REPORT_HEADERS)).value,
                         "SHORT PAGE p3")
        summary = "\n".join(
            str(r[0].value) for r in book["Summary"].iter_rows())
        self.assertIn("Out Tok includes Think Tok", summary)


# ---------------------------------------------------------------------------
# 12. Client drift — unchanged, and it must stay that way
# ---------------------------------------------------------------------------
class ClientDrift(unittest.TestCase):

    def test_this_release_did_not_touch_the_client(self):
        self.assertEqual(M.AI_CLIENT_VERSION, "1.4")
        self.assertEqual(M.AI_CLIENT_VERSION, IMG.AI_CLIENT_VERSION)

    def test_the_usage_normaliser_is_still_shared(self):
        for kind, payload in (
                ("gemini", {"candidatesTokenCount": 5,
                            "thoughtsTokenCount": 9}),
                ("anthropic", {"input_tokens": 5, "output_tokens": 6}),
                ("openai", None)):
            self.assertEqual(M.normalize_usage(kind, payload),
                             IMG.normalize_usage(kind, payload))

    def test_a_named_early_stop_is_still_refused_and_not_retried(self):
        lines = ['data: {"candidates":[{"content":{"parts":[{"text":"x"}]},'
                 '"finishReason":"MAX_TOKENS"}]}']
        slept = []
        with StreamPatch(lines):
            with self.assertRaises(M.AIError):
                M.ai_generate_with_retry(ep("gemini"), "p", b"i",
                                         "image/png", 4, slept.append,
                                         lambda m: None)
        self.assertEqual(slept, [])

    def test_the_refusal_reason_is_readable_back_off_the_error(self):
        try:
            with StreamPatch(['data: {"candidates":[{"content":{"parts":'
                              '[{"text":"x"}]},'
                              '"finishReason":"RECITATION"}]}']):
                M.ai_generate(ep("gemini"), "p", b"i")
        except M.AIError as err:
            self.assertEqual(M.refusal_reason(err), "recitation")
        else:
            self.fail("expected an AIError")

    def test_a_transport_error_has_no_named_reason(self):
        self.assertIsNone(M.refusal_reason(M.AIError("connection reset")))

    def test_pricing_never_came_back(self):
        for mod in (M, IMG):
            self.assertFalse(hasattr(mod, "endpoint_cost"))
            self.assertFalse(hasattr(mod, "CACHE_READ_RATE"))


if __name__ == "__main__":
    # Say which version is under test, read from the module rather than from
    # this file's name. Nothing here hard-codes a version.
    print("OCR & Accessibility Toolkit v{} — verification".format(M.MANIFEST["version"]))
    unittest.main(verbosity=2)
