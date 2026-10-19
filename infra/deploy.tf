# Release deploys to the app VM (.github/workflows/vm-deploy.yml,
# docs/decisions.md §275).
#
# A published release runs `deploy/deploy.sh --ref <commit>` on the VM through
# SSM Run Command — no SSH, no inbound port, no key. The GitHub deploy role may
# run exactly ONE thing: the command document below, on exactly ONE instance.
# The document takes a single parameter, the commit to deploy, and refuses
# anything but a full 40-character commit sha before the shell ever sees it, so
# holding the role is not a way to run arbitrary shell as root on the VM.
#
# The role itself is NOT defined here (README.md § The deploy role is not owned
# here): the estate baseline module mints it and owns its trust policy, and its
# description says the project repo attaches its own policies — which is what
# the inline policy at the bottom of this file does, by role name.

# ── Command document ─────────────────────────────────────────────────────────

locals {
  # The VM checkout deploy/bootstrap-vm.sh sets up, and the user every deploy
  # runs as (it owns the checkout and is in the docker group).
  deploy_user = "ec2-user"
  deploy_sh   = "/home/ec2-user/feohledger/deploy/deploy.sh"

  # SSM keeps only the first 24,000 characters of a command's stdout, and the
  # end of a deploy is where a failure is. So the full log goes to a file on the
  # VM and stdout carries only its tail — what the workflow prints. One file per
  # run: a second deploy refused by deploy.sh's lock must not truncate the log
  # of the one still running. Kept 90 days.
  deploy_log_dir  = "/var/log/feoh-deploy"
  deploy_log_tail = 20000
}

resource "aws_ssm_document" "deploy" {
  name            = "${var.project}-vm-deploy"
  document_type   = "Command"
  document_format = "JSON"

  content = jsonencode({
    schemaVersion = "2.2"
    description   = "Deploy one commit of main to the app VM: deploy/deploy.sh --ref <sha>, as ${local.deploy_user}."
    parameters = {
      Ref = {
        type        = "String"
        description = "Full 40-character sha of the commit to deploy. deploy.sh refuses one that is not on origin/main."
        # The only input the caller controls. SSM rejects anything else at
        # SendCommand time, before a shell exists — no quoting, no spaces, no
        # metacharacters can reach the command line below.
        allowedPattern = "^[0-9a-f]{40}$"
      }
    }
    mainSteps = [{
      action = "aws:runShellScript"
      name   = "deploy"
      inputs = {
        # deploy.sh builds the frontend and backend on the VM; ~30 minutes is
        # the documented ceiling, and this bounds a hung build.
        timeoutSeconds = "2700"
        runCommand = [
          "umask 027",
          "mkdir -p ${local.deploy_log_dir}",
          "find ${local.deploy_log_dir} -name '*.log' -mtime +90 -delete",
          "log=${local.deploy_log_dir}/$(date -u +%Y%m%dT%H%M%SZ)-$$.log",
          "echo \"log: $log\"",
          "sudo -iu ${local.deploy_user} ${local.deploy_sh} --ref '{{ Ref }}' > \"$log\" 2>&1",
          "rc=$?",
          "tail -c ${local.deploy_log_tail} \"$log\"",
          "exit $rc",
        ]
      }
    }]
  })
}

# ── Deploy-role permission ───────────────────────────────────────────────────

data "aws_iam_role" "deploy" {
  name = "${var.project}-deploy"
}

data "aws_iam_policy_document" "deploy_vm" {
  # SendCommand is authorized against every resource in the call — the
  # instance AND the document — so naming only this document is what keeps
  # AWS-RunShellScript (or any other document) out of reach, and naming only
  # this instance keeps the role off every other machine in the account.
  statement {
    sid       = "RunTheDeployDocumentOnTheAppVm"
    actions   = ["ssm:SendCommand"]
    resources = [aws_instance.app.arn, aws_ssm_document.deploy.arn]
  }

  # Polls the command to completion and reads its output tail. SSM defines no
  # resource type for GetCommandInvocation, so it cannot be narrowed below
  # "*"; it is read-only, and command output in this account is deploy logs.
  # ListCommandInvocations is deliberately not granted: the workflow knows its
  # own command id and never needs to enumerate anyone else's.
  statement {
    sid       = "ReadTheDeployResult"
    actions   = ["ssm:GetCommandInvocation"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "deploy_vm" {
  name   = "vm-deploy"
  role   = data.aws_iam_role.deploy.name
  policy = data.aws_iam_policy_document.deploy_vm.json
}
