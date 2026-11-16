# Automatic Oracle updates

The workflow in `.github/workflows/deploy.yml` tests pull requests and pushes. After tests pass, pushes to `main` deploy to your existing Oracle Ubuntu VM. You can also run it from GitHub's Actions tab on `main`. Change both `main` references in the workflow if your deployment branch differs.

## One-time setup

1. Push this project, including `.github/workflows/deploy.yml`, tests, and the sample JSON files, to a GitHub repository. Keep `.env` and private SSH keys outside Git. The bot must already be installed as `outage-bot.service` with `/etc/outage-bot.env` and the `outagebot` service user.

2. Create a dedicated deployment key on your computer:

   ```bash
   ssh-keygen -t ed25519 -f ~/.ssh/outage-deploy -N ""
   ```

   Add the contents of `~/.ssh/outage-deploy.pub` to `/home/ubuntu/.ssh/authorized_keys` on the VM using your existing SSH access. Never copy the private key into that file. The workflow uses the Ubuntu deployment user and its existing passwordless sudo; the private key therefore grants administrative access to the VM. Limit repository write access to trusted maintainers.

3. Pin the VM's SSH host key. On your computer:

   ```bash
   ssh-keyscan -H -t ed25519 YOUR_VM_IP > oracle-known-hosts
   ssh-keygen -lf oracle-known-hosts
   ```

   Compare the fingerprint with this command run on the VM through your existing trusted SSH connection:

   ```bash
   sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
   ```

   Proceed only when they match. The workflow uses strict host-key verification.

4. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret** and add:

   | Secret | Value |
   | --- | --- |
   | `ORACLE_HOST` | VM's public IP or hostname |
   | `ORACLE_USER` | `ubuntu` |
   | `ORACLE_SSH_KEY` | Entire contents of `~/.ssh/outage-deploy`, including header/footer |
   | `ORACLE_KNOWN_HOSTS` | Entire verified contents of `oracle-known-hosts` |

   The bot token stays on Oracle. It is not needed by GitHub.

5. Ensure GitHub's runner can reach TCP port 22 on the VM. An Oracle rule allowing only your home IP will block it. Standard hosted runners use changing IP ranges; see [GitHub's networking documentation](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#ip-addresses). Configure an appropriate network path before enabling deployment. This workflow does not modify Oracle security lists or firewall rules. If you want to keep SSH limited to your home IP, use a separate trusted self-hosted deployment runner on your network instead, and adapt the deployment job's `runs-on` label. Do not run untrusted pull-request code on a privileged self-hosted runner.

## Push an update

Commit your intended files and push to `main`. On GitHub, open **Actions → Test and deploy** to see test and deployment logs. The workflow also has a **Run workflow** button. Repository secrets are used only by the main-branch deployment job, never the pull-request test job.

The remote script builds a new virtual environment inside `/opt/outage-bot/releases/` while the old bot keeps running. It then stops the service, points `/opt/outage-bot/current` at the new release, and writes a systemd drop-in at `/etc/systemd/system/outage-bot.service.d/20-release.conf`. The existing service's user, token configuration, and database path remain in effect. Source files removed from Git do not survive inside the new release.

If the process crashes during its 15-second startup check, the script restores the previous release/configuration and restarts the service. This check verifies the process only; it cannot establish that Telegram delivery or provider fetching works. Check `/status` and `/today` after deploying. Rollback does not undo database migrations; this version introduces none.

Old releases are retained for recovery and consume disk space; review and remove unused releases periodically, keeping the active release and a known working predecessor. The script does not update the base systemd unit automatically; apply intentional changes to `deploy/outage-bot.service` separately.

Local validation performed: bot unit tests and `bash -n deploy/remote-update.sh`. A full deployment requires your repository secrets and VM access and has not been run from this workspace.
