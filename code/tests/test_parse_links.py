import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[1]))

from engine import parse_links


class ParseLinksTests(unittest.TestCase):
    def test_same_domain_duplicate_id_keeps_first_url_format(self):
        refs = parse_links(
            "https://www.amazon.com/gp/customer-reviews/RSHARED12345/\n"
            "https://www.amazon.com/review/RSHARED12345\n"
        )

        self.assertEqual(
            [(ref.domain, ref.review_id, ref.raw) for ref in refs],
            [
                (
                    "amazon.com",
                    "RSHARED12345",
                    "https://www.amazon.com/gp/customer-reviews/RSHARED12345/",
                )
            ],
        )

    def test_same_id_on_different_domains_is_kept(self):
        refs = parse_links(
            "https://www.amazon.com/gp/customer-reviews/RSHARED12345/\n"
            "https://www.amazon.in/review/RSHARED12345\n"
        )

        self.assertEqual(
            [(ref.domain, ref.review_id) for ref in refs],
            [
                ("amazon.com", "RSHARED12345"),
                ("amazon.in", "RSHARED12345"),
            ],
        )

    def test_mixed_duplicates_keep_first_occurrence_order(self):
        refs = parse_links(
            "https://www.amazon.in/review/RORDER12345\n"
            "https://www.amazon.com/gp/customer-reviews/RORDER12345/\n"
            "https://www.amazon.in/portal/customer-reviews/srp/-/RORDER12345\n"
            "https://www.amazon.com.br/review/RTHIRD12345\n"
            "https://www.amazon.com/gp/customer-reviews/RORDER12345/\n"
        )

        self.assertEqual(
            [(ref.domain, ref.review_id) for ref in refs],
            [
                ("amazon.in", "RORDER12345"),
                ("amazon.com", "RORDER12345"),
                ("amazon.com.br", "RTHIRD12345"),
            ],
        )

    def test_invalid_domain_is_rejected(self):
        refs = parse_links(
            "https://www.amazon.evil.com/gp/customer-reviews/RINVALID12345/"
        )

        self.assertEqual(refs, [])


if __name__ == "__main__":
    unittest.main()
