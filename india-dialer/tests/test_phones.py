"""Indian number parsing.  python3 -m unittest discover india-dialer/tests"""
import unittest
from unittest import mock

from _base import APP  # noqa: F401  (puts the app on sys.path)
import phones


def kinds(cell):
    numbers, intl, bad = phones.parse_cell(cell)
    return [(n["e164"], n["kind"]) for n in numbers], intl, bad


class MultiNumberCells(unittest.TestCase):
    def test_slash_and_comma_split_in_order(self):
        got, intl, bad = kinds("044-45050301 / +91 99652 51951")
        self.assertEqual(got, [("+914445050301", "landline"), ("+919965251951", "mobile")])
        got, _, _ = kinds("+91 98202 04373, +91 98200 33608")
        self.assertEqual([e for e, _ in got], ["+919820204373", "+919820033608"])

    def test_suffix_shorthand_keeps_the_front_of_the_previous_number(self):
        got, _, _ = kinds("+91 80 47780293 / 4692")
        self.assertEqual([e for e, _ in got], ["+918047780293", "+918047784692"])

    def test_local_number_keeps_the_previous_std_code(self):
        got, _, _ = kinds("0422-4330330 / 2332100")
        self.assertEqual([e for e, _ in got], ["+914224330330", "+914222332100"])

    def test_second_number_without_country_code(self):
        got, _, _ = kinds("+91-94053 64747 / 99225 71999")
        self.assertEqual(got, [("+919405364747", "mobile"), ("+919922571999", "mobile")])

    def test_duplicates_collapse(self):
        got, _, _ = kinds("9900688309 / +91 99006 88309")
        self.assertEqual(len(got), 1)


class Formats(unittest.TestCase):
    def test_every_way_of_writing_one_number(self):
        for cell in ("9900688309", "+91 99006 88309", "+91-9900688309", "09900688309", "919900688309", "0091 9900688309"):
            self.assertEqual(kinds(cell)[0], [("+919900688309", "mobile")], cell)

    def test_trunk_zero_inside_brackets(self):
        self.assertEqual(kinds("+91 (020) 66424600")[0], [("+912066424600", "landline")])

    def test_landline_codes_that_start_with_1(self):
        for cell, e164 in (("+91 129 428 3900", "+911294283900"), ("+91 124 2824000", "+911242824000"), ("+91-141-2330614", "+911412330614")):
            self.assertEqual(kinds(cell)[0], [(e164, "landline")], cell)

    def test_toll_free_is_dialled_without_country_code(self):
        numbers, _, _ = phones.parse_cell("1800 425 5758 (toll free)")
        self.assertEqual((numbers[0]["kind"], numbers[0]["dial"]), ("tollfree", "18004255758"))

    def test_international_numbers_are_set_aside(self):
        got, intl, bad = kinds("+1-855-259-3709")
        self.assertEqual((got, intl, bad), ([], ["+1-855-259-3709"], []))
        got, intl, _ = kinds("+91 98202 04373 / +1-669 264 7456")
        self.assertEqual((len(got), intl), (1, ["+1-669 264 7456"]))

    def test_junk_is_reported_not_guessed(self):
        got, intl, bad = kinds("12345")
        self.assertEqual((got, bad), ([], ["12345"]))
        self.assertEqual(kinds("")[0], [])


class LandlineOrMobile(unittest.TestCase):
    """The naive 'starts with 6-9' rule misfiles city landlines."""

    CASES = [
        ("+91 8045849995", "landline"),       # Bangalore 080 4... (IndiaMART virtual number)
        ("+91 7949366017", "landline"),       # Ahmedabad 079 4...
        ("+91 831 2499000", "landline"),      # Belgaum 0831
        ("080 6743 6743", "landline"),        # ambiguous range, written with a trunk 0
        ("+91-7292-403608", "landline"),      # ambiguous range, written as STD + local
        ("+91 8073887184", "mobile"),         # ambiguous range, written as one 10-digit block
        ("+91 86670 88060", "mobile"),        # ambiguous range, written 5 + 5
        ("+91 9545273519", "mobile"),
        ("+91 7412025252", "mobile"),
    ]

    def test_with_number_ranges(self):
        if not phones.PRECISE:
            self.skipTest("phonenumbers not installed")
        for cell, kind in self.CASES:
            self.assertEqual(kinds(cell)[0][0][1], kind, cell)

    def test_stdlib_fallback_still_gets_the_common_cases(self):
        with mock.patch.object(phones, "PRECISE", False):
            for cell, kind in self.CASES:
                self.assertEqual(kinds(cell)[0][0][1], kind, cell)


class Display(unittest.TestCase):
    def test_pretty(self):
        self.assertEqual(phones.pretty("+919820204373", "mobile"), "98202 04373")
        self.assertEqual(phones.pretty("+918041161000", "landline"), "080 4116 1000")
        self.assertEqual(phones.pretty("+918067436743", "landline"), "080 6743 6743")
        self.assertEqual(phones.pretty("18004255758", "tollfree"), "1800 425 5758")
        self.assertEqual(phones.wa_digits("+919820204373"), "919820204373")


if __name__ == "__main__":
    unittest.main()
