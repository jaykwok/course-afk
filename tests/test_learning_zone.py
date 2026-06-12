import unittest

from core.learning_zone import extract_learning_links_from_learning_zone_html


class LearningZoneParsingTests(unittest.TestCase):
    def test_extract_learning_links_from_learning_zone_html_parses_app_links(self):
        html = """
        <html>
          <body>
            <a href="https://kc.zhixueyun.com/app/#/resource?businessType=1&businessId=11111111-1111-1111-1111-111111111111">课程A</a>
            <a href="https://kc.zhixueyun.com/app/#/resource?businessType=2&businessId=22222222-2222-2222-2222-222222222222">主题B</a>
            <a href="https://kc.zhixueyun.com/#/study/course/detail/33333333-3333-3333-3333-333333333333">课程C</a>
            <a href="https://kc.zhixueyun.com/#/study/course/detail/33333333-3333-3333-3333-333333333333">课程C重复</a>
            <a href="https://example.com/ignore">忽略</a>
          </body>
        </html>
        """

        self.assertEqual(
            extract_learning_links_from_learning_zone_html(html),
            [
                "https://kc.zhixueyun.com/#/study/course/detail/11111111-1111-1111-1111-111111111111",
                "https://kc.zhixueyun.com/#/study/subject/detail/22222222-2222-2222-2222-222222222222",
                "https://kc.zhixueyun.com/#/study/course/detail/33333333-3333-3333-3333-333333333333",
            ],
        )


if __name__ == "__main__":
    unittest.main()
