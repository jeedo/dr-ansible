# Fixture: a virtual module. This file is documentation only; the action plugin
# lib/ansible/plugins/action/virtual.py builds the whole result on the
# controller, like ansible.builtin.fetch. RETURN is missing.

DOCUMENTATION = r"""
module: virtual
short_description: A module implemented entirely by its action plugin
options:
  src:
    description: Source path on the target.
    type: path
    required: true
"""

EXAMPLES = r"""
- virtual:
    src: /etc/motd
"""
