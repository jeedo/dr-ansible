# Fixture: the action plugin for the virtual module. It never runs a module on
# the target; ActionModule.run() builds the whole result, like the fetch action.

from ansible.plugins.action import ActionBase


class ActionModule(ActionBase):
    def run(self, tmp=None, task_vars=None):
        result = super().run(tmp, task_vars)
        src = self._task.args.get("src")

        if src is None:
            result["failed"] = True
            result["msg"] = "src is required"
            return result

        dest = "/tmp/fetched/" + src
        result.update(changed=True, src=src, dest=dest)
        result["checksum"] = "6e642bb8dd5c2e027bf21dd923337cbb4214f827"
        if task_vars.get("validate"):
            result["remote_checksum"] = result["checksum"]

        return dict(result, file=src)
