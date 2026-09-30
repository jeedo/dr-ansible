# Fixture: a module that returns keyword arguments directly to exit_json and
# has keys that only fail_json returns (rc, stderr). RETURN is a placeholder.

DOCUMENTATION = r"""
module: keywords
short_description: Return keyword arguments
"""

RETURN = r"""#"""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict(fail=dict(type="bool", default=False)))

    if module.params["fail"]:
        module.fail_json(msg="asked to fail", rc=1, stderr="boom")

    module.exit_json(
        changed=False,
        ping="pong",
        count=3,
        ratio=0.5,
        items=["a", "b"],
        enabled=True,
    )


if __name__ == "__main__":
    main()
