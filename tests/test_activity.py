import unittest

from tv2arr.activity import activity_columns


def event(message, **changes):
    return {
        "kind": "Scan", "message": message, "source": None,
        "listed_count": None, "new_count": None, "requested_count": None,
    } | changes


class ActivityTest(unittest.TestCase):
    def test_legacy_scan_summaries_are_split_without_modifying_the_record(self):
        for source, listed, new, requested, suffix, expected_notes in (
            ("REplay.lv", 65, 0, 1, " 1 requests sent.", ""),
            ("TV3 Play", 12, 12, 0, " First scan saved.", "First scan saved."),
            ("Apollo Kino", 48, 2, 3, " First scan saved. 3 requests sent. 1 lookup errors.",
             "First scan saved. 1 lookup errors."),
            ("REplay.lv", 0, 0, 1, " 1 request sent.", ""),
        ):
            with self.subTest(source=source, suffix=suffix):
                row = event(f"{source}: {listed} listed, {new} new.{suffix}")
                result = activity_columns(row)
                self.assertEqual(result["source"], source)
                self.assertEqual((result["listed_count"], result["new_count"], result["requested_count"]),
                                 (listed, new, requested))
                self.assertEqual(result["message"], expected_notes)
                self.assertIsNone(row["listed_count"])
                self.assertIn("listed,", row["message"])

    def test_unknown_error_and_maintenance_counts_stay_unknown(self):
        for row in (event("REplay.lv: Could not load the listing."),
                    event("Catalogue scan stopped unexpectedly."),
                    event("Removed 12 expired entries.", kind="Maintenance"),
                    event("REplay.lv: 65 listed, 0 new.", kind="Other")):
            with self.subTest(row=row):
                self.assertEqual(activity_columns(row), row)

    def test_structured_zero_counts_are_authoritative(self):
        row = event("No lookups completed.", source="replay", listed_count=65,
                    new_count=0, requested_count=0)
        self.assertEqual(activity_columns(row), row)
