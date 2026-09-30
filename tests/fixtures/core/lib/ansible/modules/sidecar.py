# Fixture: a module whose DOCUMENTATION and RETURN live in the sidecar file
# sidecar.yml next to it instead of in this file.

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict())
    module.exit_json(changed=False, version="1.0", features=["a", "b"])


if __name__ == "__main__":
    main()
