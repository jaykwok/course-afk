import unittest

from core.menu_keys import (
    MAX_MENU_OPTIONS,
    ensure_menu_option_count,
    menu_key_for_index,
    menu_key_labels,
    menu_keys_hint,
    parse_menu_key,
)


class MenuKeysTests(unittest.TestCase):
    def test_tenth_option_uses_zero_key(self):
        self.assertEqual(menu_key_for_index(10, 10), "0")
        self.assertEqual(menu_key_labels(10)[-1], "0")
        self.assertEqual(menu_keys_hint(10), "1-9/0")

    def test_fewer_than_ten_uses_plain_digits(self):
        self.assertEqual(menu_key_labels(3), ["1", "2", "3"])
        self.assertEqual(menu_keys_hint(3), "1-3")
        self.assertIsNone(parse_menu_key("0", 3))

    def test_parse_zero_and_ten_for_full_menu(self):
        self.assertEqual(parse_menu_key("0", 10), 10)
        self.assertEqual(parse_menu_key("10", 10), 10)
        self.assertEqual(parse_menu_key("1", 10), 1)
        self.assertEqual(parse_menu_key("9", 10), 9)
        self.assertIsNone(parse_menu_key("11", 10))
        self.assertIsNone(parse_menu_key("", 10))

    def test_rejects_more_than_max_options(self):
        with self.assertRaises(ValueError):
            ensure_menu_option_count(MAX_MENU_OPTIONS + 1)
        with self.assertRaises(ValueError):
            menu_key_labels(11)


if __name__ == "__main__":
    unittest.main()
