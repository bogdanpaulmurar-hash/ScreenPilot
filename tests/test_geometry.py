"""fit_view: captures are shown 1:1 in physical pixels on scaled (high-DPI) screens."""

import unittest

from screenpilot.capture.geometry import fit_view


class FitViewTests(unittest.TestCase):
    def test_small_image_is_one_to_one_physical_at_150_percent(self):
        left, top, w, h = fit_view(600, 300, 1000, 800, dpr=1.5)
        self.assertAlmostEqual(w * 1.5, 600)  # 600 image px -> 600 screen px, no upscaling
        self.assertAlmostEqual(h * 1.5, 300)
        self.assertAlmostEqual(left * 1.5, round(left * 1.5))  # origin on the physical pixel grid
        self.assertAlmostEqual(top * 1.5, round(top * 1.5))

    def test_large_image_is_fitted_and_centred(self):
        left, top, w, h = fit_view(4000, 2000, 1000, 800, dpr=1.0, margin=10)
        self.assertAlmostEqual(w, 980)
        self.assertAlmostEqual(h, 490)
        self.assertAlmostEqual(left, 10)

    def test_degenerate_inputs(self):
        self.assertEqual(fit_view(0, 10, 100, 100), (0.0, 0.0, 0.0, 0.0))
        self.assertEqual(fit_view(10, 10, 100, 100, dpr=0)[2], 10)  # invalid dpr treated as 1


if __name__ == "__main__":
    unittest.main()
