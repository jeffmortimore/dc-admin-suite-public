#!/usr/bin/env python3
"""Tests for the Page Number Generator (modules/dc_page_numbers.py) and the
DOI & XML Generator's share of DC-ISSUE.

    python3 modules/_verify_page_numbers.py

Off the network: the OAI feed is a fixture shaped like the real
document-export answer (records out of label order, no first or last page
unless a test adds them), PDFs are made here with pikepdf, and the Batch
File Downloader's reports are written with its own column names. The
downloader itself is imported to read the plan this module writes, so the
two cannot drift apart unnoticed.

Exit status 0 when every test passes. A test that cannot run (no pikepdf,
no node) is reported as skipped, by name, never as a pass.
"""
import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT))

import dc_page_numbers as M            # noqa: E402
import dc_doi_xml as DOI               # noqa: E402
# DC-FORM: a test must never write the operator's remembered form.
M.FORM_PATH = os.path.join(tempfile.mkdtemp(prefix="dcpn-form-"), "f.json")
DOI.FORM_PATH = os.path.join(tempfile.mkdtemp(prefix="dcdoi-form-"), "f.json")

try:
    import pikepdf
except ImportError:
    pikepdf = None
try:
    import openpyxl
except ImportError:
    openpyxl = None
try:
    import dc_file_downloader as D     # noqa: E402
except Exception as _e:  # the downloader needs only the standard library to import
    D = None
    D_WHY = "{}: {}".format(_e.__class__.__name__, _e)

BASE = "https://repository.example.edu"
CTX = "sample-journal"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
def record(article, label, vol="19", iss="1", doctype="research",
           title=None, pdf=True, fpage="", lpage="", ctx=CTX):
    """One <record> as the feed sends it, URLs double-escaped as it does."""
    title = title or "Article {}".format(article)
    pages = ("<fpage>{}</fpage><lpage>{}</lpage>".format(fpage, lpage)
             if fpage or lpage else "")
    full = ("<fulltext-url>{0}/cgi/viewcontent.cgi?article={1}&amp;amp;"
            "context={2}&amp;amp;unstamped=1</fulltext-url>".format(
                BASE, article, ctx) if pdf else "")
    return (
        "<record><header><identifier>oai:example:{ctx}-{a}</identifier>"
        "<setSpec>publication:{ctx}</setSpec></header><metadata>"
        "<document-export xmlns=\"http://www.bepress.com/document-import\">"
        "<documents><document><title>{t}</title><authors><author>"
        "<lname>Writer</lname><fname>Ann</fname></author></authors>"
        "<coverpage-url>{b}/{ctx}/vol{v}/iss{i}/{lab}</coverpage-url>{full}"
        "<label>{lab}</label><document-type>{dt}</document-type>"
        "<type>article</type><articleid>{a}</articleid>"
        "<native-url>{b}/context/{ctx}/article/{a}/type/native/viewcontent"
        "</native-url>{pages}<submission-path>{ctx}/vol{v}/iss{i}/{lab}"
        "</submission-path></document></documents></document-export>"
        "</metadata></record>").format(
            ctx=ctx, a=article, t=title, b=BASE, v=vol, i=iss, lab=label,
            full=full, dt=doctype, pages=pages)


def feed(records, token=""):
    tok = ("<resumptionToken>{}</resumptionToken>".format(token)
           if token else "")
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/">'
            "<ListRecords>{}{}</ListRecords></OAI-PMH>".format(
                "".join(records), tok))


NO_RECORDS = ('<?xml version="1.0"?><OAI-PMH xmlns="http://www.openarchives'
              '.org/OAI/2.0/"><error code="noRecordsMatch">No matches'
              '</error></OAI-PMH>')


class FakeFeed:
    """Stands in for fetch_url: answers each URL from a list, records asks."""

    def __init__(self, answers):
        self.answers, self.asked = list(answers), []

    def __call__(self, url):
        self.asked.append(url)
        return self.answers.pop(0)


def ref(vol="19", iss="1", journal=CTX, syear=""):
    return {"journal": journal, "volume": vol, "issue": iss, "syear": syear,
            "entry": None}


def make_pdf(path, pages):
    pdf = pikepdf.new()
    for _ in range(pages):
        pdf.add_blank_page(page_size=(612, 792))
    pdf.save(path)
    pdf.close()


def item(article, label, on=True, pdf=True, hand="", fpage="", lpage=""):
    return {"articleid": str(article), "context": CTX, "label": str(label),
            "title": "Article {}".format(article), "authors": "Ann Writer",
            "doctype": "research",
            "fulltext": ("{}/cgi/viewcontent.cgi?article={}&context={}"
                         "&unstamped=1".format(BASE, article, CTX)
                         if pdf else ""),
            "landing": "{}/{}/vol19/iss1/{}".format(BASE, CTX, label),
            "native": True, "fpage": fpage, "lpage": lpage, "on": on,
            "hand": hand}


def issue(items, iss="1", mode="restart", cover=1, first="1", vol="19"):
    return {"key": "{}/vol{}/iss{}".format(CTX, vol, iss), "journal": CTX,
            "volume": vol, "issue": iss, "syear": "", "name": "Sample Journal",
            "mode": mode, "cover": cover, "first": first, "items": items}


def arrangement(*issues):
    return {"listed": "20260101_000000", "issues": list(issues)}


REPORT_HEADERS = None


def downloader_headers():
    """The downloader's REPORT_HEADERS, read with ast (never executed)."""
    src = (HERE / "dc_file_downloader.py").read_text(encoding="utf-8")
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "REPORT_HEADERS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("REPORT_HEADERS not found in the downloader")


def write_dl_report(path, rows):
    """A downloader report: its own columns, rows given as dicts."""
    heads = downloader_headers()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(heads)
    for r in rows:
        ws.append([r.get(h, "") for h in heads])
    wb.save(path)


def dl_row(article, version, status="Downloaded", saved="", kind="primary"):
    url = ("{}/cgi/viewcontent.cgi?article={}&context={}&preview_mode=1"
           .format(BASE, article, CTX) if version == "current" else
           "{}/cgi/viewcontent.cgi?article={}&context={}&unstamped=1"
           .format(BASE, article, CTX))
    return {"Article ID": str(article), "File Kind": kind,
            "Version": version, "Status": status, "Saved Filename": saved,
            "File URL": url}


# ---------------------------------------------------------------------------
# Step 2: the listing
# ---------------------------------------------------------------------------
class Listing(unittest.TestCase):
    def setUp(self):
        self._fetch = M.fetch_url

    def tearDown(self):
        M.fetch_url = self._fetch

    def harvest(self, answers, r=None):
        M.fetch_url = FakeFeed(answers)
        return M.harvest_items(BASE, r or ref(), lambda m: None)

    def test_items_come_in_label_order_from_an_unordered_feed(self):
        items = self.harvest([feed([record(1003, 3), record(1004, 4),
                                    record(1002, 2), record(1001, 1, doctype="editor_letter"),
                                    record(1005, 5)])])
        self.assertEqual([i["label"] for i in items], ["3", "4", "2", "1", "5"])
        groups = M.group_issues(items, ref(), None, lambda m: None)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual([i["label"] for i in g["items"]], ["1", "2", "3", "4", "5"])
        self.assertEqual((g["key"], g["volume"], g["issue"]),
                         ("{}/vol19/iss1".format(CTX), "19", "1"))
        first = g["items"][0]
        self.assertEqual(first["doctype"], "editor_letter")
        self.assertNotIn("amp;", first["fulltext"])
        self.assertEqual(first["context"], CTX)
        self.assertTrue(first["native"])
        self.assertEqual((first["fpage"], first["lpage"]), ("", ""))
        self.assertTrue(all(i["on"] and i["hand"] == "" for i in g["items"]))
        self.assertEqual((g["mode"], g["cover"]), ("restart", 1))

    def test_label_ten_sorts_after_nine(self):
        items = self.harvest([feed([record(1010, 10), record(1009, 9),
                                    record(1011, "A")])])
        g = M.group_issues(items, ref(), None, lambda m: None)[0]
        self.assertEqual([i["label"] for i in g["items"]], ["9", "10", "A"])

    def test_existing_pages_are_read_when_the_feed_has_them(self):
        items = self.harvest([feed([record(1001, 1, fpage="5", lpage="17")])])
        self.assertEqual((items[0]["fpage"], items[0]["lpage"]), ("5", "17"))

    def test_a_volume_listing_is_split_into_its_issues_in_order(self):
        items = self.harvest([feed([record(2001, 1, iss="2"),
                                    record(1001, 1, iss="1"),
                                    record(1002, 2, iss="1")])],
                             ref(iss=""))
        groups = M.group_issues(items, ref(iss=""), {"name": "Sample Journal",
                                                      "page_numbering": "continue",
                                                      "cover_pages": 2},
                                lambda m: None)
        self.assertEqual([g["issue"] for g in groups], ["1", "2"])
        self.assertEqual([len(g["items"]) for g in groups], [2, 1])
        self.assertEqual({(g["mode"], g["cover"]) for g in groups},
                         {("continue", 2)})
        M.default_firsts(groups)
        self.assertEqual([g["first"] for g in groups], ["1", ""])

    def test_restart_issues_each_offer_page_one(self):
        groups = [issue([], iss="1"), issue([], iss="2")]
        for g in groups:
            g["first"] = None
        M.default_firsts(groups)
        self.assertEqual([g["first"] for g in groups], ["1", "1"])

    def test_paging_follows_the_resumption_token(self):
        fake = FakeFeed([feed([record(1001, 1)], token="a b/c"),
                         feed([record(1002, 2)])])
        M.fetch_url = fake
        items = M.harvest_items(BASE, ref(), lambda m: None)
        self.assertEqual(len(items), 2)
        self.assertIn("set=publication:{}/vol19/iss1".format(CTX), fake.asked[0])
        self.assertIn("resumptionToken=a%20b%2Fc", fake.asked[1])

    def test_no_records_is_said_as_such(self):
        with self.assertRaises(M.ListError) as cm:
            self.harvest([NO_RECORDS])
        self.assertIn("no records", str(cm.exception))

    def test_a_record_without_an_article_id_is_left_out_and_said(self):
        items = self.harvest([feed([record(1001, 1), record("", 2)])])
        said = []
        g = M.group_issues(items, ref(), None, said.append)[0]
        self.assertEqual([i["articleid"] for i in g["items"]], ["1001"])
        self.assertEqual(len(said), 1)
        self.assertIn("no article ID", said[0])

    def test_the_list_worker_lists_and_says_so(self):
        M.fetch_url = FakeFeed([feed([record(1002, 2), record(1001, 1)])])
        M.list_worker({"base_url": BASE}, [ref()], [])
        with M.LOCK:
            st = json.loads(json.dumps(M.STATE))
        self.assertEqual(st["phase"], "done")
        self.assertEqual(st["outcome"], "green")
        self.assertEqual(len(st["listing"]["issues"]), 1)
        self.assertEqual(st["listing"]["issues"][0]["first"], "1")
        self.assertIn("Listed 2 item(s) in 1 issue(s)", st["summary"])

    def test_the_list_worker_fails_when_nothing_could_be_listed(self):
        M.fetch_url = FakeFeed([NO_RECORDS])
        M.list_worker({"base_url": BASE}, [ref()], [])
        with M.LOCK:
            self.assertEqual(M.STATE["phase"], "error")
            self.assertIn("Nothing was listed", M.STATE["summary"])


# ---------------------------------------------------------------------------
# The registry, as this module reads it
# ---------------------------------------------------------------------------
class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dcpn-reg-")
        self._path = M.REGISTRY_PATH
        M.REGISTRY_PATH = Path(self.tmp) / "journals.json"

    def tearDown(self):
        M.REGISTRY_PATH = self._path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_a_missing_registry_is_said_and_never_written(self):
        got = M.load_journals()
        self.assertEqual(got["journals"], [])
        self.assertIn("no journal registry", got["note"])
        self.assertFalse(M.REGISTRY_PATH.exists())

    def test_defaults_and_unusable_settings(self):
        M.REGISTRY_PATH.write_text(json.dumps({"journals": [
            {"id": "a-j", "name": "A"},
            {"id": "b-j", "name": "B", "page_numbering": "continue",
             "cover_pages": 2},
            {"id": "c-j", "name": "C", "cover_pages": "two"}]}))
        got = M.load_journals()
        by = {j["id"]: j for j in got["journals"]}
        self.assertEqual((by["a-j"]["page_numbering"], by["a-j"]["cover_pages"]),
                         ("restart", 1))
        self.assertEqual((by["b-j"]["page_numbering"], by["b-j"]["cover_pages"]),
                         ("continue", 2))
        self.assertEqual(by["c-j"]["cover_pages"], 1)
        self.assertIn("c-j", got["note"])


# ---------------------------------------------------------------------------
# The arrangement (one validator for both endpoints)
# ---------------------------------------------------------------------------
class Arrangement(unittest.TestCase):
    def check(self, arr):
        return M.validate_arrangement(json.loads(json.dumps(arr)), BASE)

    def test_a_good_arrangement(self):
        arr, msg = self.check(arrangement(issue([item(1001, 1), item(1002, 2, on=False)])))
        self.assertEqual(msg, "")
        g = arr["issues"][0]
        self.assertEqual((g["first"], g["cover"], g["mode"]), (1, 1, "restart"))
        self.assertEqual([i["on"] for i in g["items"]], [True, False])
        self.assertEqual(g["label"], "Sample Journal, vol. 19, iss. 1")

    def refused(self, arr, words):
        got, msg = self.check(arr)
        self.assertIsNone(got)
        self.assertIn(words, msg)

    def test_a_pdf_link_on_another_host_is_refused(self):
        it = item(1001, 1)
        it["fulltext"] = it["fulltext"].replace("repository.example.edu",
                                                "elsewhere.example.org")
        self.refused(arrangement(issue([it])), "not on repository.example.edu")

    def test_a_link_that_changes_state_is_refused(self):
        it = item(1001, 1)
        it["fulltext"] += "&force=yes"
        self.refused(arrangement(issue([it])), "change something")

    def test_a_link_to_another_article_is_refused(self):
        it = item(1001, 1)
        it["fulltext"] = it["fulltext"].replace("article=1001", "article=1009")
        self.refused(arrangement(issue([it])), "names another article")

    def test_a_record_link_on_another_host_is_refused(self):
        it = item(1001, 1)
        it["landing"] = "https://elsewhere.example.org/x"
        self.refused(arrangement(issue([it])), "record link")

    def test_continue_needs_an_earlier_issue_or_a_first_page(self):
        self.refused(arrangement(issue([item(1001, 1)], mode="continue",
                                       first="")), "enter its first page")
        arr, msg = self.check(arrangement(
            issue([item(1001, 1)], iss="1"),
            issue([item(2001, 1)], iss="2", mode="continue", first="")))
        self.assertEqual(msg, "")
        self.assertIsNone(arr["issues"][1]["first"])
        arr, msg = self.check(arrangement(issue([item(1001, 1)], mode="continue",
                                                first="215")))
        self.assertEqual((msg, arr["issues"][0]["first"]), ("", 215))

    def test_another_volume_is_not_an_earlier_issue(self):
        self.refused(arrangement(
            issue([item(1001, 1)], iss="1", vol="18"),
            issue([item(2001, 1)], iss="1", vol="19", mode="continue",
                  first="")), "enter its first page")

    def test_nothing_ticked_is_refused(self):
        self.refused(arrangement(issue([item(1001, 1, on=False)])),
                     "Tick at least one")

    def test_numbers_must_be_whole(self):
        self.refused(arrangement(issue([item(1001, 1, hand="3.5")])),
                     "pages by hand")
        self.refused(arrangement(issue([item(1001, 1)], first="0")),
                     "first page number")
        self.refused(arrangement(issue([item(1001, 1)], cover=10)),
                     "cover pages")
        it = item(1001, 1)
        it["on"] = "yes"
        self.refused(arrangement(issue([it])), "Paginate is not yes or no")

    def test_nothing_listed(self):
        self.refused(None, "List the contents")
        self.refused({"issues": []}, "List the contents")


# ---------------------------------------------------------------------------
# Step 4: the download plan, and the downloader reading it
# ---------------------------------------------------------------------------
class Plan(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dcpn-plan-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def arr(self):
        arr, msg = M.validate_arrangement(arrangement(issue([
            item(1002, 2), item(1001, 1, on=False), item(1003, 3, pdf=False),
            item(1004, 4)])), BASE)
        self.assertEqual(msg, "")
        return arr

    def test_two_rows_per_ticked_item_in_the_arranged_order(self):
        rows, notes = M.plan_rows(self.arr(), BASE)
        self.assertEqual([(r[0], r[1], r[6]) for r in rows], [
            (1, "1002", "current"), (2, "1002", "current-unstamped"),
            (3, "1004", "current"), (4, "1004", "current-unstamped")])
        self.assertTrue(all(r[11] == "Not attempted" and r[4] == "primary"
                            for r in rows))
        self.assertEqual(rows[1][12], self.arr()["issues"][0]["items"][0]["fulltext"])
        self.assertIn("preview_mode=1", rows[0][12])
        self.assertEqual(len(notes), 1)
        self.assertIn("1003 has no PDF", notes[0])
        self.assertTrue(all(len(r) == len(M.DOWNLOADER_HEADERS) for r in rows))

    def test_the_columns_are_the_downloaders(self):
        self.assertEqual(M.DOWNLOADER_HEADERS, downloader_headers())

    def test_the_plan_is_not_named_as_a_whole_jobs_plan(self):
        name = M.PLAN_NAME.format("20260101_000000")
        self.assertFalse(name.startswith("DC_FileDownload_"))

    @unittest.skipIf(D is None, "the downloader could not be imported")
    def test_the_urls_are_the_downloaders(self):
        self.assertEqual(M.stamped_file_url(BASE, CTX, "1001"),
                         D.primary_file_url(BASE, CTX, "1001"))
        self.assertEqual(M.admin_url(BASE, CTX, "1001"),
                         D.admin_record_url(BASE, CTX, "1001", False))
        self.assertEqual((M.VERSION_STAMPED, M.VERSION_UNSTAMPED,
                          M.DOWNLOADER_NOT_ATTEMPTED),
                         ("current", D.VERSION_CURRENT_UNSTAMPED,
                          D.NOT_ATTEMPTED))

    @unittest.skipIf(D is None or openpyxl is None,
                     "the downloader or openpyxl is not available")
    def test_the_downloader_reads_the_plan_as_planned_files(self):
        arr = self.arr()
        rows, notes = M.plan_rows(arr, BASE)
        path = os.path.join(self.tmp, M.PLAN_NAME.format("20260101_000000"))
        M.write_download_plan(path, rows, notes, arr, "#1F4E79")
        got, skipped, absent, known = D.read_failed_rows(path)
        self.assertEqual((len(got), skipped, absent, known), (4, [], 0, {}))
        self.assertEqual([(r["article"], r["version"], r["context"]) for r in got],
                         [("1002", "current", CTX),
                          ("1002", "current-unstamped", CTX),
                          ("1004", "current", CTX),
                          ("1004", "current-unstamped", CTX)])
        # The downloader calls a session of rows never tried "planned", and
        # says "downloaded", not "recovered".
        self.assertTrue(all(r["was"] in (D.INVENTORIED, D.NOT_ATTEMPTED)
                            for r in got))
        self.assertFalse(any(r["replan"] for r in got))
        # Two identities, so the downloader names the copies apart.
        a = D.build_filename(CTX, "1002", "primary", "public", "current", "",
                             "application/pdf", b"%PDF-1.7")
        b = D.build_filename(CTX, "1002", "primary", "public",
                             "current-unstamped", "", "application/pdf",
                             b"%PDF-1.7")
        self.assertNotEqual(a, b)

    @unittest.skipIf(openpyxl is None, "openpyxl is not installed")
    def test_the_plan_workbook_says_what_it_is(self):
        arr = self.arr()
        rows, notes = M.plan_rows(arr, BASE)
        path = os.path.join(self.tmp, "p.xlsx")
        M.write_download_plan(path, rows, notes, arr, "#1F4E79")
        wb = openpyxl.load_workbook(path)
        try:
            self.assertEqual(wb.sheetnames, ["Inventory", "About"])
            about = {r[0]: r[1] for r in wb["About"].iter_rows(min_row=2, values_only=True)}
            self.assertEqual((about["Items ticked"], about["Files in the plan"]), (3, 4))
            self.assertIn("1003", about["Left out"])
        finally:
            wb.close()


# ---------------------------------------------------------------------------
# Step 5: numbering (pure)
# ---------------------------------------------------------------------------
def w(article, u=None, s=None, on=True, hand=None, u_note="", s_note=""):
    return {"articleid": str(article), "on": on, "unstamped": u, "stamped": s,
            "hand": hand, "u_note": u_note, "s_note": s_note}


class Numbering(unittest.TestCase):
    def test_restart_numbers_from_the_first_page(self):
        res, end, stop = M.number_issue([w(1, 10, 11), w(2, 4, 5)], 1, 1)
        self.assertEqual([(r["first"], r["last"]) for r in res], [(1, 10), (11, 14)])
        self.assertEqual((end, stop), (14, ""))
        self.assertEqual([r["check"] for r in res], ["agrees", "agrees"])

    def test_items_not_ticked_are_passed_over(self):
        res, end, _ = M.number_issue([w(1, 10, 11), w(2, on=False), w(3, 2, 3)], 1, 1)
        self.assertEqual([(r["first"], r["last"]) for r in res],
                         [(1, 10), (None, None), (11, 12)])
        self.assertEqual(res[1]["check"], "not paginated")
        self.assertEqual(end, 12)

    def test_a_disagreement_is_flagged_and_stops_the_numbering(self):
        res, end, stop = M.number_issue([w(1, 10, 11), w(2, 4, 4), w(3, 2, 3)], 1, 1)
        self.assertEqual([r["first"] for r in res], [1, None, None])
        self.assertEqual(res[1]["check"], "disagrees")
        self.assertIn("difference of 0", res[1]["notes"][0])
        self.assertIn("position 2 (article 2)", stop)
        self.assertIn("Not numbered: numbering stopped at position 2", res[2]["notes"][0])
        self.assertIsNone(end)

    def test_a_missing_stamped_copy_is_not_numbered(self):
        res, end, stop = M.number_issue([w(1, 10, None, s_note="the downloader's row says: HTTP 404")], 1, 1)
        self.assertEqual(res[0]["check"], "not checked")
        self.assertIn("HTTP 404", res[0]["notes"][0])
        self.assertIsNone(end)

    def test_pages_by_hand_only_for_an_item_without_a_pdf(self):
        res, end, _ = M.number_issue([w(1, None, None, hand=6, u_note="the feed has no PDF for it"),
                                      w(2, 3, 4, hand=9)], 5, 1)
        self.assertEqual([(r["first"], r["last"]) for r in res], [(5, 10), (11, 13)])
        self.assertTrue(res[0]["check"].startswith("not checked — pages entered by hand"))
        self.assertIn("(9) was not used", res[1]["notes"][-1])
        self.assertEqual(end, 13)

    def test_no_pdf_and_no_count_stops(self):
        res, end, stop = M.number_issue([w(1, None, None, u_note="the feed has no PDF for it"), w(2, 3, 4)], 1, 1)
        self.assertEqual(res[0]["check"], "no PDF")
        self.assertEqual([r["first"] for r in res], [None, None])
        self.assertIsNone(end)

    def test_an_unknown_first_page_numbers_nothing(self):
        res, end, stop = M.number_issue([w(1, 3, 4)], None, 1, "the previous issue did not finish")
        self.assertEqual(res[0]["first"], None)
        self.assertEqual(stop, "the previous issue did not finish")
        self.assertIn("previous issue did not finish", res[0]["notes"][0])

    def test_an_issue_with_nothing_ticked_ends_before_its_first_page(self):
        res, end, stop = M.number_issue([w(1, on=False)], 7, 1)
        self.assertEqual((end, stop), (6, ""))

    def test_two_cover_pages(self):
        res, _, _ = M.number_issue([w(1, 10, 12), w(2, 4, 5)], 1, 2)
        self.assertEqual([r["check"] for r in res], ["agrees", "disagrees"])

    def test_most_common_difference(self):
        self.assertEqual(M.common_difference([(10, 11), (4, 5), (3, 3)]), "1 (2 of 3)")
        self.assertEqual(M.common_difference([(10, 11), (3, 3)]), "tie: 0, 1")
        self.assertEqual(M.common_difference([(None, 3)]), "none measured")

    def test_differs(self):
        self.assertEqual(M.differs("", "", 1, 10), "")
        self.assertEqual(M.differs("1", "10", 1, 10), "no")
        self.assertEqual(M.differs("1", "9", 1, 10), "yes")
        self.assertEqual(M.differs("1", "10", None, None), "")


# ---------------------------------------------------------------------------
# Step 5: counting from the downloader's run folder
# ---------------------------------------------------------------------------
@unittest.skipIf(pikepdf is None or openpyxl is None,
                 "pikepdf or openpyxl is not installed")
class Counting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="dcpn-count-")
        self.run = os.path.join(self.tmp, "DC_FileDownloads_20260101_120000")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(os.path.join(self.run, CTX))
        os.makedirs(self.out)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def pdf(self, name, pages, where=None):
        make_pdf(os.path.join(where or os.path.join(self.run, CTX), name), pages)
        return name

    def test_reports_are_read_by_name_and_the_latest_row_wins(self):
        write_dl_report(os.path.join(self.run, "DC_FileDownload_Report_20260101_120000.xlsx"), [
            dl_row(1001, "current", status="Failed — HTTP 503"),
            dl_row(1001, "current-unstamped", saved="u.pdf"),
            dl_row(1001, "all", saved="x.pdf"),
            dl_row(1001, "current", saved="n.docx", kind="native")])
        write_dl_report(os.path.join(self.run, "DC_FileDownload_Retry_20260101_130000.xlsx"), [
            dl_row(1001, "current", saved="s.pdf")])
        write_dl_report(os.path.join(self.run, "DC_FileDownload_Retry_20260101_140000_PARTIAL.xlsx"), [
            dl_row(1001, "current", status="Not attempted")])
        wb = openpyxl.Workbook()
        wb.active.append(["Order", "Title"])
        wb.save(os.path.join(self.run, "DC_FileDownload_Report_20260101_110000.xlsx"))
        rows, read, problems = M.read_downloader_rows([self.run])
        self.assertEqual(rows[(CTX, "1001", "stamped")]["saved"], "s.pdf")
        self.assertEqual(rows[(CTX, "1001", "unstamped")]["saved"], "u.pdf")
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(read), 2)
        self.assertEqual(len(problems), 1)
        self.assertIn("not a download report", problems[0])

    def test_reports_are_read_from_the_structure_folders_too(self):
        write_dl_report(os.path.join(self.run, CTX, "DC_FileDownload_Retry_{}_20260101_120000.xlsx".format(CTX)), [
            dl_row(1001, "current", saved="s.pdf")])
        os.makedirs(os.path.join(self.run, "_incoming"))
        write_dl_report(os.path.join(self.run, "_incoming", "DC_FileDownload_Retry_x_20260101_130000.xlsx"), [
            dl_row(1001, "current", status="Not attempted")])
        os.makedirs(os.path.join(self.run, CTX, "deeper"))
        write_dl_report(os.path.join(self.run, CTX, "deeper", "DC_FileDownload_Retry_y_20260101_140000.xlsx"), [
            dl_row(1001, "current", status="Not attempted")])
        rows, read, problems = M.read_downloader_rows([self.run])
        self.assertEqual(read, ["DC_FileDownload_Retry_{}_20260101_120000.xlsx".format(CTX)])
        self.assertEqual(rows[(CTX, "1001", "stamped")]["status"], "Downloaded")

    def test_saved_files_are_found_where_the_downloader_puts_them(self):
        self.pdf("a.pdf", 1)
        self.pdf("b.pdf", 1, self.run)
        os.makedirs(os.path.join(self.run, "other"))
        self.pdf("c.pdf", 1, os.path.join(self.run, "other"))
        self.assertTrue(M.find_saved(self.run, CTX, "a.pdf").endswith(os.path.join(CTX, "a.pdf")))
        self.assertTrue(M.find_saved(self.run, CTX, "b.pdf"))
        self.assertTrue(M.find_saved(self.run, CTX, "c.pdf"))
        self.assertEqual(M.find_saved(self.run, CTX, "../c.pdf"), "")
        self.assertEqual(M.find_saved(self.run, CTX, "none.pdf"), "")

    def test_a_file_that_is_not_a_pdf_is_said(self):
        p = os.path.join(self.run, CTX, "x.pdf")
        Path(p).write_bytes(b"not a pdf")
        n, why = M.count_pages(p)
        self.assertIsNone(n)
        self.assertIn("could not be read as a PDF", why)

    def build_run(self):
        """iss1 (restart, cover 1): 1001 ok (10/11), 1002 unticked, 1003 ok
        (5/6); iss2 (continue): 2001 ok (3/4), 2002 disagrees (3/5), 2003
        after it; 2004 has no row in any report (it never ran)."""
        files = {1001: (10, 11), 1003: (5, 6), 2001: (3, 4), 2002: (3, 5),
                 2003: (2, 3)}
        rows = []
        for art, (u, s) in files.items():
            rows.append(dl_row(art, "current-unstamped", saved=self.pdf("{}_u.pdf".format(art), u)))
            rows.append(dl_row(art, "current", saved=self.pdf("{}_s.pdf".format(art), s)))
        # Where the downloader writes it: in the structure's own folder.
        write_dl_report(os.path.join(self.run, CTX, "DC_FileDownload_Retry_{}_20260101_120000.xlsx".format(CTX)), rows)
        arr, msg = M.validate_arrangement(arrangement(
            issue([item(1001, 1), item(1002, 2, on=False), item(1003, 3)],
                  iss="1"),
            issue([item(2001, 1), item(2002, 2), item(2003, 3), item(2004, 4)],
                  iss="2", mode="continue", first="")), BASE)
        self.assertEqual(msg, "")
        return arr

    def read_report(self, path):
        wb = openpyxl.load_workbook(path)
        try:
            return {name: [list(r) for r in wb[name].iter_rows(values_only=True)]
                    for name in wb.sheetnames}
        finally:
            wb.close()

    def test_count_and_number_end_to_end(self):
        arr = self.build_run()
        M.count_worker({"base_url": BASE}, arr, self.run, "", self.out)
        with M.LOCK:
            st = json.loads(json.dumps(M.STATE))
        self.assertEqual(st["phase"], "done", st["summary"])
        self.assertEqual(st["outcome"], "amber")
        self.assertIn("Numbered 3 of 6 ticked item(s) in 2 issue(s)", st["summary"])
        self.assertIn("1 flagged by the cover-page check", st["summary"])
        out = sorted(os.listdir(self.out))
        self.assertEqual([n for n in out if n.endswith(".xlsx")],
                         [os.path.basename(st["link_file"])])
        self.assertFalse(any("_PARTIAL" in n for n in out))
        self.assertTrue(any(n.startswith("DC_PageNumbers_Log_") for n in out))
        rep = self.read_report(st["link_file"])
        self.assertEqual(rep["Pages"][0], M.PAGES_HEADERS)
        pages = {r[3]: r for r in rep["Pages"][1:]}
        col = {h: i for i, h in enumerate(M.PAGES_HEADERS)}
        fl = lambda a: (pages[a][col["First Page"]], pages[a][col["Last Page"]])
        self.assertEqual(fl("1001"), (1, 10))
        self.assertEqual(fl("1002"), (None, None))
        self.assertEqual(pages["1002"][col["Paginated"]], "no")
        self.assertEqual(fl("1003"), (11, 15))
        self.assertEqual(fl("2001"), (16, 18))          # continues from iss1
        self.assertEqual(pages["2002"][col["Cover Check"]], "disagrees")
        self.assertEqual(fl("2002"), (None, None))
        self.assertIn("Not numbered: numbering stopped at position 2",
                      pages["2003"][col["Notes"]])
        self.assertIn("no downloader report has a row", pages["2004"][col["Notes"]])
        self.assertEqual(pages["1001"][col["Unstamped File"]], "1001_u.pdf")
        summary = {r[0]: r for r in rep["Summary"][1:]}
        self.assertEqual(summary["Sample Journal, vol. 19, iss. 1"][4:6], [1, 15])
        iss2 = summary["Sample Journal, vol. 19, iss. 2"]
        self.assertEqual(iss2[4], 16)
        self.assertIsNone(iss2[5])
        self.assertIn("Numbering stopped at position 2 (article 2002)", iss2[11])
        self.assertEqual(summary["Sample Journal, vol. 19, iss. 1"][1],
                         "restart at page 1")
        self.assertEqual(iss2[1], "continue from the previous issue")
        checks = {r[0]: r for r in rep["Checks"][1:]}
        self.assertEqual(checks["Cover-page check"][1], "4 of 5 item(s) agree")
        self.assertIn("table of contents was not checked", checks["Order"][2])

    def test_reports_saved_elsewhere_are_read_too(self):
        arr = self.build_run()
        elsewhere = os.path.join(self.tmp, "reports")
        os.makedirs(elsewhere)
        for n in os.listdir(os.path.join(self.run, CTX)):
            if n.endswith(".xlsx"):
                shutil.move(os.path.join(self.run, CTX, n), elsewhere)
        M.count_worker({"base_url": BASE}, arr, self.run, elsewhere, self.out)
        with M.LOCK:
            self.assertEqual(M.STATE["phase"], "done", M.STATE["summary"])

    def test_no_reports_is_an_error_that_says_where_it_looked(self):
        arr = self.build_run()
        os.remove(os.path.join(self.run, CTX, "DC_FileDownload_Retry_{}_20260101_120000.xlsx".format(CTX)))
        M.count_worker({"base_url": BASE}, arr, self.run, "", self.out)
        with M.LOCK:
            self.assertEqual(M.STATE["phase"], "error")
            self.assertIn("No download report was found in " + self.run,
                          M.STATE["summary"])
            self.assertIn("or in the folders directly inside it",
                          M.STATE["summary"])

    def test_nothing_numbered_is_red(self):
        make_pdf(os.path.join(self.run, CTX, "u.pdf"), 3)
        make_pdf(os.path.join(self.run, CTX, "s.pdf"), 3)
        write_dl_report(os.path.join(self.run, "DC_FileDownload_Retry_20260101_120000.xlsx"), [
            dl_row(1001, "current-unstamped", saved="u.pdf"),
            dl_row(1001, "current", saved="s.pdf")])
        arr, _ = M.validate_arrangement(arrangement(issue([item(1001, 1)])), BASE)
        M.count_worker({"base_url": BASE}, arr, self.run, "", self.out)
        with M.LOCK:
            self.assertEqual((M.STATE["phase"], M.STATE["outcome"]), ("done", "red"))


# ---------------------------------------------------------------------------
# The server: one validator per input, folders named, runs claimed once
# ---------------------------------------------------------------------------
def _http(port, path, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:{}{}".format(port, path),
                                 data=data, headers=dict(headers or {},
                                 **({"Content-Type": "application/json"} if data else {})))
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class Server(unittest.TestCase):
    def setUp(self):
        from http.server import ThreadingHTTPServer
        self.tmp = tempfile.mkdtemp(prefix="dcpn-srv-")
        self._reg, self._fetch = M.REGISTRY_PATH, M.fetch_url
        M.REGISTRY_PATH = Path(self.tmp) / "journals.json"
        M.REGISTRY_PATH.write_text(json.dumps({"journals": [
            {"id": CTX, "name": "Sample Journal", "page_numbering": "continue",
             "cover_pages": 2}]}))
        session = {"base_url": BASE, "hub_url": "http://127.0.0.1:8750"}
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                       M.make_handler(session, M.build_page(session)))
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        with M.LOCK:
            M.STATE["phase"] = "idle"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        M.REGISTRY_PATH, M.fetch_url = self._reg, self._fetch
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait(self):
        for _ in range(200):
            with M.LOCK:
                if M.STATE["phase"] not in M.RUNNING_PHASES:
                    return dict(M.STATE)
            time.sleep(0.02)
        self.fail("the run did not finish")

    def test_list_then_plan_then_download(self):
        code, body = _http(self.port, "/api/journals")
        self.assertEqual(json.loads(body)["journals"][0]["cover_pages"], 2)
        M.fetch_url = FakeFeed([feed([record(2001, 1, iss="2"), record(1001, 1)])])
        code, body = _http(self.port, "/api/list", {"sets": [
            {"journal": CTX, "volume": "19", "issue": ""}]})
        self.assertEqual(code, 200, body)
        st = self.wait()
        issues = st["listing"]["issues"]
        self.assertEqual([(g["issue"], g["mode"], g["cover"], g["first"])
                          for g in issues],
                         [("1", "continue", 2, "1"), ("2", "continue", 2, "")])
        code, body = _http(self.port, "/api/plan", {"arrangement": st["listing"],
                                                     "out_dir": self.tmp})
        self.assertEqual(code, 200, body)
        st = self.wait()
        self.assertEqual(st["phase"], "done", st["summary"])
        self.assertTrue(os.path.isfile(st["link_file"]))
        code, body = _http(self.port, "/download")
        self.assertEqual((code, body[:2]), (200, b"PK"))

    def test_a_listing_row_is_checked_by_the_shared_validator(self):
        code, body = _http(self.port, "/api/list", {"sets": [
            {"journal": CTX, "volume": "x"}]})
        self.assertEqual(code, 400)
        self.assertIn("volume and issue must be numbers", json.loads(body)["error"])
        code, body = _http(self.port, "/api/list", {"sets": [
            {"journal": CTX, "volume": "1"}, {"journal": CTX, "volume": "1"}]})
        self.assertIn("repeats an earlier row", json.loads(body)["error"])

    def test_plan_and_start_refuse_the_same_arrangement(self):
        bad = arrangement(issue([item(1001, 1, on=False)]))
        for path, body in (("/api/plan", {"arrangement": bad, "out_dir": self.tmp}),
                           ("/api/start", {"arrangement": bad, "run_dir": self.tmp,
                                           "out_dir": self.tmp})):
            code, said = _http(self.port, path, body)
            self.assertEqual(code, 400)
            self.assertIn("Tick at least one", json.loads(said)["error"])

    def test_one_run_at_a_time(self):
        with M.LOCK:
            M.STATE["phase"] = "counting"
        try:
            code, _ = _http(self.port, "/api/list", {"sets": [{"journal": CTX}]})
            self.assertEqual(code, 409)
            code, _ = _http(self.port, "/api/clear", {})
            self.assertEqual(code, 409)
        finally:
            with M.LOCK:
                M.STATE["phase"] = "idle"

    def test_a_foreign_page_is_refused(self):
        code, _ = _http(self.port, "/api/list", {"sets": []},
                        {"Origin": "https://elsewhere.example.org"})
        self.assertEqual(code, 403)


# ---------------------------------------------------------------------------
# The page's own arrangement functions, driven in node
# ---------------------------------------------------------------------------
def js_function(src, name):
    i = src.index("function {}(".format(name))
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise ValueError(name)


NODE = shutil.which("node")


@unittest.skipIf(NODE is None, "node is not on PATH, so the page's script was not driven")
class Page(unittest.TestCase):
    def run_js(self, code):
        page = M.build_page({"base_url": BASE}).decode("utf-8")
        fns = "".join(js_function(page, n) + "\n" for n in ("arrMove", "hasEarlier"))
        out = subprocess.run([NODE, "-e", fns + code], capture_output=True,
                             timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr.decode("utf-8", "replace"))
        return out.stdout.decode("utf-8").strip()

    def test_moves(self):
        got = self.run_js(
            "const a=['A','B','C','D'];"
            "const r=[arrMove(a,0,2), a.join('')];"
            "const b=['A','B','C']; r.push(arrMove(b,2,-5), b.join(''));"
            "const c=['A','B','C']; r.push(arrMove(c,0,99), c.join(''));"
            "const d=['A','B']; r.push(arrMove(d,5,0), d.join(''));"
            "console.log(JSON.stringify(r));")
        self.assertEqual(json.loads(got), [2, "BCAD", 0, "CAB", 2, "BCA", 5, "AB"])

    def test_has_earlier(self):
        got = self.run_js(
            "const I=[{journal:'j',volume:'1',issue:'1'},{journal:'j',volume:'1',issue:'2'},"
            "{journal:'j',volume:'2',issue:'1'},{journal:'k',volume:'2',issue:'1'}];"
            "console.log(JSON.stringify([0,1,2,3].map(i=>hasEarlier(I,i))));")
        self.assertEqual(json.loads(got), [False, True, False, False])

    def test_the_page_carries_no_backslash(self):
        src = (HERE / "dc_page_numbers.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    getattr(t, "id", "") == "PAGE" for t in node.targets):
                seg = ast.get_source_segment(src, node.value)
                own = seg.split("/* /DC-ISSUE */", 1)[1]
                self.assertNotIn("\\", own)
                return
        self.fail("PAGE not found")


# ---------------------------------------------------------------------------
# The DOI & XML Generator: the registry's new fields and its rows
# ---------------------------------------------------------------------------
class DOIRegistry(unittest.TestCase):
    def test_backfill_and_validation(self):
        reg = DOI.backfill_registry({"journals": [{"id": "a-j", "name": "A"}]})
        self.assertEqual((reg["journals"][0]["page_numbering"],
                          reg["journals"][0]["cover_pages"]), ("restart", 1))
        self.assertEqual(DOI.validate_registry(reg), "")
        reg["journals"][0]["cover_pages"] = 12
        self.assertIn("cover pages", DOI.validate_registry(reg))
        reg["journals"][0].update(cover_pages=1, page_numbering="sometimes")
        self.assertIn("page numbering", DOI.validate_registry(reg))

    def test_the_rows_answer_as_before(self):
        reg = DOI.backfill_registry({"journals": [
            {"id": "yp", "name": "Y", "structure": "yearly"}]})
        cases = [
            ([{"journal": ""}], "Volume / issue #1: enter a journal structure ID."),
            ([{"journal": "a-j", "volume": "v"}],
             "Volume / issue #1: volume and issue must be numbers."),
            (["x"], "Bad volume / issue #1."),
            ([{"journal": "a-j"}],
             "Volume / issue #1: pick at least one action (mint DOIs and/or XML output)."),
        ]
        for rows, want in cases:
            self.assertEqual(DOI.validate_sets(rows, reg), (None, want))
        sets, msg = DOI.validate_sets([{"journal": "yp", "syear": "2025",
                                        "issue": "3", "bepress": True}], reg)
        self.assertEqual(msg, "")
        self.assertEqual((sets[0]["syear"], sets[0]["issue"]), ("2025", ""))
        sets, msg = DOI.validate_sets([{"journal": "yp", "bepress": True}], reg)
        self.assertIn("yearly series", msg)

    def test_the_editor_saves_the_new_fields(self):
        from http.server import ThreadingHTTPServer
        tmp = tempfile.mkdtemp(prefix="dcdoi-reg-")
        old = DOI.REGISTRY_PATH
        DOI.REGISTRY_PATH = Path(tmp) / "journals.json"
        session = {"base_url": BASE}
        srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                  DOI.make_handler(session, DOI.build_page(session)))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            reg = DOI.load_registry()
            reg["journals"] = [{"id": "a-j", "name": "A", "page_numbering": "continue",
                                "cover_pages": 2}]
            code, body = _http(srv.server_address[1], "/api/journals", reg)
            self.assertEqual(code, 200, body)
            saved = json.loads(DOI.REGISTRY_PATH.read_text())["journals"][0]
            self.assertEqual((saved["page_numbering"], saved["cover_pages"]),
                             ("continue", 2))
            reg["journals"][0]["cover_pages"] = "2"
            code, body = _http(srv.server_address[1], "/api/journals", reg)
            self.assertEqual(code, 400)
        finally:
            srv.shutdown()
            srv.server_close()
            DOI.REGISTRY_PATH = old
            shutil.rmtree(tmp, ignore_errors=True)


def main():
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    skipped = [(t.id().split(".", 1)[1], why) for t, why in result.skipped]
    print("Page Number Generator — verification")
    print("  tests run: {}, failures: {}, errors: {}, skipped: {}".format(
        result.testsRun, len(result.failures), len(result.errors),
        len(skipped)))
    for name, why in skipped:
        print("  SKIPPED  {} — {}".format(name, why))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
