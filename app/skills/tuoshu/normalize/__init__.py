"""tuoshu 归一子包门面（P4-1 自 normalizer.py 拆分）：消费方统一 from .normalize import。

日期归一符号（P3 起）单一实现在 app/core/text_normalize.py，此处 re-export
保持消费方符号来源不变。
"""


from app.core.text_normalize import (  # noqa: F401
    STANDARD_CONTAINER_LENGTHS,
    STANDARD_CONTAINER_SUFFIXES,
    normalize_date_value,
    parse_date_or_none,
)

from .aliases import (  # noqa: F401
    KNOWN_CONTAINER_TYPES,
)
from .common import (  # noqa: F401
    extract_number,
    normalize_label,
)
from .containers import (  # noqa: F401
    is_known_container_type,
    normalize_container_type,
)
from .issues import (  # noqa: F401
    normalize_review_issues,
)
from .llm_output import (  # noqa: F401
    normalize_llm_output,
)
from .tables import (  # noqa: F401
    is_separator_row,
    parse_html_table_rows,
    pipe_row_cells,
)

__all__ = [
    "KNOWN_CONTAINER_TYPES",
    "STANDARD_CONTAINER_LENGTHS",
    "STANDARD_CONTAINER_SUFFIXES",
    "extract_number",
    "is_known_container_type",
    "is_separator_row",
    "normalize_container_type",
    "normalize_date_value",
    "normalize_label",
    "normalize_llm_output",
    "normalize_review_issues",
    "parse_date_or_none",
    "parse_html_table_rows",
    "pipe_row_cells",
]
