from core.learning_common import (
    calculate_remaining_time,
    check_permission,
    get_course_url,
    is_learned,
    time_to_seconds,
    timer,
)
from core.learning_exam import check_exam_passed, handle_examination, is_subject_url_completed
from core.learning_flows import course_learning, subject_learning
from core.learning_handlers import handle_document, handle_h5, handle_video
from core.learning_popups import (
    check_and_handle_rating_popup,
    check_rating_popup_periodically,
    handle_rating_popup,
)


__all__ = [
    "calculate_remaining_time",
    "check_and_handle_rating_popup",
    "check_exam_passed",
    "check_permission",
    "check_rating_popup_periodically",
    "course_learning",
    "get_course_url",
    "handle_document",
    "handle_examination",
    "handle_h5",
    "handle_rating_popup",
    "handle_video",
    "is_learned",
    "is_subject_url_completed",
    "subject_learning",
    "time_to_seconds",
    "timer",
]
