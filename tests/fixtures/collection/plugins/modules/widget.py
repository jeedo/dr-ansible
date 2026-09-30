# Fixture: a collection module (FQCN example.widgets.widget) with RETURN present.

DOCUMENTATION = r"""
module: widget
short_description: Manage a widget
"""

RETURN = r"""
widget_id:
    description: Identifier of the widget.
    returned: success
    type: int
    sample: 12
"""

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(argument_spec=dict(name=dict(type="str", required=True)))
    module.exit_json(changed=True, widget_id=12, name=module.params["name"])


if __name__ == "__main__":
    main()
