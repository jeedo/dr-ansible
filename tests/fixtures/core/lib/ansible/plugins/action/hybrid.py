# Fixture: the action plugin for the hybrid module. It runs the module on the
# target with _execute_module, merges that result, and adds keys of its own,
# like the copy action.

from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        module_result = self._execute_module(
            module_name="ansible.builtin.hybrid",
            module_args=self._task.args,
            task_vars=task_vars,
        )
        result.update(module_result)
        result["transferred"] = True
        return result
