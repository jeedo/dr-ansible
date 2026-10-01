"""The Ansible callback plugin that records results at runtime (FR-14).

The plugin is a plain file in this directory, shipped as package data: point
``ANSIBLE_CALLBACK_PLUGINS`` at :data:`CALLBACK_DIR` and enable
:data:`CALLBACK_NAME`. It is loaded by ansible-core inside the ansible-test
container, so it must not import anything from dr_ansible.
"""

from pathlib import Path

#: The directory holding the callback plugin file.
CALLBACK_DIR = Path(__file__).resolve().parent
#: The plugin's name, for ``ANSIBLE_CALLBACKS_ENABLED``.
CALLBACK_NAME = "dr_ansible_recorder"
