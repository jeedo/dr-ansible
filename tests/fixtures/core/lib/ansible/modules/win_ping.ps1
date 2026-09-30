#!powershell
# Fixture: a PowerShell module. dr-ansible has no Python source to analyse and
# must list it as `unsupported` rather than skip it.

#AnsibleRequires -CSharpUtil Ansible.Basic

$module = [Ansible.Basic.AnsibleModule]::Create($args, @{})
$module.Result.ping = "pong"
$module.ExitJson()
