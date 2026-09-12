import unittest

import theme


class ShadeTests(unittest.TestCase):
    def test_positive_factor_lightens_toward_white(self):
        self.assertEqual(theme.shade("#000000", 0.5), "#808080")
        self.assertEqual(theme.shade("#000000", 1.0), "#FFFFFF")

    def test_negative_factor_darkens_toward_black(self):
        self.assertEqual(theme.shade("#FFFFFF", -0.5), "#808080")
        self.assertEqual(theme.shade("#FFFFFF", -1.0), "#000000")

    def test_zero_factor_and_hash_optional(self):
        self.assertEqual(theme.shade("#3C5FC1", 0.0), "#3C5FC1")
        self.assertEqual(theme.shade("3C5FC1", 0.0), "#3C5FC1")

    def test_factor_is_clamped(self):
        self.assertEqual(theme.shade("#101010", 5.0), "#FFFFFF")
        self.assertEqual(theme.shade("#EEEEEE", -5.0), "#000000")

    def test_invalid_input_is_returned_unchanged(self):
        self.assertEqual(theme.shade("not-a-color", 0.3), "not-a-color")
        self.assertEqual(theme.shade("#ABC", 0.3), "#ABC")
        self.assertIsNone(theme.shade(None, 0.3))


class LuminanceTests(unittest.TestCase):
    def test_black_and_white_bounds(self):
        self.assertEqual(theme.relative_luminance("#000000"), 0.0)
        self.assertEqual(theme.relative_luminance("#FFFFFF"), 1.0)

    def test_light_surface_reads_above_half(self):
        self.assertGreater(theme.relative_luminance(theme.COLORS["surface"]), 0.5)

    def test_dark_surface_reads_below_half(self):
        self.assertLess(theme.relative_luminance(theme.DARK_COLORS["surface"]), 0.5)

    def test_invalid_input_falls_back_to_mid(self):
        self.assertEqual(theme.relative_luminance("bad"), 0.5)
        self.assertEqual(theme.relative_luminance(None), 0.5)


class SpaceTests(unittest.TestCase):
    def test_single_key_returns_scalar(self):
        self.assertEqual(theme.space("md"), 12)

    def test_multiple_keys_return_tuple_in_order(self):
        self.assertEqual(theme.space("lg", "sm"), (16, 8))

    def test_scale_is_on_a_four_pixel_grid(self):
        self.assertTrue(all(value % 4 == 0 for value in theme.SPACE.values()))


if __name__ == "__main__":
    unittest.main()
