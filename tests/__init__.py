# 测试包标记文件
#
# 挂课/考试流程里的「随机停顿」是真实 sleep（见 core.humanize）：不关掉的话
# 整套测试会被 章节和答题等待区间拖到几分钟。这里在导入 core.config 之前把这些
# 区间置零；需要验证停顿本身的用例自己 patch 常量，不依赖这里的默认值。
#
# 配置读取时环境变量优先于 .env，因此这里的测试设置不会被文件覆盖。
import os
import atexit
from pathlib import Path
import tempfile
from tempfile import TemporaryDirectory

# 每个测试进程独享数据目录，回归不能改用户凭证、待办或历史。
_test_temp = Path(__file__).resolve().parents[1] / "build" / "test-temp"
_test_temp.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(_test_temp)
_test_data = TemporaryDirectory(prefix="course-afk-tests-")
atexit.register(_test_data.cleanup)
os.environ["COURSE_AFK_DATA_DIR"] = _test_data.name
os.environ["OPENAI_COMPLETION_API_KEY"] = "test-isolated-key"
os.environ["OPENAI_COMPLETION_BASE_URL"] = "http://127.0.0.1:9/v1"
os.environ["MODEL_NAME"] = "test-model"

for _name in (
    "SECTION_GAP_MIN",
    "SECTION_GAP_MAX",
    "EXAM_OPTION_GAP_MIN",
    "EXAM_OPTION_GAP_MAX",
    "EXAM_SUBMIT_GAP_MIN",
    "EXAM_SUBMIT_GAP_MAX",
):
    os.environ.setdefault(_name, "0")
