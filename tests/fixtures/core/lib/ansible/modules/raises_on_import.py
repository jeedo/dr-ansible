# Fixture: an import trap. Importing or executing this file raises. dr-ansible
# must only ever parse it (AC-10); a test that audits it proves nothing ran.
# The code after the trap still gives static analysis something to find.

DOCUMENTATION = r"""
module: raises_on_import
short_description: Fail loudly if anything imports this file
"""

raise RuntimeError("dr-ansible must never import or execute module code")

from ansible.module_utils.basic import AnsibleModule  # noqa: E402


def main():
    module = AnsibleModule(argument_spec=dict())
    module.exit_json(changed=False, trapped=False)
