# 测试包标记文件
#
# 挂课/考试流程里的「随机停顿」是真实 sleep（见 core.humanize）：不关掉的话
# 整套测试会被 COURSE_GAP 等区间拖到几分钟。这里在导入 core.config 之前把这些
# 区间置零；需要验证停顿本身的用例自己 patch 常量，不依赖这里的默认值。
#
# load_dotenv 默认不覆盖已存在的环境变量，所以这里的设置对 .env 也生效。
import os

for _name in (
    "SECTION_GAP_MIN",
    "SECTION_GAP_MAX",
    "COURSE_GAP_MIN",
    "COURSE_GAP_MAX",
    "EXAM_OPTION_GAP_MIN",
    "EXAM_OPTION_GAP_MAX",
    "EXAM_SUBMIT_GAP_MIN",
    "EXAM_SUBMIT_GAP_MAX",
):
    os.environ.setdefault(_name, "0")
