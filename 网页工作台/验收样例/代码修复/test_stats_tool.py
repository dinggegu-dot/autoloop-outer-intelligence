import unittest

from stats_tool import count_nonempty


class CountsTest(unittest.TestCase):
    def test_empty_input(self):
        self.assertEqual(count_nonempty([]), 0)

    def test_blank_lines_are_not_records(self):
        self.assertEqual(count_nonempty(['甲', ' ', '\t', '', '乙\n']), 2)
