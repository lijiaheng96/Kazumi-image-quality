"""同名比对只规范化明确季号，不猜测续作、特别篇或不同作品。"""
import unittest

from helper.matching import same_title


class TitleMatchingTests(unittest.TestCase):
    def test_explicit_first_season_matches_unsuffixed_title(self):
        self.assertTrue(same_title("葬送的芙莉莲 第一季", "葬送的芙莉莲"))

    def test_chinese_and_arabic_season_numbers_match(self):
        self.assertTrue(same_title("葬送的芙莉莲第二季", "葬送的芙莉莲 第２季"))
        self.assertTrue(same_title("作品 第十二季", "作品 第12季"))

    def test_explicit_english_season_suffixes_match(self):
        self.assertTrue(same_title("葬送的芙莉莲 Season 2", "葬送的芙莉莲 第2季"))
        self.assertTrue(same_title("葬送的芙莉莲 S2", "葬送的芙莉莲 第二季"))

    def test_blank_titles_are_never_matches(self):
        self.assertFalse(same_title("", " "))

    def test_second_season_and_film_are_not_first_season(self):
        for title in ["葬送的芙莉莲 第二季", "葬送的芙莉莲 Season 2",
                      "葬送的芙莉莲 剧场版", "葬送的芙莉莲 OVA", "葬送的芙莉莲 总集篇",
                      "葬送的芙莉莲 第二季第1部分", "葬送的芙莉莲2", "葬送的芙莉莲 第一季 国语版"]:
            with self.subTest(title=title):
                self.assertFalse(same_title(title, "葬送的芙莉莲"))

    def test_original_normalization_and_title_numbers_are_preserved(self):
        self.assertTrue(same_title(" ＡＢＣ　中文 ", "abc中文"))
        self.assertTrue(same_title("86 不存在的战区", "86不存在的战区"))
        self.assertFalse(same_title("86 不存在的战区", "不存在的战区"))
        self.assertFalse(same_title("工作细胞BLACK", "工作细胞"))


if __name__ == "__main__":
    unittest.main()
