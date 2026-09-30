# Fixture: a module that is expected to return nothing, like
# ansible.builtin.include_tasks. It has no RETURN and is on the default exempt
# allowlist, so it must be reported as `exempt`, not `missing`.

DOCUMENTATION = r"""
module: include_tasks
short_description: Dynamically include a task list
"""

EXAMPLES = r"""
- include_tasks: other.yml
"""
