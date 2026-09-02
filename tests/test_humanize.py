import unittest

from core.humanize import (
    jitter,
    next_slice,
    sample_between,
    sample_int_between,
)


class JitterTests(unittest.TestCase):
    def test_jitter_stays_inside_ratio_band(self):
        values = [jitter(30, ratio=0.3) for _ in range(200)]

        self.assertTrue(all(21 <= value <= 39 for value in values))
        # 抖动的意义就在于不重复：200 次不该只有一个取值
        self.assertGreater(len(set(values)), 100)

    def test_jitter_returns_zero_for_non_positive_base(self):
        self.assertEqual(jitter(0), 0.0)
        self.assertEqual(jitter(-5), 0.0)

    def test_jitter_respects_minimum_and_maximum(self):
        values = [jitter(10, ratio=0.9, minimum=8, maximum=11) for _ in range(100)]

        self.assertTrue(all(8 <= value <= 11 for value in values))

    def test_sample_between_accepts_reversed_bounds(self):
        values = [sample_between(9, 3) for _ in range(100)]

        self.assertTrue(all(3 <= value <= 9 for value in values))

    def test_sample_int_between_is_inclusive(self):
        values = {sample_int_between(1, 3) for _ in range(200)}

        self.assertEqual(values, {1, 2, 3})


class NextSliceTests(unittest.TestCase):
    def test_slice_never_exceeds_remaining(self):
        for remaining in (0.5, 12, 30, 600):
            for _ in range(50):
                piece = next_slice(remaining, min_slice=20, max_slice=55)
                self.assertLessEqual(piece, remaining)
                self.assertGreater(piece, 0)

    def test_short_remaining_is_consumed_in_one_go(self):
        self.assertEqual(next_slice(40, min_slice=20, max_slice=55), 40)

    def test_long_remaining_is_cut_into_varied_pieces(self):
        pieces = [next_slice(3600, min_slice=20, max_slice=55) for _ in range(100)]

        self.assertTrue(all(20 <= piece <= 55 for piece in pieces))
        self.assertGreater(len(set(pieces)), 50)

    def test_no_stub_tail_is_left_behind(self):
        # 剩 60 秒、单段上限 55：不能切出 5 秒的碎尾巴
        for _ in range(200):
            piece = next_slice(60, min_slice=20, max_slice=55)
            self.assertTrue(piece == 60 or 20 <= piece <= 40)

    def test_zero_remaining_returns_zero(self):
        self.assertEqual(next_slice(0, min_slice=20, max_slice=55), 0.0)


if __name__ == "__main__":
    unittest.main()
