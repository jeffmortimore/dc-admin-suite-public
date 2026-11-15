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
  · the client drift check, unchanged and still green;
  · 1.13.0 — the accessibility half, which nothing here had touched: the
    structure walk and its verdict, the tagger's output, Alt Missing as a
    count, a tagged PDF never OCR'd, decorative images, AI pages sent and
    transcribed, a Stop part-way through a file, the AI gate (and its
    agreement with the image describer's copy), and the veraPDF adapter
    against a real report. The PDF and Office tests need pikepdf, pymupdf,
    python-docx and python-pptx and are skipped, aloud, without them; CI
    runs this file a second time with them installed.
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
# DC-FORM (1.0.1): a test must never write the operator's remembered form.
M.FORM_PATH = os.path.join(tempfile.mkdtemp(prefix="dcocr-form-"), "f.json")
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
                                              max_tokens=M.DEFAULT_REPLY_LIMIT)
        self.assertEqual(body["generationConfig"]["maxOutputTokens"],
                         M.DEFAULT_REPLY_LIMIT)

    def test_openai_builder_carries_the_ceiling_and_asks_for_usage(self):
        _u, _h, body = M.build_openai_request(ep("openai"), "p", b"i",
                                              stream=True,
                                              max_tokens=M.DEFAULT_REPLY_LIMIT)
        self.assertEqual(body["max_tokens"], M.DEFAULT_REPLY_LIMIT)
        self.assertEqual(body["stream_options"], {"include_usage": True})

    def test_anthropic_builder_carries_the_ceiling(self):
        _u, _h, body = M.build_anthropic_request(
            ep("anthropic"), "p", b"i", max_tokens=M.DEFAULT_REPLY_LIMIT)
        self.assertEqual(body["max_tokens"], M.DEFAULT_REPLY_LIMIT)

    def test_ocr_ceiling_reaches_the_wire(self):
        """The defect itself: no caller used to pass one."""
        with StreamPatch(GEMINI_LINES) as sp:
            M.ai_ocr_page(ep("gemini"), b"img", retries=0)
        self.assertEqual(sp.sent["generationConfig"]["maxOutputTokens"],
                         M.DEFAULT_REPLY_LIMIT)
        with StreamPatch(GEMINI_LINES) as sp:
            M.ai_ocr_page(ep("gemini"), b"img", retries=0, max_tokens=2500)
        self.assertEqual(sp.sent["generationConfig"]["maxOutputTokens"],
                         2500)

    def test_the_forms_limit_reaches_an_alt_draft_too(self):
        """1.0.1: one limit, from the form, for pages and alt drafts. 1.0.0
        drafted alt text at 300, which a thinking model can spend on its
        reasoning alone."""
        with StreamPatch(GEMINI_LINES) as sp:
            M.ai_alt_text(ep("gemini"), b"img", "image/png", retries=0,
                          sink={"max_tokens": 1234})
        self.assertEqual(sp.sent["generationConfig"]["maxOutputTokens"],
                         1234)
        with StreamPatch(GEMINI_LINES) as sp:
            M.ai_alt_text(ep("gemini"), b"img", "image/png", retries=0)
        self.assertEqual(sp.sent["generationConfig"]["maxOutputTokens"],
                         M.DEFAULT_REPLY_LIMIT)

    def test_the_forms_limit_reaches_a_page_through_the_context(self):
        ctx = M.new_ai_ctx({"max_tokens": 4321}, ep("gemini"))
        self.assertEqual(ctx["max_tokens"], 4321)
        ctx = M.new_ai_ctx({}, ep("gemini"))
        self.assertEqual(ctx["max_tokens"], M.DEFAULT_REPLY_LIMIT)

    def test_the_ceiling_is_not_the_old_silent_default(self):
        self.assertEqual(M.DEFAULT_REPLY_LIMIT, 8000)
        self.assertNotEqual(M.DEFAULT_REPLY_LIMIT, M.DEFAULT_MAX_TOKENS)

    def test_a_refused_alt_draft_still_reports_what_it_cost(self):
        lines = ['data: {"candidates":[{"content":{"parts":[{"text":"x"}]},'
                 '"finishReason":"MAX_TOKENS"}],"usageMetadata":'
                 '{"promptTokenCount":975,"candidatesTokenCount":20,'
                 '"thoughtsTokenCount":280}}']
        sink = M.new_ai_ctx({}, ep("gemini"))
        with StreamPatch(lines):
            with self.assertRaises(M.AIError):
                M.ai_alt_text(ep("gemini"), b"img", "image/png", retries=0,
                              sink=sink)
        self.assertEqual((sink["usage"]["in"], sink["usage"]["out"]),
                         (975, 300))


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

    def test_an_echoed_label_is_the_label_not_a_reading(self):
        got = M.parse_ocr_reply("t\nUncertain: Uncertain: None.")
        self.assertEqual((got["state"], got["uncertain"]), ("none", ""))
        got = M.parse_ocr_reply("t\nUncertain: uncertain: line 4 faint")
        self.assertEqual((got["state"], got["uncertain"]),
                         ("listed", "line 4 faint"))

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

        def fake_page(endpoint, data, mime="image/png", retries=0,
                      max_tokens=None):
            self.__dict__.setdefault("page_limits", []).append(max_tokens)
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

    def test_a_refused_page_still_reports_what_it_cost(self):
        """Client 1.5: a refused page was billed, so its usage reaches the
        file's totals. 1.0.0 recorded None for every refused page."""
        refused = self._refusal()
        refused.usage = {"in": 900, "out": 8000, "think": 7900,
                         "cache_read": 0}
        sink = M.new_ai_ctx({"ai_retries": 0}, ep("gemini"))
        self._run(["p1", refused], sink=sink)
        ok_pages = self.pages - 1             # every other page answers "ok"
        self.assertEqual(sink["usage"]["out"], 20 * ok_pages + 8000)
        self.assertEqual(sink["usage"]["think"], 5 * ok_pages + 7900)

    def test_every_page_is_sent_the_forms_reply_limit(self):
        """1.0.1: the limit travels from the form, through the per-file AI
        context, to every page request - not a module constant."""
        self.page_limits = []
        sink = M.new_ai_ctx({"ai_retries": 0, "max_tokens": 2222},
                            ep("gemini"))
        self._run(["p1", "p2", "p3"], sink=sink)
        self.assertTrue(self.page_limits)
        self.assertEqual(set(self.page_limits), {2222})


# ---------------------------------------------------------------------------
# 6. Concurrency (C4)
# ---------------------------------------------------------------------------
class Concurrency(PageIsolation):

    def test_pages_are_written_in_page_order_not_completion_order(self):
        """A scrambled transcript is worse than a short one."""
        self.pages = 6
        order = []
        lock = threading.Lock()

        def fake_page(endpoint, data, mime="image/png", retries=0,
                      max_tokens=None):
            self.__dict__.setdefault("page_limits", []).append(max_tokens)
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

    def test_the_reply_limit_comes_from_the_form(self):
        msg, cfg, _ep = M.validate_start(self.req(max_tokens="2000"),
                                         self.model, self.tools, self.ai)
        self.assertEqual((msg, cfg["max_tokens"]), ("", 2000))
        msg, cfg, _ep = M.validate_start(self.req(), self.model,
                                         self.tools, self.ai)
        self.assertEqual(cfg["max_tokens"], 8000)
        for bad in ("", "12", "x"):
            msg, cfg, _ep = M.validate_start(self.req(max_tokens=bad),
                                             self.model, self.tools, self.ai)
            self.assertIn("Reply limit (output tokens)", msg, bad)
            self.assertIsNone(cfg)

    def test_a_blank_output_folder_is_refused_by_name(self):
        msg, cfg, _ep = M.validate_start(self.req(out_dir=""), self.model,
                                         self.tools, self.ai)
        self.assertIn("\u201cOutput folder\u201d field is empty", msg)
        self.assertIsNone(cfg)

    def test_the_page_sends_the_reply_limit(self):
        """SOURCE check, said so: the start body is assembled inside a click
        handler that needs the whole page to run. The field must exist, be
        enabled with the endpoint like the other AI settings, and be sent
        as typed so a blank one is named by the server."""
        page = M.build_page({"branding": {}})
        page = page.decode("utf-8") if isinstance(page, bytes) else page
        self.assertIn('id="aimaxtok"', page)
        self.assertIn("aimaxtok.disabled=!has;", page)
        self.assertIn("max_tokens:aimaxtok.value.trim(),", page)

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
                     "AI Pages Sent", "Alt Over Limit", "Flags"):
            self.assertGreater(M.REPORT_HEADERS.index(name),
                               M.REPORT_LINK_COLS[0] - 1)

    def test_the_1_13_columns_follow_the_served_model(self):
        """Appended, so every earlier column keeps its number."""
        served = M.REPORT_HEADERS.index("Served Model")
        self.assertEqual(M.REPORT_HEADERS[served + 1:],
                         ["AI Pages Transcribed", "Page Scans",
                          "Checked By", "Rule Failures", "AI Gate",
                          "Detector Findings"])

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
        self.assertEqual(sheet.cell(
            row=2, column=M.REPORT_HEADERS.index("Flags") + 1).value,
            "SHORT PAGE p3")
        summary = "\n".join(
            str(r[0].value) for r in book["Summary"].iter_rows())
        self.assertIn("Out Tok includes Think Tok", summary)


# ---------------------------------------------------------------------------
# 12. Client drift — unchanged, and it must stay that way
# ---------------------------------------------------------------------------
class ClientDrift(unittest.TestCase):

    def test_the_client_is_1_6_the_served_model(self):
        self.assertEqual(M.AI_CLIENT_VERSION, "1.6")

    def test_both_modules_judge_a_reply_limit_the_same_way(self):
        for raw in (None, "", " ", "x", "63", 64, "8000", 32000, "32001",
                    "1.5", -1):
            self.assertEqual(M.parse_reply_limit(raw),
                             IMG.parse_reply_limit(raw), repr(raw))
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


# ---------------------------------------------------------------------------
# 1.11.0 / client 1.6 — the served model through an OCR run
# ---------------------------------------------------------------------------
class ServedModelInTheRun(unittest.TestCase):
    """process_worker with the REAL process_one, image path, ai_ocr_page and
    client; only the image bytes and the workbook write are stubbed. The
    request names gemini-3.7-flash; the stream says gemini-3.8-flash."""

    def test_the_redirect_reaches_the_row_and_the_summary(self):
        tmp = tempfile.mkdtemp()
        img = os.path.join(tmp, "a.jpg")
        Path(img).write_bytes(b"x")
        entry = {"path": img, "rel": "a.jpg", "format": "image", "ctx": "",
                 "article": "", "title": "", "state": "", "admin_url": ""}
        body = {"candidates": [{"content": {"parts": [
            {"text": "Some text\nUncertain: None."}]},
            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5,
                              "candidatesTokenCount": 3},
            "modelVersion": "gemini-3.8-flash"}
        written = {}

        def capture(path, rows, pairs, rgb, extra_note=(), **kw):
            written["rows"], written["pairs"] = rows, dict(pairs)
        said = []
        real = (M._image_send_bytes, M.write_report, M.log)
        M._image_send_bytes = lambda p: (b"img", "image/png")
        M.write_report = capture
        M.log = lambda m, *a: said.append(str(m))
        try:
            cfg = {"entries": [entry], "out_dir": tmp, "source": "test",
                   "mode": "auto", "save": "copy", "engine": "ai",
                   "ai_alt": False, "ai_retries": 0, "suffix": "_ocr",
                   "max_tokens": 8000}
            with StreamPatch(["data: " + json.dumps(body)]):
                M.process_worker({"branding": {}}, cfg,
                                 ep("gemini", model="gemini-3.7-flash"))
        finally:
            M._image_send_bytes, M.write_report, M.log = real
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertIn("rows", written, "\n".join(said[-8:]))
        row = written["rows"][0]
        self.assertEqual(row[M.REPORT_HEADERS.index("Served Model")],
                         "gemini-3.8-flash", "\n".join(said[-8:]))
        note = ("Requested gemini-3.7-flash, but the provider says "
                "gemini-3.8-flash answered.")
        self.assertEqual(written["pairs"]["Model requested"],
                         "gemini-3.7-flash")
        self.assertEqual(written["pairs"]["Model served"], note)
        self.assertIn("Model: " + note, said)

    def test_add_usage_merges_the_model_and_sums_the_counts(self):
        sink = {}
        M.add_usage(sink, {"in": 1, "out": 2, "model": "a"})
        M.add_usage(sink, {"in": 1, "out": 2, "model": "b"})
        M.add_usage(sink, {"in": 1, "out": 2})
        self.assertEqual(sink["usage"], {"in": 3, "out": 6, "think": 0,
                                         "cache_read": 0, "model": "a, b"})

    def test_a_run_with_no_billed_request_says_so(self):
        self.assertEqual(M.run_served_note([{"in_tok": 0, "out_tok": 0}],
                                           {"model": "m"}),
                         "No AI request was billed in this run.")



# ---------------------------------------------------------------------------
# 1.13.0 — what the report says is measured
# ---------------------------------------------------------------------------
try:
    import pikepdf as _pikepdf
except ImportError:
    _pikepdf = None
try:
    import fitz as _real_fitz      # noqa: F401
    _HAVE_FITZ = True
except ImportError:
    _HAVE_FITZ = False
try:
    import docx as _docx           # noqa: F401
    import pptx as _pptx           # noqa: F401
    _HAVE_OFFICE = True
except ImportError:
    _HAVE_OFFICE = False

# Said aloud by unittest as a skip, never passed in silence. CI's second OCR
# step installs these and runs this file again.
needs_pikepdf = unittest.skipUnless(
    _pikepdf is not None, "pikepdf is not installed")
needs_pdf = unittest.skipUnless(
    _pikepdf is not None and _HAVE_FITZ,
    "pikepdf and pymupdf are not installed")
needs_office = unittest.skipUnless(
    _HAVE_OFFICE, "python-docx and python-pptx are not installed")

REAL_VERAPDF_UA1 = '{"report":{"buildInformation":{"releaseDetails":[{"id":"core","version":"1.30.3","buildDate":0},{"id":"validation-model","version":"1.30.3","buildDate":0},{"id":"apps","version":"1.30.3","buildDate":0}]},"jobs":[{"itemDetails":{"name":"tagged_lo.pdf","size":23025},"validationResult":[{"details":{"passedRules":104,"failedRules":2,"passedChecks":444,"failedChecks":2,"tags":["metadata","figure","alt-text","structure"],"ruleSummaries":[{"ruleStatus":"FAILED","specification":"ISO 14289-1:2014","clause":"5","testNumber":1,"status":"failed","failedChecks":1,"tags":["metadata"],"description":"The PDF/UA version and conformance level of a file shall be specified using the PDF/UA Identification extension schema","object":"MainXMPPackage","test":"containsPDFUAIdentification == true","checks":[{"status":"failed","context":"root/document[0]/metadata[0](39 0 obj PDMetadata)/XMPPackage[0]","errorMessage":"The document metadata stream doesn\'t contain PDF/UA Identification Schema","errorArguments":[]}]},{"ruleStatus":"FAILED","specification":"ISO 14289-1:2014","clause":"7.3","testNumber":1,"status":"failed","failedChecks":1,"tags":["figure","alt-text","structure"],"description":"Figure tags shall include an alternative representation or replacement text that represents the contents marked with the Figure tag as noted in ISO 32000-1:2008, 14.7.2, Table 323","object":"SEFigure","test":"(Alt != null && Alt != \'\') || ActualText != null","checks":[{"status":"failed","context":"root/document[0]/StructTreeRoot[0](40 0 obj PDStructTreeRoot)/K[0](4 0 obj SEDocument Document)/K[3](10 0 obj SEP Standard)/K[0](11 0 obj SEFigure Figure)","errorMessage":"Figure structure element neither has an alternate description nor a replacement text","errorArguments":[]}]}]},"jobEndStatus":"normal","profileName":"PDF/UA-1 validation profile","statement":"PDF file is not compliant with Validation Profile requirements.","compliant":false}],"processingTime":{"start":0,"finish":0,"duration":"00:00:00.123","difference":123}}],"batchSummary":{"duration":{"start":0,"finish":0,"duration":"00:00:00.161","difference":161},"totalJobs":1,"outOfMemory":0,"veraExceptions":0,"multiJob":false,"failedEncryptedJobs":0,"failedParsingJobs":0,"validationSummary":{"nonCompliantPdfaCount":1,"compliantPdfaCount":0,"failedJobCount":0,"totalJobCount":1,"successfulJobCount":1},"featuresSummary":{"failedJobCount":0,"totalJobCount":0,"successfulJobCount":0},"repairSummary":{"failedJobCount":0,"totalJobCount":0,"successfulJobCount":0}}}}'

def _png(w=4, h=4):
    """A valid grayscale PNG, built without Pillow."""
    import struct
    import zlib

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    raw = b"".join(b"\x00" + b"\x80" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _font(pdf):
    P = _pikepdf
    return pdf.make_indirect(P.Dictionary(
        Type=P.Name.Font, Subtype=P.Name.Type1, BaseFont=P.Name.Helvetica))


def _image(pdf, w=8, h=8):
    P = _pikepdf
    img = P.Stream(pdf, b"\x80" * (w * h))
    img.Type = P.Name.XObject
    img.Subtype = P.Name.Image
    img.Width, img.Height = w, h
    img.ColorSpace = P.Name.DeviceGray
    img.BitsPerComponent = 8
    return pdf.make_indirect(img)


TEXT = b"BT /F1 12 Tf 72 720 Td (Findings) Tj ET"
SMALL_FIGURE = b"q 100 0 0 80 72 500 cm /Im0 Do Q"
FULL_PAGE_SCAN = b"q 612 0 0 792 0 0 cm /Scan Do Q"


def _plain_pdf(path, content=TEXT + b"\n" + SMALL_FIGURE, images=("Im0",),
               marked=False):
    """An untagged page: text and the named images, drawn by `content`."""
    P = _pikepdf
    pdf = P.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    xobj = P.Dictionary()
    for name in images:
        xobj[P.Name("/" + name)] = _image(pdf)
    page.Resources = P.Dictionary(Font=P.Dictionary(F1=_font(pdf)),
                                  XObject=xobj)
    page.Contents = pdf.make_stream(content)
    if marked:
        pdf.Root.MarkInfo = P.Dictionary(Marked=True)
    pdf.save(path)
    return path


def _tagged_pdf(path, alts=("Bar chart", None), th=False, mark=True,
                second_untagged=False, orphan=False, empty=False):
    """A tagged page shaped like a word processor's export: a heading
    reached through the RoleMap, Figures with and without /Alt, a table
    whose cell is marked by an MCR dictionary, and MCIDs given both as
    integers and in arrays."""
    P = _pikepdf
    pdf = P.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    font = _font(pdf)
    page.Resources = P.Dictionary(Font=P.Dictionary(F1=font),
                                  XObject=P.Dictionary(Im0=_image(pdf)))
    parts = [b"/Heading1 <</MCID 0>> BDC " + TEXT + b" EMC"]
    mcid = 1
    for _alt in alts:
        parts.append(b"/Figure <</MCID %d>> BDC " % mcid + SMALL_FIGURE
                     + b" EMC")
        mcid += 1
    parts.append(b"/TD <</MCID %d>> BDC BT /F1 10 Tf 72 400 Td (2026) Tj "
                 b"ET EMC" % mcid)
    page.Contents = pdf.make_stream(b"\n".join(parts))
    page.StructParents = 0
    pg = page.obj
    tree = pdf.make_indirect(P.Dictionary(
        Type=P.Name.StructTreeRoot,
        RoleMap=P.Dictionary(Heading1=P.Name.H1)))
    if not empty:
        doc = pdf.make_indirect(P.Dictionary(Type=P.Name.StructElem,
                                             S=P.Name.Document, P=tree))
        kids = [pdf.make_indirect(P.Dictionary(
            Type=P.Name.StructElem, S=P.Name("/Heading1"), P=doc, Pg=pg,
            K=0))]
        for i, alt in enumerate(alts, 1):
            fig = P.Dictionary(Type=P.Name.StructElem, S=P.Name.Figure,
                               P=doc, Pg=pg, K=P.Array([i]))
            if alt:
                fig.Alt = P.String(alt)
            kids.append(pdf.make_indirect(fig))
        cell = pdf.make_indirect(P.Dictionary(
            Type=P.Name.StructElem, S=P.Name("/TH" if th else "/TD"),
            K=P.Dictionary(Type=P.Name.MCR, MCID=mcid, Pg=pg)))
        row = pdf.make_indirect(P.Dictionary(
            Type=P.Name.StructElem, S=P.Name.TR, K=P.Array([cell])))
        kids.append(pdf.make_indirect(P.Dictionary(
            Type=P.Name.StructElem, S=P.Name.Table, P=doc,
            K=P.Array([row]))))
        if orphan:
            kids.append(pdf.make_indirect(P.Dictionary(
                Type=P.Name.StructElem, S=P.Name.P, P=doc, Pg=pg, K=99)))
        doc.K = P.Array(kids)
        tree.K = doc
    pdf.Root.StructTreeRoot = tree
    if mark:
        pdf.Root.MarkInfo = P.Dictionary(Marked=True)
    if second_untagged:
        p2 = pdf.add_blank_page(page_size=(612, 792))
        p2.Resources = P.Dictionary(Font=P.Dictionary(F1=font))
        p2.Contents = pdf.make_stream(TEXT)
    pdf.Root.Lang = P.String("en-US")
    pdf.docinfo["/Title"] = "Fixture"
    pdf.save(path)
    return path


def _scan_pdf(path, pages=1, with_text=True):
    """Page scans shaped like ocrmypdf's output: the scan drawn over the
    whole page and, with text, an invisible text layer in a Form XObject
    named OCR-*."""
    P = _pikepdf
    pdf = P.new()
    font = _font(pdf)
    for _ in range(pages):
        page = pdf.add_blank_page(page_size=(612, 792))
        xobj = P.Dictionary(Scan=_image(pdf, 16, 16))
        content = FULL_PAGE_SCAN
        if with_text:
            form = P.Stream(pdf, b"BT 3 Tr /F1 12 Tf 72 720 Td "
                                 b"(Scanned words) Tj ET")
            form.Type = P.Name.XObject
            form.Subtype = P.Name.Form
            form.BBox = P.Array([0, 0, 612, 792])
            form.Resources = P.Dictionary(Font=P.Dictionary(F1=font))
            xobj[P.Name("/OCR-fixture")] = pdf.make_indirect(form)
            content += b"\nq /OCR-fixture Do Q"
        page.Resources = P.Dictionary(XObject=xobj)
        page.Contents = pdf.make_stream(content)
    pdf.save(path)
    return path


def _cfg(mode, out, **kw):
    cfg = {"mode": mode, "save": "copy", "out_dir": out, "suffix": "",
           "tag": True, "deskew": False, "langs": "eng", "jobs": 1,
           "doc_lang": "en-US", "set_title": True, "image_out": "pdf",
           "engine": "local", "ai_alt": False, "ai_retries": 0,
           "ai_concurrency": 1, "max_tokens": 8000, "endpoint": "",
           "confirm": False}
    cfg.update(kw)
    return cfg


class _Quiet(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._log = M.log
        self.said = []
        M.log = lambda m, *a: self.said.append(str(m))

    def tearDown(self):
        M.log = self._log
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, name):
        return os.path.join(self.tmp, name)

    def one(self, path, mode="evaluate", ep_=None, **kw):
        run = os.path.join(self.tmp, "run")
        os.makedirs(run, exist_ok=True)
        return M.process_one(M._entry(path, os.path.basename(path)), 1,
                             _cfg(mode, run, **kw), ep_, run)


@needs_pikepdf
class StructureWalk(_Quiet):
    """pdf_structure() and tag_verdict(): Tagged was the presence of a
    StructTreeRoot, and every one of these read "Yes"."""

    def walk(self, path):
        with _pikepdf.open(path) as pdf:
            return M.pdf_structure(pdf)

    def test_a_tagged_file_counts_figures_tables_and_headings(self):
        s = self.walk(_tagged_pdf(self.p("t.pdf")))
        self.assertEqual((s["figures"], s["figures_alt"]), (2, 1))
        self.assertEqual((s["tables"], s["tables_th"]), (1, 0))
        self.assertEqual(s["headings"], 1)       # through the RoleMap
        self.assertEqual(s["elements"], 7)
        self.assertEqual((s["untagged_pages"], s["orphans"]), (0, 0))
        self.assertEqual(M.tag_verdict(s), ("Yes", []))

    def test_a_header_cell_is_found_inside_its_table(self):
        s = self.walk(_tagged_pdf(self.p("t.pdf"), th=True))
        self.assertEqual((s["tables"], s["tables_th"]), (1, 1))

    def test_an_empty_tree_is_not_tagged(self):
        s = self.walk(_tagged_pdf(self.p("t.pdf"), empty=True))
        self.assertEqual(M.tag_verdict(s)[0], "Empty tree")

    def test_an_untagged_page_makes_it_incomplete(self):
        s = self.walk(_tagged_pdf(self.p("t.pdf"), second_untagged=True))
        self.assertEqual(M.tag_verdict(s)[0], "Incomplete — untagged "
                         "content on 1 of 2 page(s)")

    def test_a_tag_pointing_at_nothing_is_an_orphan(self):
        s = self.walk(_tagged_pdf(self.p("t.pdf"), orphan=True))
        self.assertEqual(s["orphans"], 1)
        self.assertIn("1 tag(s) point at content", M.tag_verdict(s)[0])

    def test_markinfo_missing_is_named(self):
        s = self.walk(_tagged_pdf(self.p("t.pdf"), mark=False))
        self.assertEqual(M.tag_verdict(s)[0],
                         "Incomplete — MarkInfo is not set")

    def test_marked_but_without_a_tree_is_no(self):
        s = self.walk(_plain_pdf(self.p("u.pdf"), marked=True))
        self.assertEqual(M.tag_verdict(s)[0],
                         "No (marked as tagged, but no structure tree)")

    def test_content_in_an_artifact_is_not_untagged(self):
        path = _tagged_pdf(self.p("t.pdf"))
        with _pikepdf.open(path, allow_overwriting_input=True) as pdf:
            page = pdf.pages[0]
            page.Contents = pdf.make_stream(
                page.Contents.read_bytes()
                + b"\n/Artifact BMC " + SMALL_FIGURE + b" EMC")
            pdf.save(path)
        self.assertEqual(self.walk(path)["untagged_pages"], 0)


@needs_pdf
class PdfEvaluation(_Quiet):

    def test_a_tagged_file_missing_alt_does_not_meet_checks(self):
        row = self.one(_tagged_pdf(self.p("t.pdf")))
        self.assertEqual(row["tagged"], "Yes")
        self.assertEqual(row["alt_missing"], 1)
        self.assertEqual(row["accessibility"], M.STATUS_MANUAL)
        self.assertIn("Add alt text for 1 figure(s)", row["next"])
        self.assertIn("1 table(s) have no header cells", row["next"])

    def test_a_page_scan_is_not_counted_as_missing_alt(self):
        row = self.one(_scan_pdf(self.p("s.pdf"), with_text=False))
        self.assertEqual((row["images"], row["scan_pages"]), (1, 1))
        self.assertEqual(row["alt_missing"], 0)
        self.assertIn("Run OCR (1 page(s) without text)", row["next"])

    def test_a_small_image_on_an_untagged_page_still_needs_alt(self):
        row = self.one(_plain_pdf(self.p("u.pdf")))
        self.assertEqual((row["tagged"], row["alt_missing"]), ("No", 1))
        self.assertEqual(row["scan_pages"], 0)

    def test_alt_missing_is_counted_not_read_from_the_steps(self):
        """The regex is gone: a count survives a reworded step."""
        import ast
        src = Path(M.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "process_one":
                body = ast.dump(node)
                self.assertNotIn("Add alt text for", body)
                self.assertNotIn("'search'", body)


@needs_pdf
class Tagger(_Quiet):
    """tag_pdf() on its own: what a screen reader following the tags gets."""

    def structure(self, path):
        with _pikepdf.open(path) as pdf:
            return M.pdf_structure(pdf)

    def test_a_scan_with_a_text_layer_is_an_artifact_and_its_text_a_p(self):
        path = _scan_pdf(self.p("s.pdf"))
        res = M.tag_pdf(path, "en-US", "Scan")
        self.assertEqual((res["scans"], res["figures"], res["p"]), (1, 0, 1))
        s = self.structure(path)
        self.assertEqual((s["figures"], s["untagged_pages"]), (0, 0))
        self.assertEqual(M.tag_verdict(s)[0], "Yes")

    def test_a_scan_with_no_text_is_left_alone(self):
        path = _scan_pdf(self.p("s.pdf"), with_text=False)
        res = M.tag_pdf(path, "en-US", "Scan")
        self.assertEqual((res["tagged"], res["figures"]), (False, 0))
        self.assertFalse(self.structure(path)["tree"])

    def test_a_decorative_reply_makes_an_artifact(self):
        path = _plain_pdf(self.p("u.pdf"))
        res = M.tag_pdf(path, "en-US", "T", alt_fn=lambda pno, name: "")
        self.assertEqual((res["decorative"], res["figures"]), (1, 0))
        s = self.structure(path)
        self.assertEqual((s["figures"], s["untagged_pages"]), (0, 0))

    def test_a_drafted_alt_lands_on_its_figure(self):
        path = _plain_pdf(self.p("u.pdf"))
        res = M.tag_pdf(path, "en-US", "T",
                        alt_fn=lambda pno, name: "Bar chart")
        self.assertEqual((res["figures"], res["alts"]), (1, 1))
        self.assertEqual(self.structure(path)["figures_alt"], 1)

    def test_auto_tag_off_sets_metadata_only(self):
        """1.12.0 tagged the file anyway whenever a title or a language was
        being set."""
        path = _plain_pdf(self.p("u.pdf"))
        res = M.tag_pdf(path, "en-US", "A title", tag=False)
        self.assertTrue(res["title_set"])
        self.assertFalse(res["tagged"])
        self.assertFalse(self.structure(path)["tree"])

    def test_an_empty_tree_is_replaced(self):
        path = _tagged_pdf(self.p("t.pdf"), empty=True)
        res = M.tag_pdf(path, "en-US", "T")
        self.assertTrue(res["tagged"])
        self.assertEqual(M.tag_verdict(self.structure(path))[0], "Yes")

    def test_a_tree_with_elements_is_kept(self):
        path = _tagged_pdf(self.p("t.pdf"))
        before = self.structure(path)
        res = M.tag_pdf(path, "en-US", "T")
        self.assertTrue(res["already"])
        self.assertEqual(self.structure(path), before)

    def test_a_draft_is_asked_only_for_images_the_page_draws(self):
        path = _plain_pdf(self.p("u.pdf"), images=("Im0", "Unused"))
        asked = []
        M.tag_pdf(path, "en-US", "T",
                  alt_fn=lambda pno, name: asked.append(name) or "x")
        self.assertEqual(asked, ["/Im0"])


@needs_pdf
class ProcessModes(_Quiet):

    def _tagged_with_a_scan_page(self):
        """A tagged file whose second page is a scan with no text."""
        path = _tagged_pdf(self.p("t.pdf"))
        P = _pikepdf
        with P.open(path, allow_overwriting_input=True) as pdf:
            page = pdf.add_blank_page(page_size=(612, 792))
            page.Resources = P.Dictionary(XObject=P.Dictionary(
                Scan=_image(pdf, 16, 16)))
            page.Contents = pdf.make_stream(FULL_PAGE_SCAN)
            pdf.save(path)
        return path

    def test_a_tagged_pdf_is_never_ocrd(self):
        real = M.run_ocrmypdf

        def refuse(*a, **k):
            raise AssertionError("ocrmypdf was run on a tagged PDF")
        M.run_ocrmypdf = refuse
        try:
            for mode in ("auto", "force"):
                row = self.one(self._tagged_with_a_scan_page(), mode)
                self.assertEqual(row["ocr"], "Skipped (tagged PDF)", mode)
                self.assertIn("1 page(s) without text in a tagged PDF",
                              row["next"], mode)
        finally:
            M.run_ocrmypdf = real

    def test_tagged_is_read_from_the_output(self):
        row = self.one(_plain_pdf(self.p("u.pdf")), "auto")
        self.assertEqual(row["tagged"], "Yes (heuristic)")
        row = self.one(_plain_pdf(self.p("v.pdf")), "auto", tag=False)
        self.assertEqual(row["tagged"], "No")
        self.assertIn("Untagged — rerun with Auto-tag on", row["next"])

    def test_a_stop_part_way_through_a_pdf_keeps_its_row(self):
        path = _scan_pdf(self.p("s.pdf"), pages=2, with_text=False)
        body = {"candidates": [{"content": {"parts": [
            {"text": "Some text\nUncertain: None."}]},
            "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5,
                              "candidatesTokenCount": 3}}
        captured = {}
        real = (M.write_report, M._raster_bytes, M.urllib.request.urlopen)

        def answer_then_stop(req, timeout=None):
            M.STOP_EVENT.set()
            return FakeResponse(("data: " + json.dumps(body) + "\n")
                                .encode("utf-8"))
        M.write_report = (lambda path_, rows, pairs, rgb, extra_note=(), **kw:
                          captured.update(rows=rows, pairs=dict(pairs)))
        M._raster_bytes = lambda pg: (b"img", "image/png")
        M.urllib.request.urlopen = answer_then_stop
        try:
            cfg = _cfg("auto", self.tmp, engine="ai",
                       entries=[M._entry(path, "s.pdf")], source="test")
            M.process_worker({"branding": {}}, cfg, ep("gemini"))
        finally:
            M.write_report, M._raster_bytes, M.urllib.request.urlopen = real
            M.STOP_EVENT.clear()
        row = captured["rows"][0]
        col = M.REPORT_HEADERS.index
        self.assertEqual(row[col("Status")], "Stopped part-way")
        self.assertEqual(row[col("AI Pages Sent")], 1)
        self.assertIn("1 of the 1 page(s) sent", row[col("Next Steps")])
        self.assertTrue(row[col("Output File")].endswith("_ai_text.txt"))
        self.assertNotIn("Stopped before any file was processed.",
                         self.said)


@needs_office
class OfficeDecorative(_Quiet):
    """Word and PowerPoint mark a picture decorative with an extension, not
    with its alt text."""

    def _docx(self):
        import docx
        d = docx.Document()
        d.add_picture(io.BytesIO(_png()))
        path = self.p("d.docx")
        d.save(path)
        return path

    def _pr(self, path):
        import docx
        return list(M.iter_docx_images(docx.Document(path)))[0][0]

    def test_a_decorative_picture_is_not_missing_alt(self):
        import docx
        path = self._docx()
        self.assertEqual(M.eval_docx(path)["alt_missing"], 1)
        d = docx.Document(path)
        pr = list(M.iter_docx_images(d))[0][0]
        M.mark_decorative(pr)
        d.save(path)
        self.assertTrue(M.is_decorative(self._pr(path)))
        self.assertEqual(M.eval_docx(path)["alt_missing"], 0)

    def test_a_decorative_reply_is_marked_the_way_word_marks_it(self):
        path = self._docx()
        real = M._draft_alt
        M._draft_alt = lambda ctx, part, log_fn: ""
        try:
            res = M.process_docx(path, path, "", {"ep": object()},
                                 lambda m: None)
        finally:
            M._draft_alt = real
        self.assertEqual((res["decorative"], res["alt_drafted"],
                          res["alt_missing"]), (1, 0, 0))
        pr = self._pr(path)
        self.assertTrue(M.is_decorative(pr))
        self.assertFalse(pr.get("descr"))

    def test_a_pptx_picture_named_after_its_file_needs_alt(self):
        import pptx
        from pptx.util import Inches
        prs = pptx.Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_picture(io.BytesIO(_png()), Inches(1), Inches(1))
        path = self.p("p.pptx")
        prs.save(path)
        self.assertEqual(M.eval_pptx(path)["alt_missing"], 1)


class _ImageRuns(object):
    """process_worker over image entries with a scripted endpoint: the
    real process_one, image path, client and report rows; only the image
    bytes and the workbook write are stubbed."""

    def run_images(self, replies, **kw):
        tmp = tempfile.mkdtemp()
        entries = []
        for i in range(kw.pop("files", len(replies))):
            img = os.path.join(tmp, "p{}.jpg".format(i))
            Path(img).write_bytes(b"x")
            entries.append(M._entry(img, "p{}.jpg".format(i), kw.pop(
                "meta", None)))
        script = list(replies)
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            step = script.pop(0)
            if isinstance(step, Exception):
                raise step
            if callable(step):
                return step()
            return FakeResponse(("data: " + json.dumps(step) + "\n")
                                .encode("utf-8"))
        written, said = {}, []
        real = (M._image_send_bytes, M.write_report, M.log,
                M.urllib.request.urlopen)
        M._image_send_bytes = lambda p: (b"img", "image/png")
        M.write_report = (lambda path, rows, pairs, rgb, extra_note=(), **kw:
                          written.update(rows=rows, pairs=dict(pairs)))
        M.log = lambda m, *a: said.append(str(m))
        M.urllib.request.urlopen = fake
        try:
            cfg = {"entries": entries, "out_dir": tmp, "source": "test",
                   "mode": kw.get("mode", "auto"), "save": "copy",
                   "engine": "ai",
                   "ai_alt": kw.get("ai_alt", False),
                   "ai_retries": kw.get("retries", 0),
                   "suffix": "", "max_tokens": 8000,
                   "ai_override": kw.get("override", False)}
            M.process_worker({"branding": {}}, cfg, ep("gemini"))
        finally:
            (M._image_send_bytes, M.write_report, M.log,
             M.urllib.request.urlopen) = real
            M.STOP_EVENT.clear()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        return written, said, calls

    OK = {"candidates": [{"content": {"parts": [
        {"text": "Words\nUncertain: None."}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3}}
    CUT = {"candidates": [{"content": {"parts": [{"text": "Wor"}]},
                           "finishReason": "MAX_TOKENS"}],
           "usageMetadata": {"promptTokenCount": 5,
                             "candidatesTokenCount": 8000}}

class AIPagesSentAndTranscribed(_ImageRuns, unittest.TestCase):

    def test_sent_and_transcribed_are_counted_apart(self):
        row, manual, review = {}, [], []
        ctx = {"usage": M.new_usage(),
               "pages": [page(1, 900), page(2, 0, refused="max_tokens"),
                         page(3, 0, refused=M.REQUEST_FAILED)]}
        M.apply_ai_results(row, ctx, manual, review)
        self.assertEqual((row["ai_pages"], row["ai_pages_ok"]), (3, 1))
        joined = " | ".join(manual)
        self.assertIn("1 page(s) were not transcribed — the model stopped "
                      "and named a reason", joined)
        self.assertIn("1 page(s) were not transcribed — the request failed "
                      "and no reply came back", joined)

    def test_a_failed_request_is_not_called_a_model_stop(self):
        text = M._page_placeholder(M.REQUEST_FAILED)
        self.assertNotIn("model stopped", text)
        self.assertIn("the request for this page failed", text)
        self.assertIn("the model stopped for 'max_tokens'",
                      M._page_placeholder("max_tokens"))

    def test_the_note_says_refused_pages_are_in_the_totals(self):
        note = "\n".join(M.AI_REVIEW_NOTE)
        self.assertNotIn("EXCLUDE", note)
        self.assertIn("Token totals include what the provider reported "
                      "for refused pages", note)

    def test_the_summary_counts_through_the_worker(self):
        written, _said, _calls = self.run_images(
            [self.OK, self.CUT, ConnectionResetError("reset by peer")])
        pairs = written["pairs"]
        self.assertEqual(pairs["AI pages sent"], 3)
        self.assertEqual(pairs["AI pages transcribed"], 1)
        self.assertEqual(pairs["Pages not transcribed"],
                         "1 (the model stopped and named a reason) · 1 "
                         "(the request failed with no reply)")
        col = M.REPORT_HEADERS.index("AI Pages Transcribed")
        # A measured nought is shown, not blanked.
        self.assertEqual([r[col] for r in written["rows"]], [1, 0, 0])
        self.assertEqual(pairs["Total output tokens"], "8,003")


class StopPartWay(_ImageRuns, unittest.TestCase):
    """A Stop during a file. Before 1.13.0 the file vanished from the
    report, and a Stop during the only file wrote no report at all."""

    def test_a_stop_during_the_only_file_still_writes_its_row(self):
        def drop_then_stop():
            M.STOP_EVENT.set()          # the backoff wait sees it at once
            raise ConnectionResetError("reset by peer")
        written, said, calls = self.run_images([drop_then_stop], retries=1)
        self.assertEqual(len(calls), 1)
        rows = written["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][M.REPORT_HEADERS.index("Status")],
                         "Stopped part-way")
        self.assertNotIn("Stopped before any file was processed.", said)
        with M.LOCK:
            summary = M.STATE.get("summary", "")
        self.assertIn("0 of 1 file(s) done, 1 stopped part-way", summary)

    def test_a_stop_in_file_two_keeps_file_two(self):
        def drop_then_stop():
            M.STOP_EVENT.set()
            raise ConnectionResetError("reset by peer")
        written, _said, _calls = self.run_images([self.OK, drop_then_stop],
                                                 retries=1)
        statuses = [r[M.REPORT_HEADERS.index("Status")]
                    for r in written["rows"]]
        self.assertEqual(statuses, ["Processed", "Stopped part-way"])


class AIGate(_ImageRuns, unittest.TestCase):
    """Only files the downloader marks public go to the endpoint, unless
    the run's override is on."""

    HIDDEN = {"record": True, "visibility": "hidden",
              "access": "restricted"}

    def test_the_gate_decides_by_visibility_and_access(self):
        cases = [
            ({}, (True, "visibility not known")),
            ({"record": True, "visibility": "public", "access": "open"},
             (True, "public")),
            ({"record": True, "visibility": "public", "access": "n/a"},
             (True, "public")),
            ({"record": True, "visibility": "hidden", "access": "open"},
             (False, "hidden — not sent")),
            ({"record": True, "visibility": "public",
              "access": "restricted"},
             (False, "access restricted — not sent")),
            ({"record": True, "visibility": "public", "access": "unknown"},
             (False, "access unknown — not sent")),
            ({"record": True}, (False, "visibility not recorded, access "
                                       "not recorded — not sent")),
        ]
        for entry, expected in cases:
            self.assertEqual(M.ai_gate(entry), expected, entry)
        self.assertEqual(M.ai_gate(self.HIDDEN, override=True),
                         (True, "sent under override (hidden, access "
                                "restricted)"))

    def test_both_ai_modules_gate_alike(self):
        img = Path(IMG.__file__)
        ocr = Path(M.__file__)
        for name in ("ai_gate", "AI_GATE_PUBLIC", "AI_GATE_UNKNOWN",
                     "_AI_GATE_OPEN"):
            a, b = _ast_named(ocr, name), _ast_named(img, name)
            self.assertIsNotNone(a, name)
            self.assertEqual(a, b, name)

    def test_a_hidden_file_is_never_sent(self):
        written, said, calls = self.run_images([self.OK],
                                               meta=dict(self.HIDDEN))
        self.assertEqual(calls, [])
        row = written["rows"][0]
        self.assertEqual(row[M.REPORT_HEADERS.index("AI Gate")],
                         "hidden, access restricted — not sent")
        self.assertTrue(row[M.REPORT_HEADERS.index("OCR Action")]
                        .startswith("Not sent ("))
        self.assertEqual(written["pairs"]["Files the gate held back"], 1)

    def test_the_override_sends_it_and_says_so(self):
        written, _said, calls = self.run_images(
            [self.OK], meta=dict(self.HIDDEN), override=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            written["rows"][0][M.REPORT_HEADERS.index("AI Gate")],
            "sent under override (hidden, access restricted)")
        self.assertIn("override on", written["pairs"]["AI gate"])

    def test_validate_start_carries_the_override(self):
        tmp = tempfile.mkdtemp()
        model = _model(tmp)
        tools = {"ocrmypdf": True, "tesseract": True, "ghostscript": True,
                 "pikepdf": True, "pymupdf": True, "verapdf": "/x/verapdf"}
        ai = {"endpoints": [ep("gemini", name="G")], "default": "G"}
        base = {"paths": [model[0]["path"]], "mode": "auto", "save": "copy",
                "out_dir": tmp, "engine": "ai", "endpoint": "G",
                "ai_override": True}
        _m, cfg, _e = M.validate_start(base, model, tools, ai)
        self.assertTrue(cfg["ai_override"])
        self.assertEqual(cfg["verapdf"], "/x/verapdf")
        base.update(engine="local", ai_alt=False, verapdf=False)
        _m, cfg, _e = M.validate_start(base, model, tools, ai)
        self.assertFalse(cfg["ai_override"])     # nothing to override
        self.assertEqual(cfg["verapdf"], "")

    def test_the_downloader_report_reaches_both_modules_entries(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not installed")
        tmp = tempfile.mkdtemp()
        sub = os.path.join(tmp, "DC_FileDownloads_x", "ctx")
        os.makedirs(sub)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Saved Filename", "Article ID", "Title", "Visibility",
                   "Access"])
        ws.append(["a.pdf", "1", "A", "hidden", "restricted"])
        wb.save(os.path.join(sub, "DC_FileDownload_Report_ctx.xlsx"))
        for name in ("a.pdf", "b.pdf"):
            Path(os.path.join(sub, name)).write_bytes(b"%PDF-1.4\n")
        entries, _run = M.scan_run_folder(tmp)
        by = dict((e["name"], e) for e in entries)
        self.assertEqual((by["a.pdf"]["record"], by["a.pdf"]["visibility"],
                          by["a.pdf"]["access"]),
                         (True, "hidden", "restricted"))
        # In a downloader run, a file its report does not describe is still
        # a record — held back, never "visibility not known".
        self.assertTrue(by["b.pdf"]["record"])
        self.assertEqual(M.ai_gate(by["b.pdf"]),
                         (False, "visibility not recorded, access not "
                                 "recorded — not sent"))
        for name in ("a.jpg",):
            Path(os.path.join(sub, name)).write_bytes(b"x")
        ws.append(["a.jpg", "2", "B", "hidden", "open"])
        wb.save(os.path.join(sub, "DC_FileDownload_Report_ctx.xlsx"))
        img_entries = IMG.scan_run_folder(tmp)[0]
        self.assertEqual(
            M.ai_gate(img_entries[0]), (False, "hidden — not sent"))


class ImageAltMissing(_ImageRuns, unittest.TestCase):
    """A bare image: Alt Missing is a count. 1.12.0 read it out of a step
    ("Write alt text…") that its pattern did not match, so it said 0."""

    def test_an_image_without_a_draft_is_missing_alt(self):
        entry = M._entry(os.path.join(tempfile.mkdtemp(), "a.png"), "a.png")
        entry["format"] = "image"
        row = {"alt_drafted": 0}
        M.process_image_entry(entry, {"mode": "evaluate"}, None, None, row)
        self.assertEqual(row["alt_missing"], 1)

    def test_a_decorative_reply_is_not_missing_alt(self):
        decorative = {"candidates": [{"content": {"parts": [
            {"text": "DECORATIVE"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5,
                              "candidatesTokenCount": 1}}
        written, _said, calls = self.run_images([self.OK, decorative],
                                                ai_alt=True, files=1)
        self.assertEqual(len(calls), 2)
        row = written["rows"][0]
        self.assertEqual(row[M.REPORT_HEADERS.index("Alt Missing")], 0)
        self.assertIn("The AI judged this image decorative",
                      row[M.REPORT_HEADERS.index("Next Steps")])


class DownloaderNamesAndReports(unittest.TestCase):
    """The gate in the configuration the suite is run in: files on one disk,
    the downloader's reports in another folder. Before this, a run whose
    reports were elsewhere read "visibility not known" for every file, and
    the gate sent hidden files."""

    def _run_folder(self, report_beside=False, rows=None):
        import openpyxl
        tmp = tempfile.mkdtemp()
        run = os.path.join(tmp, "files", "DC_FileDownloads_20990101_000000")
        sub = os.path.join(run, "etd")
        os.makedirs(sub)
        names = ["etd_7_primary_public_current_A.pdf",
                 "etd_8_primary_hidden_original_B.pdf",
                 "etd_9_primary_public_current_C.pdf"]
        for name in names:
            Path(os.path.join(sub, name)).write_bytes(b"%PDF-1.4\n")
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Saved Filename", "Article ID", "Title", "Visibility",
                   "Access"])
        for row in rows or [(names[0], "7", "A", "public", "open"),
                            (names[1], "8", "B", "hidden", "open"),
                            (names[2], "9", "C", "public", "restricted")]:
            ws.append(list(row))
        if report_beside:
            wb.save(os.path.join(sub, "DC_FileDownload_Report_etd_x.xlsx"))
            return run, None
        reports = os.path.join(tmp, "reports")
        flat = os.path.join(reports, os.path.basename(run))
        os.makedirs(flat)
        wb.save(os.path.join(flat, "DC_FileDownload_Report_etd_x.xlsx"))
        return run, reports

    def _gates(self, entries):
        return dict((e["name"].split("_")[1], M.ai_gate(e)) for e in entries)

    def setUp(self):
        try:
            import openpyxl     # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")

    def test_the_name_parser(self):
        self.assertEqual(M.downloader_name_meta(
            "/x/etd_1001_primary_hidden_original_Integer-Sol.pdf"),
            {"article": "1001", "visibility": "hidden"})
        self.assertEqual(M.downloader_name_meta(
            "event_331_native_public_current_Talk.mp4"),
            {"article": "331", "visibility": "public"})
        for name in ("thesis.pdf", "etd_12_primary_secret_x.pdf",
                     "DC_OCR_Report_x.xlsx", ""):
            self.assertEqual(M.downloader_name_meta(name), {}, name)

    def test_without_its_reports_a_run_holds_every_file_back(self):
        run, _reports = self._run_folder()
        entries = M.scan_run_folder(run)[0]
        gates = self._gates(entries)
        self.assertEqual(gates["7"], (False, "access not recorded — not "
                                             "sent"))
        self.assertEqual(gates["8"], (False, "hidden, access not recorded "
                                             "— not sent"))
        self.assertIn("the other 3 carry no access record",
                      M.reported_note(entries))

    def test_reports_saved_elsewhere_are_found(self):
        run, reports = self._run_folder()
        entries, _run = M.scan_run_folder(run, reports)
        gates = self._gates(entries)
        self.assertEqual(gates["7"], (True, "public"))
        self.assertEqual(gates["8"], (False, "hidden — not sent"))
        self.assertEqual(gates["9"], (False, "access restricted — not sent"))
        self.assertEqual(M.reported_note(entries),
                         "Downloader reports describe all 3 file(s).")
        # The run's own report folder works as well as the folder above it.
        entries, _run = M.scan_run_folder(
            run, os.path.join(reports, os.path.basename(run)))
        self.assertEqual(self._gates(entries)["7"], (True, "public"))

    def test_a_reports_folder_without_reports_is_refused(self):
        run, _reports = self._run_folder()
        empty = tempfile.mkdtemp()
        with self.assertRaises(ValueError) as ctx:
            M.scan_run_folder(run, empty)
        self.assertIn("No downloader report", str(ctx.exception))

    def test_the_describer_finds_reports_elsewhere_too(self):
        run, reports = self._run_folder(rows=[
            ("etd_7_primary_public_current_A.jpg", "7", "A", "public",
             "open")])
        sub = os.path.join(run, "etd")
        Path(os.path.join(sub, "etd_7_primary_public_current_A.jpg")
             ).write_bytes(b"x")
        held = IMG.scan_run_folder(run)[0]
        self.assertEqual(IMG.ai_gate(held[0]),
                         (False, "access not recorded — not sent"))
        found = IMG.scan_run_folder(run, reports)[0]
        self.assertEqual(IMG.ai_gate(found[0]), (True, "public"))

    def test_a_downloader_named_copy_in_a_plain_folder_is_held_back(self):
        tmp = tempfile.mkdtemp()
        for name in ("etd_12_primary_hidden_original_x.pdf", "notes.pdf"):
            Path(os.path.join(tmp, name)).write_bytes(b"%PDF-1.4\n")
        gates = dict((e["name"], M.ai_gate(e))
                     for e in M.scan_folder(tmp, False))
        self.assertEqual(gates["etd_12_primary_hidden_original_x.pdf"],
                         (False, "hidden, access not recorded — not sent"))
        self.assertEqual(gates["notes.pdf"], (True, "visibility not known"))
        entries, _bad = M.scan_paths(os.path.join(
            tmp, "etd_12_primary_hidden_original_x.pdf"))
        self.assertFalse(M.ai_gate(entries[0])[0])

    def test_both_modules_read_names_and_reports_alike(self):
        img, ocr = Path(IMG.__file__), Path(M.__file__)
        for name in ("_DL_NAME_RE", "downloader_name_meta",
                     "_with_name_meta", "external_reports", "reported_note",
                     "downloader_reports", "DL_REPORT_GLOBS"):
            a, b = _ast_named(ocr, name), _ast_named(img, name)
            self.assertIsNotNone(a, name)
            self.assertEqual(a, b, name)

    def _post_scan(self, mod, body):
        import threading
        import urllib.request as ur
        from http.server import ThreadingHTTPServer
        session = mod.load_session(Path("/nonexistent/session.json"))
        srv = ThreadingHTTPServer(("127.0.0.1", 0), mod.make_handler(
            session, mod.build_page(session)))
        port = srv.server_address[1]
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        opener = ur.build_opener(ur.ProxyHandler({}))
        real_log = mod.log
        mod.log = lambda *a, **k: None
        try:
            req = ur.Request(
                "http://127.0.0.1:{}/api/scan".format(port),
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json",
                         "Host": "127.0.0.1:{}".format(port)},
                method="POST")
            try:
                with opener.open(req, timeout=10) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except ur.HTTPError as e:
                return json.loads(e.read().decode("utf-8"))
        finally:
            mod.log = real_log
            srv.shutdown()
            srv.server_close()

    def test_both_scan_endpoints_take_the_reports_folder(self):
        run, reports = self._run_folder(rows=[
            ("etd_7_primary_public_current_A.pdf", "7", "A", "public",
             "open"),
            ("etd_7_primary_public_current_A.jpg", "7", "A", "public",
             "open")])
        Path(os.path.join(run, "etd", "etd_7_primary_public_current_A.jpg")
             ).write_bytes(b"x")
        for mod in (M, IMG):
            out = self._post_scan(mod, {"mode": "run", "folder": run,
                                        "reports": reports})
            self.assertTrue(out.get("ok"), (mod.__name__, out))
            with mod.LOCK:
                model = list(mod.MODEL or [])
            hit = [e for e in model if e["name"].startswith("etd_7_")]
            self.assertTrue(hit and hit[0]["reported"], mod.__name__)
            out = self._post_scan(mod, {"mode": "run", "folder": run,
                                        "reports": "/no/such/folder"})
            self.assertIn("Reports folder", out.get("error", ""),
                          mod.__name__)

    def test_both_pages_send_and_keep_the_reports_folder(self):
        """SOURCE check, said so: the scan body is built in doScan()."""
        for mod in (M, IMG):
            page = mod.build_page({"branding": {}}).decode("utf-8")
            self.assertIn('id="rptdir"', page, mod.__name__)
            self.assertIn("reports:$('rptdir').value", page, mod.__name__)
            self.assertIn("$('rptrow').hidden=m!=='run';", page,
                          mod.__name__)
            self.assertIn("rptdir", mod.FORM_FIELDS, mod.__name__)


class TitlesAndLooseImages(_Quiet):

    def test_placeholder_titles(self):
        for title in ("Untitled", "Microsoft Word - thesis.docx",
                      "Document1", "final draft.pdf", "Presentation 2"):
            self.assertTrue(M.title_is_placeholder(title), title)
        self.assertTrue(M.title_is_placeholder("thesis", "/x/thesis.pdf"))
        for title in ("", "Monster Quest: Background, Myth and Context",
                      "Documenting the Delta"):
            self.assertFalse(M.title_is_placeholder(title), title)
        self.assertEqual(M.title_word("Untitled"), "Placeholder")
        self.assertEqual(M.title_word(""), "Missing")

    @needs_pdf
    def test_a_placeholder_title_is_named_and_replaced(self):
        path = _tagged_pdf(self.p("t.pdf"), alts=("x",))
        with _pikepdf.open(path, allow_overwriting_input=True) as pdf:
            pdf.docinfo["/Title"] = "Microsoft Word - thesis.docx"
            pdf.save(path)
        row = self.one(path)
        self.assertEqual(row["doc_title"], "Placeholder")
        self.assertIn("Replace the placeholder document title", row["next"])
        res = M.tag_pdf(path, "en-US", "Monster Quest")
        self.assertTrue(res["title_set"])

    @needs_pdf
    def test_images_outside_the_tags_count_as_missing_alt(self):
        """A word-processor thesis behind a tagged cover page: the tree has
        the cover's tags only, and the thesis's images are outside it."""
        path = _tagged_pdf(self.p("t.pdf"), alts=("Logo",))
        P = _pikepdf
        with P.open(path, allow_overwriting_input=True) as pdf:
            page = pdf.add_blank_page(page_size=(612, 792))
            page.Resources = P.Dictionary(
                Font=P.Dictionary(F1=_font(pdf)),
                XObject=P.Dictionary(Im0=_image(pdf)))
            page.Contents = pdf.make_stream(TEXT + b"\n" + SMALL_FIGURE)
            pdf.save(path)
        row = self.one(path)
        self.assertTrue(row["tagged"].startswith("Incomplete"))
        self.assertEqual(row["alt_missing"], 1)
        self.assertIn("1 image(s) outside the tags need tagging and alt "
                      "text", row["next"])

    @needs_office
    def test_a_placeholder_word_title_is_named(self):
        import docx
        d = docx.Document()
        d.core_properties.title = "Document1"
        path = self.p("w.docx")
        d.save(path)
        row = self.one(path)
        self.assertEqual(row["doc_title"], "Placeholder")
        self.assertIn("Replace the placeholder document title", row["next"])



@needs_pdf
class TitleSetByTheRun(_Quiet):
    """Live run, ocrmypdf 17.10: a scan titled "scanned" was given the title
    "scanned" from its file name, and the same row then said Doc Title
    "Set" and "Replace the placeholder document title"."""

    def test_a_title_from_the_file_name_is_said_to_be_one(self):
        path = _plain_pdf(self.p("scanned.pdf"))
        row = self.one(path, "auto")
        self.assertEqual(row["doc_title"], "Set (from the file name)")
        self.assertNotIn("Replace the placeholder", row["next"])
        self.assertIn("set from the file's name", row["next"])

    def test_a_record_title_is_said_to_be_one(self):
        path = _plain_pdf(self.p("x.pdf"))
        run = os.path.join(self.tmp, "run")
        os.makedirs(run, exist_ok=True)
        entry = M._entry(path, "x.pdf", {"title": "Monster Quest"})
        row = M.process_one(entry, 1, _cfg("auto", run), None, run)
        self.assertEqual(row["doc_title"], "Set (record title)")
        self.assertNotIn("file's name", row["next"])



class NamesTheDownloaderBuilds(unittest.TestCase):
    """The name parser against names the downloader itself builds. It was
    first tested with hand-written primary and native names only; the
    downloader's supplemental files are supp<N>, which it did not parse, so
    a hidden supplemental file in a plain folder went out as "visibility
    not known"."""

    KINDS = ("primary", "native", "coverletter", "supp1", "supp12")
    VERSIONS = ("current", "current-unstamped", "original", "rev4")

    def test_every_name_the_downloader_builds_parses_back(self):
        try:
            import dc_file_downloader as DL
        except Exception as e:      # said, not passed
            self.skipTest("the downloader could not be imported, so its "
                          "names were not checked: {}".format(e))
        n = 0
        for ctx in ("ctx", "two_part"):
            for kind in self.KINDS:
                for vis in ("public", "hidden"):
                    for version in self.VERSIONS:
                        name = DL.build_filename(ctx, 4021, kind, vis,
                                                 version, "A Title, Long.pdf")
                        for mod in (M, IMG):
                            self.assertEqual(
                                mod.downloader_name_meta(name),
                                {"article": "4021", "visibility": vis},
                                (mod.__name__, name))
                        n += 1
        self.assertEqual(n, 2 * 5 * 2 * 4)

    def test_a_hidden_supplemental_file_in_a_plain_folder_is_held_back(self):
        tmp = tempfile.mkdtemp()
        try:
            for name in ("ctx_5_supp1_hidden_current_x.jpg",
                         "ctx_5_supp12_hidden_rev2_y.jpg"):
                Path(os.path.join(tmp, name)).write_bytes(b"x")
            ocr = M.scan_folder(tmp, False)
            img = IMG.scan_folder(tmp, False)[0]
            self.assertEqual(len(ocr), 2)
            self.assertEqual(len(img), 2)
            for entry in ocr:
                self.assertEqual(M.ai_gate(entry), (False, "hidden, access "
                                 "not recorded — not sent"), entry["name"])
            for entry in img:
                self.assertEqual(IMG.ai_gate(entry), (False, "hidden, access "
                                 "not recorded — not sent"), entry["name"])
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


class RerunSessionReports(unittest.TestCase):
    """A structure's re-run sessions write DC_FileDownload_Retry_<ctx>_<stamp>
    reports. Both AI modules looked only for DC_FileDownload_Report_*, so a
    Reports folder holding a re-run's report was refused, and beside the
    files the report was not read: every file read "access not recorded"."""

    NAMES = ("ctx_1_primary_public_current_A.jpg",
             "ctx_2_primary_public_current_B.jpg")

    def setUp(self):
        try:
            import openpyxl     # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        self.tmp = tempfile.mkdtemp()
        self.run = os.path.join(self.tmp, "files",
                                "DC_FileDownloads_20990101_000000")
        self.sub = os.path.join(self.run, "ctx")
        os.makedirs(self.sub)
        for name in self.NAMES:
            Path(os.path.join(self.sub, name)).write_bytes(b"x")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _report(self, folder, name, b_access):
        import openpyxl
        os.makedirs(folder, exist_ok=True)
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["Saved Filename", "Article ID", "Title", "Visibility",
                   "Access"])
        ws.append([self.NAMES[0], "1", "A", "public", "open"])
        ws.append([self.NAMES[1], "2", "B", "public", b_access])
        wb.save(os.path.join(folder, name))

    def _gates(self, mod, reports=""):
        entries = mod.scan_run_folder(self.run, reports)[0]
        return dict((e["name"][:5], mod.ai_gate(e)) for e in entries)

    def test_a_rerun_report_saved_elsewhere_is_read(self):
        reports = os.path.join(self.tmp, "reports")
        self._report(os.path.join(reports, os.path.basename(self.run)),
                     "DC_FileDownload_Retry_ctx_20990101_000000.xlsx",
                     "restricted")
        for mod in (M, IMG):
            self.assertEqual(self._gates(mod, reports), {
                "ctx_1": (True, "public"),
                "ctx_2": (False, "access restricted — not sent")},
                mod.__name__)

    def test_a_rerun_report_beside_the_files_is_read(self):
        self._report(self.sub,
                     "DC_FileDownload_Retry_ctx_20990101_000000.xlsx",
                     "restricted")
        for mod in (M, IMG):
            self.assertEqual(self._gates(mod)["ctx_2"],
                             (False, "access restricted — not sent"),
                             mod.__name__)

    def test_the_latest_session_s_row_wins(self):
        self._report(self.sub,
                     "DC_FileDownload_Report_ctx_20990101_000000.xlsx",
                     "restricted")
        self._report(self.sub,
                     "DC_FileDownload_Retry_ctx_20990102_000000.xlsx", "open")
        for mod in (M, IMG):
            self.assertEqual(self._gates(mod)["ctx_2"], (True, "public"))
        # By the stamp, not by the kind of report.
        self._report(self.sub,
                     "DC_FileDownload_Report_ctx_20990103_000000.xlsx",
                     "restricted")
        for mod in (M, IMG):
            self.assertEqual(self._gates(mod)["ctx_2"],
                             (False, "access restricted — not sent"))

    def test_a_folder_with_no_report_names_both_kinds(self):
        empty = os.path.join(self.tmp, "nothing")
        os.makedirs(empty)
        with self.assertRaises(ValueError) as cm:
            M.scan_run_folder(self.run, empty)
        self.assertIn("DC_FileDownload_Report_*.xlsx or "
                      "DC_FileDownload_Retry_*.xlsx", str(cm.exception))


class GateHeldBack(_ImageRuns, unittest.TestCase):
    """What the gate held back is the work a run would otherwise have sent.
    Every barred file was logged as "Not sent to the AI endpoint: … — not
    sent" and counted as held back — in Evaluate mode, which sends nothing,
    all of them — and a format the module does not process was given a
    gate."""

    HIDDEN = {"record": True, "visibility": "hidden", "access": "open"}

    def test_evaluate_mode_holds_nothing_back_and_previews(self):
        written, said, calls = self.run_images(
            [], files=1, meta=dict(self.HIDDEN), mode="evaluate")
        self.assertEqual(calls, [])
        self.assertEqual([s for s in said if "AI gate:" in s
                          or "Not sent to the AI" in s], [])
        pairs = written["pairs"]
        self.assertNotIn("Files the gate held back", pairs)
        self.assertEqual(pairs["Files a processing run could not send"], 1)
        self.assertTrue(pairs["AI gate"].startswith(
            "Evaluate only sends nothing to the AI endpoint."))
        self.assertEqual(
            written["rows"][0][M.REPORT_HEADERS.index("AI Gate")],
            "hidden — not sent")
        written, _said, _calls = self.run_images(
            [], files=1, meta=dict(self.HIDDEN), mode="evaluate",
            override=True)
        self.assertIn("With the override on", written["pairs"]["AI gate"])
        self.assertEqual(
            written["pairs"]["Files a processing run could not send"], 0)

    def test_a_held_back_image_is_logged_once_per_kind_of_work(self):
        written, said, calls = self.run_images(
            [self.OK], meta=dict(self.HIDDEN), ai_alt=True)
        self.assertEqual(calls, [])
        self.assertEqual([s for s in said if "AI gate:" in s], [
            "    AI gate: hidden — not sent (transcription)",
            "    AI gate: hidden — not sent (alt text)"])
        self.assertEqual([s for s in said if "Not sent to the AI endpoint:"
                          in s], [])
        self.assertEqual(written["pairs"]["Files the gate held back"], 1)
        self.assertIn(M.GATE_ALT_STEP.format("hidden"),
                      written["rows"][0][M.REPORT_HEADERS.index(
                          "Next Steps")])

    def test_a_format_the_module_does_not_process_has_no_gate(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "ctx_5_native_hidden_current_x.txt")
            Path(path).write_bytes(b"x")
            entry = M._entry(path, os.path.basename(path))
            self.assertTrue(entry["record"])
            row = M.process_one(entry, 1, _cfg("auto", tmp, engine="ai",
                                               ai_alt=True),
                                ep("gemini"), tmp)
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(row["ai_gate"], "")
        self.assertFalse(row.get("gate_barred"))
        self.assertEqual(row["status"], "Not processed")


@needs_pdf
class GateHeldBackPdf(_Quiet):
    """A PDF the gate bars is held back only where the run would have sent
    something: a page with no text to transcribe, or an image to draft alt
    text for."""

    HIDDEN = {"record": True, "visibility": "hidden", "access": "open"}

    def _no_network(self):
        calls = []
        real = M.urllib.request.urlopen
        M.urllib.request.urlopen = lambda *a, **k: calls.append(1)
        self.addCleanup(setattr, M.urllib.request, "urlopen", real)
        return calls

    def _one(self, path, **kw):
        run = os.path.join(self.tmp, "run")
        os.makedirs(run, exist_ok=True)
        entry = M._entry(path, os.path.basename(path), dict(self.HIDDEN))
        return M.process_one(entry, 1, _cfg("auto", run, **kw),
                             ep("gemini"), run)

    def test_a_pdf_that_needs_nothing_sent_is_not_held_back(self):
        calls = self._no_network()
        row = self._one(_plain_pdf(self.p("t.pdf")), engine="ai")
        self.assertEqual(calls, [])
        self.assertEqual(row["ai_gate"], "hidden — not sent")
        self.assertFalse(row.get("gate_held"))
        self.assertEqual([s for s in self.said if "AI gate:" in s], [])

    def test_alt_drafts_withheld_are_said_and_change_nothing_else(self):
        calls = self._no_network()
        row = self._one(_plain_pdf(self.p("t.pdf")), ai_alt=True)
        self.assertEqual(calls, [])
        self.assertEqual(row["gate_held"], ["alt text"])
        self.assertIn(M.GATE_ALT_STEP.format("hidden"), row["next"])
        self.assertIn("    AI gate: hidden — not sent (alt text)", self.said)
        run2 = os.path.join(self.tmp, "run2")
        os.makedirs(run2)
        plain = M.process_one(M._entry(_plain_pdf(self.p("u.pdf")), "u.pdf"),
                              1, _cfg("auto", run2), None, run2)
        self.assertEqual((row["tagged"], row["alt_missing"]),
                         (plain["tagged"], plain["alt_missing"]))

    def test_the_worker_counts_only_what_it_held_back(self):
        calls = self._no_network()
        entries = [M._entry(_plain_pdf(self.p("text.pdf")), "text.pdf",
                            dict(self.HIDDEN)),
                   M._entry(_plain_pdf(self.p("scan.pdf"),
                                       content=SMALL_FIGURE), "scan.pdf",
                            dict(self.HIDDEN))]
        written = {}
        real = M.write_report
        M.write_report = (lambda path, rows, pairs, rgb, extra_note=(), **kw:
                          written.update(rows=rows, pairs=dict(pairs)))
        try:
            cfg = _cfg("auto", self.tmp, engine="ai", entries=entries,
                       source="test", ai_override=False)
            M.process_worker({"branding": {}}, cfg, ep("gemini"))
        finally:
            M.write_report = real
            M.STOP_EVENT.clear()
        self.assertEqual(calls, [])
        self.assertEqual(written["pairs"]["Files the gate held back"], 1)
        gates = [r[M.REPORT_HEADERS.index("AI Gate")] for r in
                 written["rows"]]
        self.assertEqual(gates, ["hidden — not sent"] * 2)
        self.assertEqual([s for s in self.said if "AI gate:" in s],
                         ["    AI gate: hidden — not sent (transcription)"])


@needs_office
class GateHeldBackOffice(_Quiet):
    """Word: alt drafts the gate withheld are said; a file with no picture
    to draft for is not held back."""

    def _one(self, paragraphs_only=False):
        import docx
        d = docx.Document()
        if paragraphs_only:
            d.add_paragraph("Findings")
        else:
            d.add_picture(io.BytesIO(_png()))
        path = self.p("d.docx")
        d.save(path)
        run = os.path.join(self.tmp, "run")
        os.makedirs(run, exist_ok=True)
        entry = M._entry(path, "d.docx", {"record": True,
                                          "visibility": "hidden",
                                          "access": "open"})
        return M.process_one(entry, 1, _cfg("auto", run, ai_alt=True),
                             ep("gemini"), run)

    def test_alt_drafts_withheld_from_a_word_file_are_said(self):
        row = self._one()
        self.assertEqual(row["gate_held"], ["alt text"])
        self.assertIn(M.GATE_ALT_STEP.format("hidden"), row["next"])

    def test_a_word_file_with_no_picture_is_not_held_back(self):
        row = self._one(paragraphs_only=True)
        self.assertEqual(row["ai_gate"], "hidden — not sent")
        self.assertFalse(row.get("gate_held"))



class HeldBackImageWording(_ImageRuns, unittest.TestCase):
    """A sentence that says "not sent" names the gate's reason once."""

    def test_a_held_back_image_names_the_reason_once(self):
        written, _said, calls = self.run_images(
            [self.OK], meta={"record": True, "visibility": "hidden",
                             "access": "open"}, ai_alt=True)
        self.assertEqual(calls, [])
        row = written["rows"][0]
        self.assertEqual(row[M.REPORT_HEADERS.index("OCR Action")],
                         "Not sent (hidden)")
        steps = row[M.REPORT_HEADERS.index("Next Steps")]
        self.assertIn("Not sent to the AI endpoint (hidden) — rerun", steps)
        self.assertIn("AI alt-text drafts not requested (hidden) — rerun",
                      steps)
        self.assertNotIn("not sent)", steps)


@needs_pdf
class NoTextLayerSaysWhy(_Quiet):
    """A processed PDF whose pages still have no text layer was told
    "inspect the source scans" — for a file the gate held back, and for one
    whose transcription the AI engine wrote to a sidecar, too."""

    def _textless(self, name):
        # A page scan with no text, as a real scan arrives: the tagger
        # leaves it alone, so the file stays untagged. (A small figure on a
        # textless page is tagged, and takes the tagged file's advice.)
        return _scan_pdf(self.p(name), with_text=False)

    def test_a_held_back_pdf_says_the_gate_held_it_back(self):
        calls = []
        real = M.urllib.request.urlopen
        M.urllib.request.urlopen = lambda *a, **k: calls.append(1)
        try:
            run = os.path.join(self.tmp, "run")
            os.makedirs(run)
            entry = M._entry(self._textless("t.pdf"), "t.pdf", {
                "record": True, "visibility": "hidden", "access": "open"})
            row = M.process_one(entry, 1, _cfg("auto", run, engine="ai"),
                                ep("gemini"), run)
        finally:
            M.urllib.request.urlopen = real
        self.assertEqual(calls, [])
        self.assertEqual(row["ocr"], "Not sent (hidden)")
        self.assertIn("Not sent to the AI endpoint (hidden) — rerun",
                      row["next"])
        self.assertIn("1 page(s) still have no text layer — the AI gate "
                      "held back their transcription", row["next"])
        self.assertNotIn("inspect the source scans", row["next"])
        self.assertNotIn("not sent)", row["next"])

    def test_an_ai_sidecar_pdf_says_where_its_text_went(self):
        real = M.ai_ocr_pdf_sidecar

        def fake(ep_, pdf, sidecar, *a, **k):
            Path(sidecar).write_text("Findings", encoding="utf-8")
        M.ai_ocr_pdf_sidecar = fake
        try:
            run = os.path.join(self.tmp, "run")
            os.makedirs(run)
            entry = M._entry(self._textless("t.pdf"), "t.pdf")
            row = M.process_one(entry, 1, _cfg("auto", run, engine="ai"),
                                ep("gemini"), run)
        finally:
            M.ai_ocr_pdf_sidecar = real
        self.assertEqual(row["ocr"], "AI text sidecar")
        self.assertIn("1 page(s) still have no text layer — the AI engine "
                      "writes its transcription to a text sidecar, not into "
                      "the PDF", row["next"])
        self.assertNotIn("inspect the source scans", row["next"])

    def test_the_scan_advice_stays_where_the_run_cannot_say_why(self):
        info = M.eval_pdf(self._textless("t.pdf"))
        manual, review = [], []
        M._pdf_steps(info, manual, review, processed=True)
        self.assertIn("1 page(s) still have no text layer — inspect the "
                      "source scans", manual)
        manual, review = [], []
        M._pdf_steps(info, manual, review, processed=True,
                     textless_why="the reason")
        self.assertIn("1 page(s) still have no text layer — the reason",
                      manual)
        self.assertFalse([s for s in manual if "inspect" in s])
        self.assertEqual(M._textless_why("OCR (new text layer)"), "")



RULE = b"72 700 m 540 700 l S"
BOX = b"72 600 100 20 re f"
CLIP = b"0 0 612 792 re W n"


def _vector_pdf(path, content, shading=False, forms=()):
    """An untagged page of text and vector drawings: `content` is drawn
    after the text; `forms` names Form XObjects to add ("Rule" draws a line,
    "Pic" draws an image); `shading` adds /Sh0 for an `sh`."""
    P = _pikepdf
    pdf = P.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    xobj = P.Dictionary()
    for name in forms:
        if name == "Rule":
            form = P.Stream(pdf, RULE)
        else:
            # An image with a border: a figure, not a drawing.
            form = P.Stream(pdf, b"q 10 0 0 10 0 0 cm /Im0 Do Q\n" + RULE)
            form.Resources = P.Dictionary(XObject=P.Dictionary(
                Im0=_image(pdf)))
        form.Type = P.Name.XObject
        form.Subtype = P.Name.Form
        form.BBox = P.Array([0, 0, 612, 792])
        xobj[P.Name("/" + name)] = pdf.make_indirect(form)
    res = P.Dictionary(Font=P.Dictionary(F1=_font(pdf)), XObject=xobj)
    if shading:
        res.Shading = P.Dictionary(Sh0=pdf.make_indirect(P.Dictionary(
            ShadingType=2, ColorSpace=P.Name.DeviceRGB,
            Coords=P.Array([0, 0, 100, 0]),
            Function=P.Dictionary(FunctionType=2, Domain=P.Array([0, 1]),
                                  C0=P.Array([0, 0, 0]),
                                  C1=P.Array([1, 1, 1]), N=1))))
    page.Resources = res
    page.Contents = pdf.make_stream(TEXT + b"\n" + content)
    pdf.save(path)
    return path


def _append_to_page(path, extra, shading=False):
    """Draw `extra` after a page's own content, outside its marked content
    unless `extra` says otherwise."""
    P = _pikepdf
    with P.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        page.Contents = pdf.make_stream(
            page.Contents.read_bytes() + b"\n" + extra)
        if shading:
            page.Resources.Shading = P.Dictionary(Sh0=pdf.make_indirect(
                P.Dictionary(ShadingType=2, ColorSpace=P.Name.DeviceRGB,
                             Coords=P.Array([0, 0, 100, 0]),
                             Function=P.Dictionary(
                                 FunctionType=2, Domain=P.Array([0, 1]),
                                 C0=P.Array([0, 0, 0]),
                                 C1=P.Array([1, 1, 1]), N=1))))
        pdf.save(path)
    return path


def _unwrapped(path, ops=("S", "f", "sh", "n")):
    """{operator: times drawn outside every marked-content sequence}."""
    P = _pikepdf
    found = {}
    with P.open(path) as pdf:
        for page in pdf.pages:
            depth = 0
            for inst in P.parse_content_stream(page):
                if isinstance(inst, P.ContentStreamInlineImage):
                    continue
                op = str(inst.operator)
                if op in ("BMC", "BDC"):
                    depth += 1
                elif op == "EMC":
                    depth = max(depth - 1, 0)
                elif op in ops and not depth:
                    found[op] = found.get(op, 0) + 1
    return found


@needs_pdf
class VectorDrawings(_Quiet):
    """Lines, rules and fills outside the tags. The walk did not count them,
    so a file veraPDF failed under 7.1-3 hundreds of times read "Yes"; the
    tagger tagged text and images and left them outside the tags."""

    def structure(self, path):
        with _pikepdf.open(path) as pdf:
            return M.pdf_structure(pdf)

    def test_the_walk_names_drawings_outside_the_tags(self):
        path = _tagged_pdf(self.p("t.pdf"))
        self.assertEqual(M.tag_verdict(self.structure(path))[0], "Yes")
        _append_to_page(path, RULE)
        s = self.structure(path)
        self.assertEqual((s["vector_pages"], s["untagged_pages"]), (1, 0))
        self.assertEqual(M.tag_verdict(s)[0], "Incomplete — vector drawings "
                         "outside the tags on 1 of 1 page(s)")

    def test_a_shading_is_a_drawing(self):
        path = _append_to_page(_tagged_pdf(self.p("t.pdf")),
                               b"q /Sh0 sh Q", shading=True)
        s = self.structure(path)
        self.assertEqual((s["vector_pages"], s["untagged_pages"]), (1, 0))

    def test_drawings_marked_as_artifacts_are_not_counted(self):
        path = _append_to_page(_tagged_pdf(self.p("t.pdf")),
                               b"/Artifact BMC " + RULE + b" EMC")
        s = self.structure(path)
        self.assertEqual(s["vector_pages"], 0)
        self.assertEqual(M.tag_verdict(s)[0], "Yes")

    def test_the_tagger_marks_painted_paths_and_leaves_a_clip(self):
        path = _vector_pdf(self.p("v.pdf"),
                           CLIP + b"\n" + RULE + b"\n" + BOX
                           + b"\nq /Sh0 sh Q", shading=True)
        res = M.tag_pdf(path, "en-US", "T")
        self.assertEqual(res["vectors"], 3)
        s = self.structure(path)
        self.assertEqual((s["vector_pages"], s["untagged_pages"]), (0, 0))
        self.assertEqual(M.tag_verdict(s)[0], "Yes")
        # A clipping path paints nothing; it stays where the stream had it.
        self.assertEqual(_unwrapped(path), {"n": 1})

    def test_a_form_that_only_draws_is_an_artifact(self):
        path = _vector_pdf(self.p("v.pdf"), b"q /Rule Do Q", forms=("Rule",))
        res = M.tag_pdf(path, "en-US", "T")
        self.assertEqual(res["vectors"], 1)
        self.assertEqual(M.tag_verdict(self.structure(path))[0], "Yes")

    def test_a_form_that_draws_an_image_is_not_called_a_drawing(self):
        """It may be a figure, so it is left untagged — and the walk says
        so."""
        path = _vector_pdf(self.p("v.pdf"), b"q /Pic Do Q", forms=("Pic",))
        res = M.tag_pdf(path, "en-US", "T")
        self.assertEqual(res["vectors"], 0)
        self.assertEqual(M.tag_verdict(self.structure(path))[0],
                         "Incomplete — untagged content on 1 of 1 page(s)")

    def test_the_stream_s_own_artifacts_are_left_alone(self):
        path = _vector_pdf(self.p("v.pdf"), b"/Artifact BMC " + RULE
                           + b" EMC\n" + BOX)
        res = M.tag_pdf(path, "en-US", "T")
        self.assertEqual(res["vectors"], 1)
        with _pikepdf.open(path) as pdf:
            ops = [str(i.operator) for i in
                   _pikepdf.parse_content_stream(pdf.pages[0])
                   if not isinstance(i, _pikepdf.ContentStreamInlineImage)]
        # One /Artifact of its own, one the tagger added: never nested.
        text = " ".join(ops)
        self.assertEqual(text.count("BMC"), 2)
        self.assertNotIn("BMC BMC", text)

    def test_a_processed_row_says_how_many_paths_it_marked(self):
        path = _vector_pdf(self.p("v.pdf"), RULE + b"\n" + BOX)
        row = self.one(path, "auto")
        self.assertEqual(row["tagged"], "Yes (heuristic)")
        self.assertIn(M.VECTOR_STEP.format(2), row["next"])
        self.assertEqual(_unwrapped(row["output"], ops=("S", "f")), {})



def _replace_content(path, old, new):
    """Rewrite page 1's content stream at one anchor, asserted present."""
    P = _pikepdf
    with P.open(path, allow_overwriting_input=True) as pdf:
        page = pdf.pages[0]
        data = page.Contents.read_bytes()
        assert data.count(old) == 1, old
        page.Contents = pdf.make_stream(data.replace(old, new))
        pdf.save(path)
    return path


HEADING_IN_BT = (b"BT /F1 12 Tf 72 720 Td /Heading1 <</MCID 0>> BDC "
                 b"(Findings) Tj EMC ET")


@needs_pdf
class TextJudgedWhereShown(_Quiet):
    """A tag may open inside a text object. The walk judged text by where
    its text object began, so a file veraPDF passed under 7.1-3 read
    "untagged content on every page", and text shown after its tag closed,
    inside the same text object, was never seen."""

    def walk(self, path):
        with _pikepdf.open(path) as pdf:
            return M.pdf_structure(pdf)

    def heading(self, new):
        return _replace_content(
            _tagged_pdf(self.p("t.pdf")),
            b"/Heading1 <</MCID 0>> BDC " + TEXT + b" EMC", new)

    def test_a_tag_opened_inside_a_text_object_is_tagged(self):
        s = self.walk(self.heading(HEADING_IN_BT))
        self.assertEqual(s["untagged_pages"], 0)
        self.assertEqual(M.tag_verdict(s)[0], "Yes")

    def test_text_shown_outside_every_tag_is_untagged_by_each_operator(self):
        for show in (b"(stray) Tj", b"[(stray)] TJ", b"(stray) '",
                     b"0 0 (stray) \""):
            with self.subTest(show=show):
                s = self.walk(self.heading(HEADING_IN_BT.replace(
                    b"EMC ET", b"EMC " + show + b" ET")))
                self.assertEqual(s["untagged_pages"], 1)

    def test_a_text_object_begun_inside_a_tag_can_show_text_after_it(self):
        s = self.walk(self.heading(
            b"/Heading1 <</MCID 0>> BDC BT /F1 12 Tf 72 720 Td (Findings) "
            b"Tj EMC (stray) Tj ET"))
        self.assertEqual(s["untagged_pages"], 1)


class PlaceholderAltWords(unittest.TestCase):
    """What counts as no alt text at all."""

    def test_labels_file_names_and_bare_numbers_count_as_none(self):
        for alt in ("image 18", "Image18", "image001", "Figure 3",
                    "Figure 3.2", "Fig. 4-1", "(Figure 1)", "Figure 12a",
                    "Picture 3", "Pic #4", "graphic", "Untitled",
                    "alt text", "img0041.png", "---", "12", "", "  "):
            with self.subTest(alt=alt):
                self.assertTrue(M.alt_is_missing(alt))

    def test_words_about_the_picture_are_alt_text(self):
        for alt in ("Figure 3: enrollment by year", "Image of a dog",
                    "Photograph of the library", "Chart of enrollment",
                    "Bar chart"):
            with self.subTest(alt=alt):
                self.assertFalse(M.alt_is_missing(alt))

    def test_the_step_quotes_the_placeholder(self):
        self.assertEqual(
            M.alt_step(18, 18, "image 18", "figure"),
            "Add alt text for 18 figure(s) — each carries only "
            "placeholder alt text, such as “image 18”, which "
            "counts as none")
        self.assertEqual(
            M.alt_step(5, 2, "Picture 3", "image"),
            "Add alt text for 5 image(s) — 2 of them carry only "
            "placeholder alt text, such as “Picture 3”, which "
            "counts as none")
        self.assertEqual(M.alt_step(1, 0, "", "image"),
                         "Add alt text for 1 image(s)")


@needs_pdf
class PlaceholderAltPdf(_Quiet):
    """An auto-tagger fills every Figure's /Alt with a label. veraPDF's
    7.3-1 asks only that alt text be there, so it passes; a screen reader
    reads "image 18"."""

    def walk(self, path):
        with _pikepdf.open(path) as pdf:
            return M.pdf_structure(pdf)

    def test_placeholder_alt_is_missing_and_named(self):
        path = _tagged_pdf(self.p("t.pdf"),
                           alts=("image 1", None, "Bar chart"))
        s = self.walk(path)
        self.assertEqual((s["figures"], s["figures_alt"],
                          s["figures_placeholder"], s["placeholder_example"]),
                         (3, 1, 1, "image 1"))
        row = self.one(path)
        self.assertEqual(row["alt_missing"], 2)
        self.assertIn(M.alt_step(2, 1, "image 1", "figure"), row["next"])
        self.assertIn("1 of them carry", row["next"])

    def test_every_figure_a_placeholder_says_each(self):
        row = self.one(_tagged_pdf(self.p("t.pdf"), alts=("image 18",)))
        self.assertIn("Add alt text for 1 figure(s) — each carries only "
                      "placeholder alt text, such as “image 18”",
                      row["next"])

    def test_an_empty_actualtext_is_not_alt_text(self):
        path = _tagged_pdf(self.p("t.pdf"), alts=(None,))
        with _pikepdf.open(path, allow_overwriting_input=True) as pdf:
            n = 0
            for obj in pdf.objects:
                if (isinstance(obj, _pikepdf.Dictionary)
                        and obj.get("/S") == _pikepdf.Name.Figure):
                    obj.ActualText = _pikepdf.String("")
                    n += 1
            self.assertEqual(n, 1)
            pdf.save(path)
        self.assertEqual(self.walk(path)["figures_alt"], 0)


@needs_office
class PlaceholderAltOffice(_Quiet):
    """Word and PowerPoint: a label or a file name in a picture's alt text
    is counted missing, named in the step, and replaced by a draft — and a
    draft that replaced one says so."""

    def _docx(self, descr):
        import docx
        d = docx.Document()
        d.add_picture(io.BytesIO(_png()))
        list(M.iter_docx_images(d))[0][0].set("descr", descr)
        path = self.p("d.docx")
        d.save(path)
        return path

    def _pptx(self):
        """python-pptx gives a picture its file name as alt text, as
        PowerPoint can."""
        import pptx
        from pptx.util import Inches
        prs = pptx.Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_picture(io.BytesIO(_png()), Inches(1), Inches(1))
        path = self.p("p.pptx")
        prs.save(path)
        return path

    def drafted(self, fn, path, text):
        real = M._draft_alt
        M._draft_alt = lambda ctx, part, log_fn: text
        try:
            return fn(path, self.p("out" + os.path.splitext(path)[1]), "",
                      {"ep": object()}, lambda m: None)
        finally:
            M._draft_alt = real

    def test_a_word_label_counts_missing_and_is_named(self):
        path = self._docx("Picture 3")
        info = M.eval_docx(path)
        self.assertEqual((info["alt_missing"], info["alt_placeholder"],
                          info["placeholder_example"]), (1, 1, "Picture 3"))
        row = self.one(path)
        self.assertEqual(row["alt_missing"], 1)
        self.assertIn(M.alt_step(1, 1, "Picture 3", "image"), row["next"])

    def test_real_word_alt_is_left_alone(self):
        info = M.eval_docx(self._docx("Figure 3: enrollment by year"))
        self.assertEqual((info["alt_missing"], info["alt_placeholder"]),
                         (0, 0))

    def test_a_word_draft_that_replaced_a_label_is_said(self):
        path = self._docx("Picture 3")
        res = self.drafted(M.process_docx, path, "Enrollment by year.")
        self.assertEqual((res["alt_drafted"], res["replaced"],
                          res["alt_missing"], res["alt_placeholder"]),
                         (1, 1, 0, 0))
        undrafted = M.process_docx(path, self.p("o2.docx"), "", None,
                                   lambda m: None)
        self.assertEqual((undrafted["alt_missing"],
                          undrafted["alt_placeholder"],
                          undrafted["placeholder_example"]),
                         (1, 1, "Picture 3"))

    def test_the_row_says_a_draft_replaced_a_label(self):
        path = self._docx("Picture 3")
        real = M._draft_alt
        M._draft_alt = lambda ctx, part, log_fn: "Enrollment by year."
        try:
            row = self.one(path, "auto", ep("gemini"), ai_alt=True)
        finally:
            M._draft_alt = real
        self.assertIn("Review 1 AI-drafted alt text(s) for accuracy — "
                      "1 replaced placeholder alt text", row["next"])

    def test_a_slide_picture_left_with_its_file_name(self):
        path = self._pptx()
        info = M.eval_pptx(path)
        self.assertEqual((info["alt_missing"], info["alt_placeholder"],
                          info["placeholder_example"]), (1, 1, "image.png"))
        res = M.process_pptx(path, self.p("o.pptx"), "", None,
                             lambda m: None)
        self.assertEqual((res["alt_missing"], res["alt_placeholder"],
                          res["replaced"]), (1, 1, 0))
        res = self.drafted(M.process_pptx, path, "Enrollment by year.")
        self.assertEqual((res["alt_drafted"], res["replaced"],
                          res["alt_placeholder"]), (1, 1, 0))


def _with_form(path, form_content, nested=False, pages=1):
    """Draw a Form XObject /Fm0 outside the page's marked content on each
    of `pages` pages (the same object on each). With `nested`, /Fm0 only
    draws /Fm1, and /Fm1 draws `form_content`."""
    P = _pikepdf
    with P.open(path, allow_overwriting_input=True) as pdf:
        first = pdf.pages[0]
        font = first.Resources.Font.F1

        def form(content, xobjects=None):
            f = P.Stream(pdf, content)
            f.Type = P.Name.XObject
            f.Subtype = P.Name.Form
            f.BBox = P.Array([0, 0, 612, 792])
            f.Resources = P.Dictionary(Font=P.Dictionary(F1=font))
            if xobjects:
                f.Resources.XObject = P.Dictionary(xobjects)
            return pdf.make_indirect(f)

        fm = form(form_content)
        if nested:
            fm = form(b"q /Fm1 Do Q", {"/Fm1": fm})
        while len(pdf.pages) < pages:
            pdf.add_blank_page(page_size=(612, 792))
        for page in list(pdf.pages)[:pages]:
            if "/Resources" not in page.obj:
                page.Resources = P.Dictionary()
            if "/XObject" not in page.Resources:
                page.Resources.XObject = P.Dictionary()
            page.Resources.XObject.Fm0 = fm
            old = page.Contents.read_bytes() if "/Contents" in page.obj \
                else b""
            page.Contents = pdf.make_stream(old + b"\nq /Fm0 Do Q")
        pdf.save(path)
    return path


MARKED_IN_FORM = (b"/P <</MCID 9>> BDC BT /F1 10 Tf 72 300 Td (Note) Tj ET "
                  b"EMC /Artifact BMC 72 290 m 300 290 l S EMC")


@needs_pdf
class FormsMarkedInside(_Quiet):
    """An auto-tagger may mark content inside a Form XObject and draw the
    form outside the page's own marked content. veraPDF reads inside the
    form and finds nothing untagged; the walk called the page untagged."""

    def walk(self, path):
        with _pikepdf.open(path) as pdf:
            return M.pdf_structure(pdf)

    def test_a_form_whose_content_is_all_marked_is_not_untagged(self):
        for inside in (MARKED_IN_FORM,
                       b"/Artifact BMC BT /F1 10 Tf 72 300 Td (Page 4) Tj ET "
                       b"EMC"):
            with self.subTest(inside=inside):
                s = self.walk(_with_form(_tagged_pdf(self.p("t.pdf")),
                                         inside))
                self.assertEqual(s["untagged_pages"], 0)
                self.assertEqual(M.tag_verdict(s)[0], "Yes")

    def test_anything_outside_the_form_s_own_marks_is_untagged(self):
        for stray in (b"BT /F1 10 Tf 72 250 Td (stray) Tj ET",
                      b"72 240 m 300 240 l S",
                      b"q 10 0 0 10 0 0 cm BI /W 1 /H 1 /CS /G /BPC 8 ID "
                      b"\x80 EI Q"):
            with self.subTest(stray=stray):
                s = self.walk(_with_form(_tagged_pdf(self.p("t.pdf")),
                                         MARKED_IN_FORM + b"\n" + stray))
                self.assertEqual(s["untagged_pages"], 1)

    def test_a_form_drawn_by_a_form_is_followed(self):
        s = self.walk(_with_form(_tagged_pdf(self.p("t.pdf")),
                                 MARKED_IN_FORM, nested=True))
        self.assertEqual(s["untagged_pages"], 0)
        s = self.walk(_with_form(_tagged_pdf(self.p("u.pdf")),
                                 b"BT /F1 10 Tf 72 250 Td (stray) Tj ET",
                                 nested=True))
        self.assertEqual(s["untagged_pages"], 1)

    def test_a_form_is_read_once_per_file(self):
        path = _with_form(_tagged_pdf(self.p("t.pdf")), MARKED_IN_FORM,
                          pages=3)
        real, calls = M._form_marked_walk, []

        def counted(*a):
            calls.append(1)
            return real(*a)
        M._form_marked_walk = counted
        try:
            s = self.walk(path)
        finally:
            M._form_marked_walk = real
        self.assertEqual((s["pages"], s["untagged_pages"]), (3, 0))
        self.assertEqual(len(calls), 1)


def _figures_pdf(path, figs, kids_tree=False, role=None):
    """A tagged page shaped like an auto-tagger's output: each Figure marks
    one drawing at `rect` (x, y, w, h). figs = [(rect, alt, kind)]: kind
    "image" draws its own image XObject (sized by the rect, so two figures
    of one size render alike), "shared" draws one image object every
    "shared" Figure draws, "inline" an inline image, "stm" puts its
    content in a form and points at it by an MCR with /Stm. The ParentTree
    maps each MCID to its Figure — flat (/Nums), or through /Kids. `role`
    names a custom type mapped to Figure in the RoleMap, used for every
    Figure."""
    P = _pikepdf
    pdf = P.new()
    page = pdf.add_blank_page(page_size=(612, 792))
    font = _font(pdf)
    xobj = P.Dictionary()
    parts = [b"/P <</MCID 0>> BDC " + TEXT + b" EMC"]
    tree = pdf.make_indirect(P.Dictionary(Type=P.Name.StructTreeRoot))
    doc = pdf.make_indirect(P.Dictionary(Type=P.Name.StructElem,
                                         S=P.Name.Document, P=tree))
    pg = page.obj
    kids = [pdf.make_indirect(P.Dictionary(Type=P.Name.StructElem,
                                           S=P.Name.P, P=doc, Pg=pg, K=0))]
    slots = [kids[0]]
    for i, (rect, alt, kind) in enumerate(figs, 1):
        x, y, w, h = rect
        cm = ("q {} 0 0 {} {} {} cm ".format(w, h, x, y)).encode()
        fig = P.Dictionary(Type=P.Name.StructElem,
                           S=P.Name("/" + (role or "Figure")), P=doc, Pg=pg)
        if alt is not None:
            fig.Alt = P.String(alt)
        if kind == "stm":
            form = P.Stream(pdf, b"/Figure <</MCID 0>> BDC " + cm
                            + b"/Im0 Do Q EMC")
            form.Type, form.Subtype = P.Name.XObject, P.Name.Form
            form.BBox = P.Array([0, 0, 612, 792])
            form.Resources = P.Dictionary(XObject=P.Dictionary(
                Im0=_image(pdf)))
            name = "/Fm{}".format(i)
            xobj[P.Name(name)] = pdf.make_indirect(form)
            parts.append(name.encode() + b" Do")
            fig.K = P.Dictionary(Type=P.Name.MCR, MCID=0,
                                 Stm=xobj[P.Name(name)])
            kids.append(pdf.make_indirect(fig))
            slots.append(None)
            continue
        if kind == "inline":
            draw = cm + b"BI /W 1 /H 1 /CS /G /BPC 8 ID \x80 EI Q"
        elif kind == "shared":
            if "/ImS" not in xobj:
                xobj[P.Name("/ImS")] = _image(pdf)
            draw = cm + b"/ImS Do Q"
        else:
            name = "/Im{}".format(i)
            xobj[P.Name(name)] = _image(pdf, max(int(w) // 4, 1),
                                        max(int(h) // 4, 1))
            draw = cm + name.encode() + b" Do Q"
        parts.append(b"/Figure <</MCID %d>> BDC " % i + draw + b" EMC")
        fig.K = i
        ref = pdf.make_indirect(fig)
        kids.append(ref)
        slots.append(ref)
    page.Resources = P.Dictionary(Font=P.Dictionary(F1=font), XObject=xobj)
    page.Contents = pdf.make_stream(b"\n".join(parts))
    page.StructParents = 0
    doc.K = P.Array(kids)
    tree.K = doc
    arr = pdf.make_indirect(P.Array([s if s is not None else None
                                     for s in slots]))
    if kids_tree:
        leaf = pdf.make_indirect(P.Dictionary(Nums=P.Array([0, arr]),
                                              Limits=P.Array([0, 0])))
        tree.ParentTree = pdf.make_indirect(P.Dictionary(
            Kids=P.Array([leaf])))
    else:
        tree.ParentTree = pdf.make_indirect(P.Dictionary(
            Nums=P.Array([0, arr])))
    tree.ParentTreeNextKey = 1
    if role:
        tree.RoleMap = P.Dictionary({"/" + role: P.Name.Figure})
    pdf.Root.StructTreeRoot = tree
    pdf.Root.MarkInfo = P.Dictionary(Marked=True)
    pdf.Root.Lang = P.String("en-US")
    pdf.docinfo["/Title"] = "Fixture"
    pdf.save(path)
    return path


class _Drafts(object):
    """ai_alt_text replaced by a script of replies (text, "" for
    decorative, or an exception); every request is recorded."""

    def drafting(self, replies):
        self.asked = []
        script = list(replies)
        real = M.ai_alt_text

        def fake(ep_, blob, mime, retries=0, sink=None):
            self.asked.append((len(blob), mime))
            step = script.pop(0) if script else "A drafted description."
            if isinstance(step, Exception):
                raise step
            return step
        M.ai_alt_text = fake
        self.addCleanup(setattr, M, "ai_alt_text", real)

    def ctx(self, budget=25):
        return {"ep": object(), "budget": [budget], "alt_limit": budget,
                "retries": 0, "alt_chars": [], "usage": M.new_usage()}

    def walk(self, path):
        with _pikepdf.open(path) as pdf:
            return M.pdf_structure(pdf)


@needs_pdf
class FigureRegions(_Quiet):
    """Where a Figure's content is drawn, from the content it marks."""

    def regions(self, content, xobjects=None, pdf=None):
        """The page is made in `pdf` when one is given: an XObject made in
        one Pdf cannot go on a page of another (newer pikepdf raises
        ForeignObjectError; older ones allowed it)."""
        P = _pikepdf
        pdf = pdf if pdf is not None else P.new()
        page = pdf.add_blank_page(page_size=(612, 792))
        page.Resources = P.Dictionary(XObject=P.Dictionary(xobjects or {}))
        page.Contents = pdf.make_stream(content)
        return M._mcid_regions(page, {0})[0]

    def test_an_image_follows_the_transformation(self):
        P = _pikepdf
        pdf = P.new()
        r = self.regions(b"q 2 0 0 2 10 10 cm /F <</MCID 0>> BDC q 50 0 0 "
                         b"40 0 0 cm /Im0 Do Q EMC Q",
                         {"/Im0": _image(pdf)}, pdf=pdf)
        self.assertEqual([round(v) for v in r["box"]], [10, 10, 110, 90])
        self.assertEqual(r["kinds"], {"image"})

    def test_a_form_is_its_bbox_through_its_matrix(self):
        P = _pikepdf
        pdf = P.new()
        form = P.Stream(pdf, b"0 0 10 10 re f")
        form.Type, form.Subtype = P.Name.XObject, P.Name.Form
        form.BBox = P.Array([0, 0, 10, 10])
        form.Matrix = P.Array([1, 0, 0, 1, 5, 5])
        r = self.regions(b"q 3 0 0 3 0 0 cm /F <</MCID 0>> BDC /Fm0 Do EMC Q",
                         {"/Fm0": pdf.make_indirect(form)}, pdf=pdf)
        self.assertEqual([round(v) for v in r["box"]], [15, 15, 45, 45])

    def test_a_painted_path_and_an_inline_image(self):
        r = self.regions(b"/F <</MCID 0>> BDC 72 700 m 172 700 l S q 10 0 0 "
                         b"10 300 300 cm BI /W 1 /H 1 /CS /G /BPC 8 ID \x80 "
                         b"EI Q EMC")
        self.assertEqual([round(v) for v in r["box"]], [72, 300, 310, 700])
        self.assertEqual(r["kinds"], {"vector", "inline"})

    def test_text_alone_is_not_located(self):
        r = self.regions(b"/F <</MCID 0>> BDC BT /F1 12 Tf 72 720 Td (x) Tj "
                         b"ET EMC")
        self.assertIsNone(r["box"])
        self.assertEqual(r["kinds"], {"text"})


@needs_pdf
class TreeAlt(_Drafts, _Quiet):
    """set_alt into an existing tree: drafts land on Figures with no alt
    text or placeholder alt text, never on real alt text; what is not
    drafted is counted and named; nothing is saved unless the tags are no
    worse."""

    SIZES = ((72, 500, 100, 80), (300, 500, 120, 60), (72, 300, 140, 90),
             (300, 300, 80, 100))

    def test_targets_are_the_figures_a_reader_gets_nothing_from(self):
        path = _figures_pdf(self.p("t.pdf"), [
            (self.SIZES[0], "image 1", "image"),
            (self.SIZES[1], None, "image"),
            (self.SIZES[2], "Bar chart of enrollment", "image")],
            role="Diagram")
        with _pikepdf.open(path) as pdf:
            got = M._figure_targets(pdf)
        self.assertEqual([(t["page"], t["mcids"], t["held"]) for t in got],
                         [(0, [1], "image 1"), (0, [2], "")])

    def test_drafts_land_and_real_alt_is_kept(self):
        path = _figures_pdf(self.p("t.pdf"), [
            (self.SIZES[0], "image 1", "image"),
            (self.SIZES[1], None, "image"),
            (self.SIZES[2], "Bar chart of enrollment", "image")])
        self.drafting(["Enrollment by year, 2019 to 2024.",
                       "A campus map with the library marked."])
        res = M.draft_tree_alt(path, self.ctx(), self.said.append)
        self.assertEqual((res["wanted"], res["drafted"], res["replaced"]),
                         (2, 2, 1))
        self.assertEqual(len(self.asked), 2)
        s = self.walk(path)
        self.assertEqual((s["figures"], s["figures_alt"],
                          s["figures_placeholder"]), (3, 3, 0))
        with _pikepdf.open(path) as pdf:
            alts = sorted(str(o.Alt) for o in pdf.objects
                          if isinstance(o, _pikepdf.Dictionary)
                          and o.get("/S") == _pikepdf.Name.Figure)
        self.assertIn("Bar chart of enrollment", alts)
        manual, review = [], []
        M.tree_alt_steps(res, manual, review)
        self.assertEqual(review, [M.drafted_step(2, 1)])
        self.assertEqual(manual, [])

    def test_a_draft_that_is_itself_a_placeholder_is_not_written(self):
        path = _figures_pdf(self.p("t.pdf"),
                            [(self.SIZES[0], None, "image")])
        before = Path(path).read_bytes()
        self.drafting(["Image"])
        res = M.draft_tree_alt(path, self.ctx(), self.said.append)
        self.assertEqual((res["drafted"], res["rejected"],
                          res["rejected_example"]), (0, 1, "Image"))
        self.assertEqual(Path(path).read_bytes(), before)
        manual, review = [], []
        M.tree_alt_steps(res, manual, review)
        self.assertIn("1 AI draft(s) were themselves placeholder text, such "
                      "as “Image”, and were not written", manual)

    def test_the_same_picture_is_asked_about_once(self):
        """One image object drawn by two Figures, at two sizes."""
        path = _figures_pdf(self.p("t.pdf"), [
            ((72, 500, 100, 80), None, "shared"),
            ((300, 300, 50, 40), None, "shared")])
        self.drafting(["A seal of the university."])
        res = M.draft_tree_alt(path, self.ctx(), self.said.append)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual((res["drafted"], res["shared"]), (2, 1))

    def test_the_per_file_limit_is_counted_and_said(self):
        path = _figures_pdf(self.p("t.pdf"),
                            [(r, None, "image") for r in self.SIZES[:3]])
        self.drafting([])
        ctx = self.ctx(budget=2)
        res = M.draft_tree_alt(path, ctx, self.said.append)
        self.assertEqual((len(self.asked), res["drafted"],
                          res["over_budget"], ctx["over_budget"]),
                         (2, 2, 1, 1))
        row, manual, review = {}, [], []
        M.apply_ai_results(row, ctx, manual, review)
        self.assertIn("1 alt-text draft(s) not requested — this run "
                      "drafts at most 2 per file (Alt-text drafts per "
                      "file); process the output again to draft the rest",
                      manual)
        # The output, processed again, asks only about what is left.
        self.drafting([])
        res = M.draft_tree_alt(path, self.ctx(), self.said.append)
        self.assertEqual((res["wanted"], len(self.asked)), (1, 1))

    def test_a_page_drawn_in_slices_is_not_sent(self):
        strips = [((72, 600 - 22 * i, 468, 20), "image %d" % i, "image")
                  for i in range(4)]
        path = _figures_pdf(self.p("t.pdf"), strips)
        self.drafting([])
        res = M.draft_tree_alt(path, self.ctx(), self.said.append)
        self.assertEqual((res["strips"], res["strip_pages"],
                          len(self.asked)), (4, 1, 0))
        manual, review = [], []
        M.tree_alt_steps(res, manual, review)
        self.assertTrue(any(m.startswith("4 figure(s) on 1 page(s) are "
                                         "page-wide strips") for m in manual))
        # Two are not slices: they are asked about like any figure.
        path = _figures_pdf(self.p("u.pdf"), [
            ((72, 600, 468, 20), None, "image"),
            ((72, 560, 468, 24), None, "image")])
        self.drafting([])
        res = M.draft_tree_alt(path, self.ctx(), self.said.append)
        self.assertEqual((res["strips"], len(self.asked)), (0, 2))

    def _artifact_checks(self, path, before_figs):
        s = self.walk(path)
        self.assertEqual(s["figures"], before_figs - 1)
        self.assertEqual((s["untagged_pages"], s["orphans"]), (0, 0))
        self.assertEqual(M.tag_verdict(s)[0], "Yes")
        with _pikepdf.open(path) as pdf:
            ops = [str(i.operator) for i in
                   _pikepdf.parse_content_stream(pdf.pages[0])
                   if not isinstance(i, _pikepdf.ContentStreamInlineImage)]
            tree = pdf.Root.StructTreeRoot
            entry = M._parent_tree_entry(tree, 0)
            self.assertIsNone(entry[1])
        self.assertEqual(ops.count("BMC"), 1)

    def test_a_line_becomes_an_artifact_without_asking(self):
        for kind in ("image", "inline"):
            with self.subTest(kind=kind):
                path = _figures_pdf(self.p(kind + ".pdf"), [
                    ((300, 420, 49, 0.6), "image 1", kind),
                    (self.SIZES[0], None, "image")])
                self.drafting(["A seal of the university."])
                res = M.draft_tree_alt(path, self.ctx(), self.said.append)
                self.assertEqual((res["lines"], res["artifacts"],
                                  res["drafted"], len(self.asked)),
                                 (1, 1, 1, 1))
                self._artifact_checks(path, 2)

    def test_a_decorative_reply_becomes_an_artifact(self):
        for kids_tree in (False, True):
            with self.subTest(kids_tree=kids_tree):
                path = _figures_pdf(self.p("d%d.pdf" % kids_tree), [
                    (self.SIZES[0], None, "image"),
                    (self.SIZES[1], None, "image")], kids_tree=kids_tree)
                self.drafting(["", "A campus map."])
                res = M.draft_tree_alt(path, self.ctx(), self.said.append)
                self.assertEqual((res["decorative"], res["artifacts"],
                                  res["drafted"]), (1, 1, 1))
                self._artifact_checks(path, 2)
                manual, review = [], []
                M.tree_alt_steps(res, manual, review)
                self.assertIn("1 figure(s) were made artifacts (decoration) "
                              "— 1 the AI judged decorative, 0 that are "
                              "only lines; confirm each", review)

    def test_nothing_is_saved_when_the_check_after_writing_finds_worse(self):
        path = _figures_pdf(self.p("t.pdf"),
                            [(self.SIZES[0], None, "image")])
        before = Path(path).read_bytes()
        self.drafting(["A campus map."])
        real = M._tree_no_worse
        M._tree_no_worse = lambda *a: "untagged pages 0 → 1"
        try:
            res = M.draft_tree_alt(path, self.ctx(), self.said.append)
        finally:
            M._tree_no_worse = real
        self.assertEqual((res["drafted"], res["rolled_back"]),
                         (0, "untagged pages 0 → 1"))
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertFalse(os.path.exists(path + ".alt.tmp"))
        manual, review = [], []
        M.tree_alt_steps(res, manual, review)
        self.assertEqual(manual, ["Alt text drafted into the tags was not "
                                  "saved — the check after writing "
                                  "found untagged pages 0 → 1"])

    def test_the_check_after_writing_is_real(self):
        """The real _tree_no_worse, on a real walk: a Figure made an
        artifact on purpose is not "worse"; one more is."""
        before = {"elements": 5, "figures_alt": 1, "untagged_pages": 0}
        self.assertEqual(M._tree_no_worse(before, {"elements": 4,
                         "figures_alt": 1, "untagged_pages": 0}, 0, 1), "")
        self.assertIn("elements 5 → 3", M._tree_no_worse(before, {
            "elements": 3, "figures_alt": 1, "untagged_pages": 0}, 0, 1))
        self.assertIn("untagged pages 0 → 1", M._tree_no_worse(before, {
            "elements": 5, "figures_alt": 2, "untagged_pages": 1}, 1))

    def test_content_marked_inside_a_form_is_not_located(self):
        path = _figures_pdf(self.p("t.pdf"),
                            [(self.SIZES[0], "image 1", "stm")])
        self.drafting([])
        res = M.draft_tree_alt(path, self.ctx(), self.said.append)
        self.assertEqual((res["wanted"], res["unlocated"], len(self.asked)),
                         (1, 1, 0))

    def test_a_stop_between_requests_leaves_the_file_as_it_was(self):
        path = _figures_pdf(self.p("t.pdf"),
                            [(r, None, "image") for r in self.SIZES[:2]])
        before = Path(path).read_bytes()
        self.drafting([])
        checks = []

        def check():
            checks.append(1)
            if len(checks) == 2:
                raise M.StopRequested()
        with self.assertRaises(M.StopRequested):
            M.draft_tree_alt(path, self.ctx(), self.said.append, check)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(Path(path).read_bytes(), before)
        self.assertFalse(os.path.exists(path + ".alt.tmp"))

    def test_a_failed_request_leaves_the_other_figures(self):
        path = _figures_pdf(self.p("t.pdf"),
                            [(r, None, "image") for r in self.SIZES[:2]])
        self.drafting([M.AIError("connection reset"), "A campus map."])
        ctx = self.ctx()
        res = M.draft_tree_alt(path, ctx, self.said.append)
        self.assertEqual((res["failed"], res["drafted"], ctx["alt_failed"]),
                         (1, 1, 1))
        self.assertEqual(self.walk(path)["figures_alt"], 1)


@needs_pdf
class TreeAltProcess(_Drafts, _Quiet):
    """The process mode drafts into an existing tree; the gate holds it
    back; Evaluate sends nothing."""

    def fixture(self):
        return _figures_pdf(self.p("t.pdf"), [
            ((72, 500, 100, 80), "image 1", "image"),
            ((300, 500, 120, 60), None, "image")])

    def test_process_drafts_into_the_existing_tree(self):
        self.drafting(["Enrollment by year.", "A campus map."])
        row = self.one(self.fixture(), "auto", ep("gemini"), ai_alt=True)
        self.assertEqual(len(self.asked), 2)
        self.assertEqual((row["tagged"], row["alt_drafted"],
                          row["alt_missing"]), ("Yes", 2, 0))
        self.assertIn(M.drafted_step(2, 1), row["next"])
        self.assertTrue(any("Alt text into the existing tags: 2 of 2"
                            in m for m in self.said))

    def test_the_gate_holds_it_back(self):
        self.drafting([])
        path = self.fixture()
        run = os.path.join(self.tmp, "run")
        os.makedirs(run, exist_ok=True)
        entry = M._entry(path, "t.pdf", {"record": True,
                                         "visibility": "hidden",
                                         "access": "open"})
        row = M.process_one(entry, 1, _cfg("auto", run, ai_alt=True),
                            ep("gemini"), run)
        self.assertEqual(self.asked, [])
        self.assertEqual(row["gate_held"], ["alt text"])
        self.assertIn(M.GATE_ALT_STEP.format("hidden"), row["next"])

    def test_evaluate_sends_nothing(self):
        self.drafting([])
        row = self.one(self.fixture(), "evaluate", ep("gemini"), ai_alt=True)
        self.assertEqual(self.asked, [])
        self.assertEqual(row["alt_missing"], 2)

    def test_the_run_s_limit_reaches_the_tree(self):
        self.drafting([])
        row = self.one(self.fixture(), "auto", ep("gemini"), ai_alt=True,
                       alt_limit=1)
        self.assertEqual((len(self.asked), row["alt_drafted"],
                          row["alt_missing"]), (1, 1, 1))
        self.assertIn("this run drafts at most 1 per file", row["next"])


class AltLimitField(unittest.TestCase):
    """Alt-text drafts per file: one function validates it, the start
    endpoint calls it, the page sends and keeps it. ValidateStart's
    fixture, without running its tests a second time."""

    setUp = ValidateStart.setUp
    req = ValidateStart.req

    def test_the_field_is_parsed_and_named(self):
        self.assertEqual(M.parse_alt_limit(None), (25, ""))
        self.assertEqual(M.parse_alt_limit(" 40 "), (40, ""))
        for bad in ("", "0", "501", "x"):
            limit, err = M.parse_alt_limit(bad)
            self.assertIsNone(limit, bad)
            self.assertIn("Alt-text drafts per file", err)

    def test_the_start_endpoint_uses_it(self):
        msg, cfg, _ep = M.validate_start(self.req(alt_limit="40"),
                                         self.model, self.tools, self.ai)
        self.assertEqual((msg, cfg["alt_limit"]), ("", 40))
        msg, cfg, _ep = M.validate_start(self.req(alt_limit="0"),
                                         self.model, self.tools, self.ai)
        self.assertIn("Alt-text drafts per file", msg)
        self.assertIsNone(cfg)
        self.assertEqual(M.new_ai_ctx({"alt_limit": 40}, ep("gemini"))
                         ["budget"], [40])

    def test_the_page_sends_and_keeps_it(self):
        """SOURCE check, said so (the start body is built in a click
        handler)."""
        page = M.build_page({"branding": {}}).decode("utf-8")
        for token in ('id="aialtmax"', "alt_limit:aialtmax.value.trim()",
                      "aialtmax.disabled=!has;"):
            self.assertIn(token, page)
        self.assertIn("aialtmax", M.FORM_FIELDS)


@needs_pdf
class LimitInEveryPath(_Drafts, _Quiet):
    """The per-file limit is counted wherever a draft is not asked for
    because of it — Auto-tag's drafts and Word's as well as the tree's."""

    def test_auto_tag_counts_what_the_limit_held_back(self):
        path = _plain_pdf(self.p("u.pdf"))
        ctx = self.ctx(budget=0)
        alt_fn, _n = M._pdf_alt_fn(path, ctx, self.said.append)
        self.assertIsNone(alt_fn(0, "/Im0"))
        self.assertEqual(ctx["over_budget"], 1)

    @needs_office
    def test_word_counts_what_the_limit_held_back(self):
        import docx
        d = docx.Document()
        d.add_picture(io.BytesIO(_png()))
        path = self.p("d.docx")
        d.save(path)
        ctx = self.ctx(budget=0)
        res = M.process_docx(path, self.p("o.docx"), "", ctx, lambda m: None)
        self.assertEqual((res["alt_missing"], ctx["over_budget"]), (1, 1))


def _ast_named(path, name):
    import ast
    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in tree.body:
        if getattr(node, "name", None) == name:
            return ast.dump(node)
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == name for t in node.targets):
            return ast.dump(node)
    return None


class VeraPDF(unittest.TestCase):
    """The adapter, against a real veraPDF 1.30.3 PDF/UA-1 report captured
    on a word-processor fixture (its path reduced to the file name and its
    timestamps zeroed; the parser reads neither)."""

    def test_the_real_report_is_read(self):
        r = M.parse_verapdf_json(REAL_VERAPDF_UA1)
        self.assertEqual((r["version"], r["compliant"], r["failed_rules"]),
                         ("1.30.3", False, 2))
        self.assertEqual([x[0] for x in r["rules"]], ["5-1", "7.3-1"])

    def test_a_real_rule_failure_is_manual_work(self):
        row, manual, review = {}, [], []
        M.apply_verapdf(row, M.parse_verapdf_json(REAL_VERAPDF_UA1),
                        manual, review)
        self.assertEqual(row["checked_by"],
                         "veraPDF 1.30.3 PDF/UA-1 + structure walk")
        self.assertEqual(row["rule_failures"], "5-1 ×1; 7.3-1 ×1")
        self.assertEqual(len(manual), 1)
        self.assertIn("1 rule(s) failed", manual[0])

    def test_only_the_identifier_missing_is_review_not_manual(self):
        """Derived from the real report by removing its 7.3-1 summary, so
        the shape is veraPDF's own."""
        data = json.loads(REAL_VERAPDF_UA1)
        det = data["report"]["jobs"][0]["validationResult"][0]["details"]
        det["ruleSummaries"] = [r for r in det["ruleSummaries"]
                                if r["clause"] == "5"]
        det["failedRules"] = 1
        row, manual, review = {}, [], []
        M.apply_verapdf(row, M.parse_verapdf_json(json.dumps(data)),
                        manual, review)
        self.assertEqual(manual, [])
        self.assertIn("only the PDF/UA identifier is missing", review[0])

    def _run(self, code, out=b"", err=b"", launcher="/x/verapdf",
             path="/tmp/a.pdf"):
        seen = []
        real = M.subprocess.run

        def fake(args, **kw):
            seen.append(args)
            return types.SimpleNamespace(returncode=code, stdout=out,
                                         stderr=err)
        M.subprocess.run = fake
        try:
            return M.verapdf_check(path, launcher), seen
        finally:
            M.subprocess.run = real

    def test_verapdf_is_run_as_documented(self):
        result, seen = self._run(1, REAL_VERAPDF_UA1.encode("utf-8"))
        self.assertEqual(seen, [["/x/verapdf", "-f", "ua1", "--format",
                                 "json", "/tmp/a.pdf"]])
        self.assertEqual(result["error"], "")

    def test_an_unexpected_exit_is_reported_as_itself(self):
        result, _seen = self._run(7, err=b"warning\nboom")
        self.assertEqual(result, {"error": "exit 7: boom"})
        result, _seen = self._run(1, b"not json")
        self.assertEqual(result["error"],
                         "its report could not be read (JSONDecodeError)")

    def test_a_batch_launcher_never_gets_a_dangerous_path(self):
        result, seen = self._run(0, launcher="C:/v/verapdf.bat",
                                 path="C:/a&b.pdf")
        self.assertEqual(seen, [])
        self.assertIn("not run", result["error"])

    def test_the_launcher_is_found_from_the_environment(self):
        tmp = tempfile.mkdtemp()
        exe = os.path.join(tmp, "verapdf")
        Path(exe).write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(exe, 0o755)
        old = os.environ.get("DC_VERAPDF")
        os.environ["DC_VERAPDF"] = exe
        try:
            self.assertEqual(M.find_verapdf(), exe)
        finally:
            if old is None:
                os.environ.pop("DC_VERAPDF", None)
            else:
                os.environ["DC_VERAPDF"] = old

    def test_the_summary_lists_each_failed_rule(self):
        r = M.parse_verapdf_json(REAL_VERAPDF_UA1)
        pairs = dict(M._verapdf_summary([
            {"format": "PDF", "checked_by": "x", "verapdf": r}]))
        self.assertIn("1 of 1 PDF(s) checked", pairs["veraPDF (PDF/UA-1)"])
        self.assertTrue(pairs["  PDF/UA-1 rule 7.3-1"].startswith(
            "1 file(s) — Figure tags shall include"))

    @needs_pdf
    def test_the_row_names_what_checked_it(self):
        tmp = tempfile.mkdtemp()
        path = _tagged_pdf(os.path.join(tmp, "t.pdf"))
        real = (M.verapdf_check, M.log)
        M.verapdf_check = lambda p, launcher: M.parse_verapdf_json(
            REAL_VERAPDF_UA1)
        M.log = lambda *a: None
        try:
            row = M.process_one(M._entry(path, "t.pdf"), 1,
                                _cfg("evaluate", tmp, verapdf="/x/verapdf"),
                                None, tmp)
        finally:
            M.verapdf_check, M.log = real
        self.assertEqual(row["checked_by"],
                         "veraPDF 1.30.3 PDF/UA-1 + structure walk")
        self.assertEqual(row["rule_failures"], "5-1 ×1; 7.3-1 ×1")


class PageControls(unittest.TestCase):

    def test_the_page_sends_and_keeps_the_new_settings(self):
        """SOURCE check, said so (the start body is built in a click
        handler): both controls exist, are sent, and are remembered."""
        page = M.build_page({"branding": {}}).decode("utf-8")
        for token in ('id="aioverride"', 'id="vpdf"',
                      "ai_override:$('aioverride').checked",
                      "verapdf:$('vpdf').checked",
                      "$('aioverride').disabled=!has;"):
            self.assertIn(token, page)
        self.assertIn("aioverride", M.FORM_FIELDS)
        self.assertIn("vpdf", M.FORM_FIELDS)

# ---------------------------------------------------------------------------
# The detectors (headings and tables): structure only, nothing changed.
# ---------------------------------------------------------------------------
def _detect_pdf(path, pages, rolemap=None):
    """A tagged file built item by item, one list per page, every element a
    child of Document in the order given (so two Table items in a row are
    adjacent siblings): (role, size) is text shown at that size; ("Scaled",
    role, size, k) the same through a cm of k; ("Tm", role, size, k) the
    same through a text matrix of k; ("Table", ["HD", "DD"]) a table, one
    string per row, H a TH cell and D a TD; ("Img", role[, alt]) an image
    drawn in that element."""
    P = _pikepdf
    pdf = P.new()
    font = _font(pdf)
    img = _image(pdf)
    tree = pdf.make_indirect(P.Dictionary(Type=P.Name.StructTreeRoot))
    if rolemap:
        tree.RoleMap = P.Dictionary({"/" + k: P.Name("/" + v)
                                     for k, v in rolemap.items()})
    doc = pdf.make_indirect(P.Dictionary(Type=P.Name.StructElem,
                                         S=P.Name.Document, P=tree))
    kids = []

    def elem(role, pg, k, parent=doc):
        return pdf.make_indirect(P.Dictionary(
            Type=P.Name.StructElem, S=P.Name("/" + role), P=parent, Pg=pg,
            K=k))

    for items in pages:
        page = pdf.add_blank_page(page_size=(612, 792))
        page.Resources = P.Dictionary(Font=P.Dictionary(F1=font),
                                      XObject=P.Dictionary(Im0=img))
        pg = page.obj
        parts, mcid, y = [], 0, 760

        def shown(size, mc, before=b"", after=b"", tm=None):
            y_ = 760 - 12 * (mc + 1)
            place = (b"%d 0 0 %d 72 %d Tm" % (tm, tm, y_) if tm else
                     b"72 %d Td" % y_)
            return (b"/Span <</MCID %d>> BDC " % mc + before +
                    b"BT /F1 %d Tf " % size + place + b" (x) Tj ET" + after
                    + b" EMC")
        for it in items:
            kind = it[0]
            if kind == "Table":
                rows = []
                for spec in it[1]:
                    cells = []
                    for c in spec:
                        parts.append(shown(10, mcid))
                        cells.append(elem("TH" if c == "H" else "TD", pg,
                                          mcid))
                        mcid += 1
                    rows.append(pdf.make_indirect(P.Dictionary(
                        Type=P.Name.StructElem, S=P.Name.TR,
                        K=P.Array(cells))))
                kids.append(pdf.make_indirect(P.Dictionary(
                    Type=P.Name.StructElem, S=P.Name.Table, P=doc,
                    K=P.Array(rows))))
            elif kind == "Img":
                parts.append(b"/Span <</MCID %d>> BDC q 50 0 0 50 72 300 cm "
                             b"/Im0 Do Q EMC" % mcid)
                el = elem(it[1], pg, mcid)
                if len(it) > 2:
                    el.Alt = P.String(it[2])
                kids.append(el)
                mcid += 1
            elif kind == "Scaled":
                k = it[3]
                parts.append(shown(it[2], mcid, b"q %d 0 0 %d 0 0 cm " % (k, k),
                                   b" Q"))
                kids.append(elem(it[1], pg, mcid))
                mcid += 1
            elif kind == "Tm":
                parts.append(shown(it[2], mcid, tm=it[3]))
                kids.append(elem(it[1], pg, mcid))
                mcid += 1
            else:
                parts.append(shown(it[1], mcid))
                kids.append(elem(it[0], pg, mcid))
                mcid += 1
        page.Contents = pdf.make_stream(b"\n".join(parts) or b" ")
    doc.K = P.Array(kids)
    tree.K = doc
    pdf.Root.StructTreeRoot = tree
    pdf.Root.MarkInfo = P.Dictionary(Marked=True)
    pdf.Root.Lang = P.String("en-US")
    pdf.docinfo["/Title"] = "Fixture"
    pdf.save(path)
    return path


BODY = [("P", 12), ("P", 12)]


@needs_pikepdf
class Detectors(_Quiet):

    def found(self, pages, **kw):
        path = _detect_pdf(self.p("d{}.pdf".format(len(os.listdir(
            self.tmp)))), pages, **kw)
        with _pikepdf.open(path) as pdf:
            return M.structure_findings(pdf)

    def ids(self, found):
        return [f["detector"] for f in found]

    def test_no_headings_in_more_than_one_page(self):
        self.assertEqual(self.ids(self.found([BODY, BODY])),
                         ["no-headings"])
        self.assertEqual(self.ids(self.found([BODY])), [])

    def test_heuristic_tags_are_not_said_to_have_no_headings(self):
        path = _detect_pdf(self.p("h.pdf"), [BODY, BODY])
        with _pikepdf.open(path) as pdf:
            self.assertEqual(M.structure_findings(pdf, heuristic=True), [])

    def test_few_headings_is_more_than_twenty_pages_a_heading(self):
        many = [[("H1", 12)] + BODY] + [BODY] * 20      # 21 pages, 1 heading
        self.assertIn("few-headings", self.ids(self.found(many)))
        self.assertNotIn("few-headings", self.ids(self.found(many[:20])))

    def test_first_heading_below_h1_names_its_page_and_element(self):
        found = self.found([BODY, [("H2", 14)] + BODY])
        hit = [f for f in found if f["detector"] == "first-heading-not-h1"]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["page"], 2)
        self.assertEqual(hit[0]["element"], "#3 H2")   # Document is #0

    def test_a_skipped_level_and_one_that_is_not(self):
        self.assertIn("skipped-level", self.ids(self.found(
            [[("H1", 14)] + BODY + [("H3", 12)] + BODY])))
        for heads in (("H1", "H2", "H3"), ("H1", "H2", "H3", "H1")):
            page = []
            for h in heads:
                page += [(h, 14)] + BODY
            self.assertNotIn("skipped-level", self.ids(self.found([page])))

    def test_too_many_headings(self):
        flood = [[("H1", 12)] * 20 + [("P", 12)] * 10]
        self.assertIn("too-many-headings", self.ids(self.found(flood)))
        fine = [[("H1", 12)] * 20 + [("P", 12)] * 100]
        self.assertNotIn("too-many-headings", self.ids(self.found(fine)))

    def test_a_heading_on_a_page_of_its_own(self):
        found = self.found([[("H1", 14)] + BODY, [("H1", 14)],
                            [("H2", 14)] + BODY])
        hit = [f for f in found if f["detector"] == "heading-on-own-page"]
        self.assertEqual([f["page"] for f in hit], [2])
        self.assertIn("check the level and the parent heading",
                      hit[0]["observed"])

    def test_the_largest_text_on_the_first_page(self):
        self.assertIn("largest-text-not-heading", self.ids(self.found(
            [[("P", 20)] + BODY + BODY])))
        self.assertNotIn("largest-text-not-heading", self.ids(self.found(
            [[("H1", 20)] + BODY + BODY])))
        # a title set at body size is not seen by size — said in the code
        self.assertNotIn("largest-text-not-heading", self.ids(self.found(
            [[("P", 12)] + BODY + BODY])))

    def test_text_size_follows_the_cm_and_the_text_matrix(self):
        for kind in ("Scaled", "Tm"):
            found = self.found([[(kind, "P", 10, 2)] + BODY + BODY])
            hit = [f for f in found
                   if f["detector"] == "largest-text-not-heading"]
            self.assertEqual(len(hit), 1, kind)
            self.assertIn("(20 pt;", hit[0]["observed"], kind)

    def test_the_rolemap_is_resolved(self):
        found = self.found([[("Heading3", 14)] + BODY],
                           rolemap={"Heading3": "H3"})
        self.assertEqual(self.ids(found), ["first-heading-not-h1"])
        self.assertEqual(found[0]["element"], "#1 Heading3 (H3)")

    def test_tables_one_by_one(self):
        sep = ("P", 12)
        found = self.found([[("H1", 14), ("Table", ["DD", "DD"]), sep,
                             ("Table", ["HH"]), sep,
                             ("Table", ["HH", "DD"])]])
        self.assertEqual(sorted(self.ids(found)), sorted([
            "table-no-header-cells", "table-header-cells-only",
            "one-row-table"]))

    def test_adjacent_tables_are_one_split_table(self):
        found = self.found([[("H1", 14), ("Table", ["HH"]),
                             ("Table", ["DD"]), ("Table", ["DD", "DD"]),
                             ("P", 12)]])
        self.assertEqual(self.ids(found), ["split-table"])
        self.assertIn("3 adjacent tagged tables, page 1: 4 row(s) (2 of "
                      "one row), 2 TH, 6 TD", found[0]["observed"])

    def test_one_row_tables_on_one_page(self):
        sep = ("P", 12)
        page = [("H1", 14)]
        for _ in range(3):
            page += [("Table", ["HD"]), sep]
        found = self.found([page])
        self.assertEqual(self.ids(found).count("one-row-table"), 3)
        self.assertEqual(self.ids(found).count("one-row-tables-on-page"), 1)
        page = [("H1", 14), ("Table", ["HD"]), sep, ("Table", ["HD"]), sep]
        self.assertNotIn("one-row-tables-on-page",
                         self.ids(self.found([page])))

    def test_an_image_drawn_in_text(self):
        base = [("H1", 14)] + BODY
        hit = self.found([base + [("Img", "P")]])
        self.assertEqual(self.ids(hit), ["image-in-text"])
        self.assertEqual(hit[0]["page"], 1)
        for clean in (("Img", "Figure"), ("Img", "P", "A chart of sales")):
            self.assertEqual(self.ids(self.found([base + [clean]])), [])

    def test_findings_are_in_detector_order(self):
        found = self.found([[("Img", "P"), ("H3", 12), ("Table", ["DD"]),
                             ("P", 12), ("Table", ["DD"]), ("P", 12)]]
                           + [BODY])
        order = [d for d, _w in M.DETECTORS]
        got = [order.index(d) for d in self.ids(found)]
        self.assertEqual(got, sorted(got))

    def test_every_detector_the_code_names_is_described(self):
        """Count the group: every string passed to add() in
        structure_findings() is a DETECTORS id, and every id is passed."""
        import ast
        with open(M.__file__, encoding="utf-8") as fh:
            src = fh.read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "structure_findings")
        named = {c.args[0].value for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and getattr(c.func, "id", "")
                 == "add" and c.args and isinstance(c.args[0], ast.Constant)}
        self.assertEqual(named, {d for d, _w in M.DETECTORS})

    def test_findings_word(self):
        found = [{"detector": "split-table"}, {"detector": "no-headings"},
                 {"detector": "split-table"}]
        self.assertEqual(M.findings_word(found),
                         "no-headings ×1; split-table ×2")


@needs_pdf
class DetectorsInTheRun(_Quiet):

    def test_evaluate_reports_the_findings(self):
        row = self.one(_detect_pdf(self.p("e.pdf"), [BODY, BODY]))
        self.assertEqual(row["detectors"], "no-headings ×1")
        self.assertEqual(len(row["findings"]), 1)
        self.assertIn("1 detector finding(s) on headings and tables "
                      "(no-headings ×1) — see the Detectors sheet",
                      row["next"])

    def test_process_reads_them_from_the_output(self):
        """The pair: the processing path, not only Evaluate."""
        row = self.one(_detect_pdf(self.p("q.pdf"), [BODY, BODY]), "auto")
        self.assertEqual(row["status"], "Processed")
        self.assertEqual(row["detectors"], "no-headings ×1")
        real = M.structure_findings
        M.structure_findings = lambda pdf, heuristic=False: []
        try:
            row = self.one(_detect_pdf(self.p("r.pdf"), [BODY, BODY]),
                           "auto")
        finally:
            M.structure_findings = real
        self.assertEqual(row["detectors"], "")

    def test_an_untagged_pdf_is_not_read(self):
        row = self.one(_plain_pdf(self.p("u.pdf")))
        self.assertIsNone(row["findings"])
        self.assertNotIn("detector", row["next"])

    def test_heuristic_tags_add_no_no_headings_finding(self):
        row = self.one(_plain_pdf(self.p("v.pdf")), "auto")
        self.assertEqual(row["tagged"], "Yes (heuristic)")
        self.assertNotIn("no-headings", row["detectors"])

    def test_a_detector_that_fails_costs_the_findings_not_the_row(self):
        real = M.structure_findings

        def broken(pdf, heuristic=False):
            raise ValueError("planted")
        M.structure_findings = broken
        try:
            row = self.one(_detect_pdf(self.p("b.pdf"), [BODY, BODY]))
        finally:
            M.structure_findings = real
        self.assertEqual(row["status"], "Evaluated")
        self.assertIn("could not read the structure tree (ValueError: "
                      "planted)", row["next"])
        self.assertEqual(row["findings"], [])

    def _workbook(self, paths):
        import openpyxl
        out = os.path.join(self.tmp, "out")
        os.makedirs(out)
        cfg = _cfg("evaluate", out, source="test",
                   entries=[M._entry(p, os.path.basename(p)) for p in paths])
        M.process_worker({"branding": {}}, cfg, None)
        found = [f for f in os.listdir(out) if f.startswith("DC_OCR_Report")]
        self.assertEqual(len(found), 1, found)
        return openpyxl.load_workbook(os.path.join(out, found[0]))

    def test_the_workbook_has_a_detectors_sheet(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        book = self._workbook([
            _detect_pdf(self.p("a.pdf"), [BODY, BODY]),
            _detect_pdf(self.p("b.pdf"), [[("H1", 14), ("Table", ["DD"]),
                                           ("Table", ["DD"])]])])
        sheet = book["Detectors"]
        self.assertEqual([c.value for c in sheet[1]], M.DETECTOR_HEADERS)
        rows = [[c.value for c in r] for r in sheet.iter_rows(min_row=2)]
        self.assertEqual([r[4] for r in rows],
                         ["no-headings", "split-table"])
        self.assertEqual(rows[1][2], 1)
        self.assertEqual(rows[1][3], "#2 Table")
        files = book["Files"]
        col = M.REPORT_HEADERS.index("Detector Findings") + 1
        self.assertEqual(files.cell(row=2, column=col).value,
                         "no-headings ×1")
        summary = {r[0].value: r[1].value for r in book["Summary"].iter_rows()}
        self.assertIn("2 finding(s) in 2 of 2 tagged PDF(s) read",
                      summary["Detectors (headings and tables)"])
        self.assertIn("1 in 1 file(s)", summary["  split-table"])
        self.assertEqual(summary["  Page and Element"], M.DETECTOR_NOTE)

    def test_no_finding_is_said_and_no_tagged_pdf_means_no_sheet(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        book = self._workbook([_detect_pdf(self.p("c.pdf"),
                                           [[("H1", 14)] + BODY])])
        rows = [[c.value for c in r]
                for r in book["Detectors"].iter_rows(min_row=2)]
        self.assertEqual(rows[0][4], "(none)")
        summary = {r[0].value: r[1].value for r in book["Summary"].iter_rows()}
        self.assertEqual(summary["  Detector findings"], "none")
        import shutil
        shutil.rmtree(os.path.join(self.tmp, "out"))
        book = self._workbook([_plain_pdf(self.p("p.pdf"))])
        self.assertNotIn("Detectors", book.sheetnames)

if __name__ == "__main__":
    # Say which version is under test, read from the module rather than from
    # this file's name. Nothing here hard-codes a version.
    print("OCR & Accessibility Toolkit v{} — verification".format(M.MANIFEST["version"]))
    unittest.main(verbosity=2)
