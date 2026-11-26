"""Run:  python -m unittest test_problem1 -v   (uses the local mock API, no internet needed)"""
import os
import sqlite3
import tempfile
import threading
import unittest

import mock_api
import q1_books
import q2_scores
import q3_csv_import

HERE = os.path.dirname(os.path.abspath(__file__))


class WithMockAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = mock_api.make_server(0)  # port 0 = any free port
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")

    def tearDown(self):
        self.tmp.cleanup()


class TestBooks(WithMockAPI):
    def test_store_is_idempotent_and_drops_incomplete(self):
        for _ in range(2):  # running twice must not duplicate
            q1_books.main(["--url", self.base + "/books", "--db", self.db])
        conn = sqlite3.connect(self.db)
        rows = conn.execute("SELECT title, author, publication_year FROM books").fetchall()
        conn.close()
        self.assertEqual(len(rows), 6)  # 7 records, 1 duplicate in the payload
        self.assertIn(("A Book With No Year", "Anonymous", None), rows)

    def test_clean_rejects_missing_fields_and_bad_year(self):
        good, dropped = q1_books.clean([{"title": "T", "author": ""},
                                        {"title": "X", "author": "Y", "publication_year": "abc"}])
        self.assertEqual((good, dropped), ([("X", "Y", None)], 1))

    def test_404_raises_without_retrying(self):
        with self.assertRaises(Exception):
            q1_books.get_json(self.base + "/nope")


class TestScores(WithMockAPI):
    def test_averages_ignore_missing_scores(self):
        students = q1_books.get_json(self.base + "/students")
        per, class_avg, by_subj = q2_scores.averages(students)
        d = dict(per)
        self.assertAlmostEqual(d["Farah"], 92.5)  # chemistry is null -> average of 2, not 3
        self.assertAlmostEqual(d["Asha"], (92 + 85 + 78) / 3)
        self.assertEqual(len(per), 7)
        self.assertAlmostEqual(class_avg, 1568 / 20)  # 20 real scores

    def test_chart_written(self):
        out = os.path.join(self.tmp.name, "c.png")
        q2_scores.main(["--url", self.base + "/students", "--out", out])
        with open(out, "rb") as f:
            self.assertEqual(f.read(8), b"\x89PNG\r\n\x1a\n")


class TestCSV(WithMockAPI):
    def test_import_sample(self):
        inserted, dupes, rejects = q3_csv_import.main([os.path.join(HERE, "data", "users.csv"), "--db", self.db])
        self.assertEqual(inserted, 5)
        self.assertEqual(len(dupes), 1)    # ASHA.RAO@ == asha.rao@
        self.assertEqual(len(rejects), 2)  # empty name, bad email
        # second run: everything is a duplicate
        inserted, dupes, _ = q3_csv_import.main([os.path.join(HERE, "data", "users.csv"), "--db", self.db])
        self.assertEqual((inserted, len(dupes)), (0, 6))

    def test_missing_column_is_an_error(self):
        bad = os.path.join(self.tmp.name, "bad.csv")
        with open(bad, "w") as f:
            f.write("name,phone\nA,1\n")
        with self.assertRaises(ValueError):
            q3_csv_import.read_rows(bad)


if __name__ == "__main__":
    unittest.main()
