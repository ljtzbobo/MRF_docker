"""任务 DAG 公共骨架与变体。"""

from .task_template import TaskTemplate, build_template, restore_task
from .business_task import BusinessTaskTemplate
from .external import (represent_external_task, represent_external_tasks,
                       represent_normalized_task, represent_template_variant)
from .refinement import (RefinementResult, SpecificationTask,
                         refine_business_task)

__all__ = ["BusinessTaskTemplate", "TaskTemplate", "build_template",
           "represent_external_task", "represent_external_tasks",
           "represent_normalized_task", "represent_template_variant",
           "restore_task", "RefinementResult", "SpecificationTask",
           "refine_business_task"]
