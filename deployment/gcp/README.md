# Running the backend on Google Cloud

Target: one `g2-standard-8` in `asia-south1` (8 vCPU, 32 GB RAM, one NVIDIA L4 with 24 GB), replacing the g4dn.2xlarge box. Draft of 2026-10-28; none of it has been run. The EC2 box was stopped when this was written, so step 1 reads its real state before anything is built.

Supabase, the S3 bucket and the Vercel frontend stay where they are. What moves is everything on the box: the model weights, the vector index, the services and the `.env`.

## Before you start

- **Credit window.** Billing > Credits shows the credit's End date. The credit must be spent within 90 days of sign-up, and upgrading to a paid account does not extend that.
- **Paid account.** A Free Trial account cannot add GPUs or request quota. Upgrade it (step 2); the remaining credit carries over.
- **GPU quota.** Request it straight after upgrading. Google does not promise approval or give a timeline, so create nothing until it arrives.

What the credit buys (US$300 against Mumbai list prices for `g2-standard-8`, VM only; the disk, the IP, egress and tax come out of the same credit):

| | $/hour | 24/7 | 12 h/day | 8 h/day |
|---|---|---|---|---|
| On-demand | 0.8886 | 14 days | 28 days | 42 days |
| Spot | 0.4526 | 28 days | 55 days | 83 days |

The prices are gcloud-compute.com's copy of Google's list on 2026-10-28, and Spot prices move. Build on Spot and use on-demand for demo days. A Spot VM created with the STOP action (step 3) is stopped, not deleted, when it is preempted, so its disk survives.

Where each command runs:

- **EC2**: the AWS box, from the console's EC2 Instance Connect terminal or from SSH.
- **Cloud Shell**: the terminal in the Google Cloud console. gcloud is installed and signed in.
- **VM**: the new machine. From Cloud Shell use `gcloud compute ssh ubuntu@raikou-gpu --zone=asia-south1-b`, or use the console's SSH button.

Set these in Cloud Shell once per session:

```bash
PROJECT=your-project-id
REGION=asia-south1
ZONE=asia-south1-b
VM=raikou-gpu
```

The L4 is offered in zones a, b and c of asia-south1. If a zone is out of capacity, change `ZONE` and retry.

## 1. Read the EC2 box (EC2)

Start the instance in the AWS console if it is stopped; the Elastic IP (3.7.243.74) comes back with it. Run this and keep the output. Prefix `docker` with `sudo` if it reports a permission error.

```bash
. /etc/os-release; echo "os: $PRETTY_NAME"
echo "sar_env python: $(~/sar_env/bin/python -V)"
~/sar_env/bin/pip freeze > ~/sar_env.freeze.txt; echo "freeze lines: $(wc -l < ~/sar_env.freeze.txt)"
echo "vllm image: $(docker image inspect vllm/vllm-openai:latest --format '{{index .RepoDigests 0}}')"
echo "containers:"; docker ps -a --format '  {{.Names}} | {{.Image}} | {{.Status}}'
docker inspect raikou-qdrant --format 'qdrant: binds={{json .HostConfig.Binds}} ports={{json .HostConfig.PortBindings}} restart={{.HostConfig.RestartPolicy.Name}}'
echo "qdrant api key set (0 = no): $(grep -c '^QDRANT_API_KEY=.' ~/backend/.env)"
echo ".env keys that mention AWS or this box:"; grep -E 'amazonaws|ec2|3\.7\.243\.74' ~/backend/.env | cut -d= -f1
echo "code deployed:"; git -C ~/raikou-src log -1 --oneline
echo "drift from main (expect nothing):"; diff -rq ~/raikou-src/backend ~/backend -x .env -x models -x sessions -x __pycache__ -x '*.pyc'
df -h / | tail -1
du -sh ~/backend/models ~/qdrant_storage_v1 ~/olmoearth_env ~/sar_env 2>/dev/null
ls /etc/systemd/system/sar-backend.service.d/ /etc/nginx/sites-enabled/
echo "enabled at boot:"; systemctl is-enabled sar-backend sarchat-vllm raikou-outbox-dispatcher raikou-worker-cpu@1 raikou-worker-gpu@0 raikou-worker-optical@0 redis-server nginx 2>&1 | paste -sd' '
crontab -l 2>/dev/null; systemctl list-timers --no-pager | head -8
```

What to take from it:

- The `sar_env` Python version. It becomes `PYVER` in step 6.
- The vLLM image digest. Step 6 pins it so the new box runs the same build.
- The Qdrant image, ports and mount. Step 6 repeats them.
- Drift. The new box builds from GitHub `main`, so if the diff prints anything, push or reconcile that code first.
- Disk. The boot disk should hold the used space plus about 50 GB. The 150 GB below is a starting point.
- Any `.env` key listed under AWS. Review it in step 6.

## 2. Upgrade, budget and quota (console)

1. Billing: upgrade the Free Trial account to a paid account. The remaining credit carries over.
2. Billing > Budgets & alerts: create a budget with alerts at 50%, 90% and 100%. A budget only sends email; it does not stop spending.
3. IAM & Admin > Quotas & System Limits: request 1 for `GPUs (all regions)`, then 1 for the L4 quota in `asia-south1` (filter the list for `L4`; Google's quota page names the metric `GPU_FAMILY:NVIDIA_L4`). As the reason, say it is one GPU VM for an academic SAR and optical analysis demo, replacing an AWS g4dn instance.
4. Do not request preemptible L4 quota (`PREEMPTIBLE_NVIDIA_L4_GPUS`). Spot VMs use the standard quota until you ask for a preemptible one, and from then on they can only use that.
5. Wait for the approval emails. If a request stays pending or is refused, see "If the quota doesn't come".

## 3. Network and VM (Cloud Shell)

```bash
gcloud config set project $PROJECT
gcloud services enable compute.googleapis.com

gcloud compute addresses create raikou-api-ip --region=$REGION
IP=$(gcloud compute addresses describe raikou-api-ip --region=$REGION --format="value(address)")
echo "$IP"

gcloud compute firewall-rules create raikou-web \
  --network=default --direction=INGRESS --allow=tcp:80,tcp:443 \
  --target-tags=raikou-web --source-ranges=0.0.0.0/0
```

```bash
gcloud compute instances create $VM \
  --zone=$ZONE --machine-type=g2-standard-8 \
  --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
  --boot-disk-size=150GB --boot-disk-type=pd-balanced \
  --maintenance-policy=TERMINATE --restart-on-failure \
  --address=$IP --tags=raikou-web \
  --metadata=enable-oslogin=FALSE \
  --no-service-account --no-scopes
```

- For the build phase on Spot, add `--provisioning-model=SPOT --instance-termination-action=STOP` and remove `--restart-on-failure`. Start a preempted Spot VM again with `gcloud compute instances start $VM --zone=$ZONE`.
- The `default` network already allows SSH from anywhere, and the VM takes keys only. If the project has no `default` network, create the firewall rules on the network you use.
- `--no-service-account --no-scopes` means the VM holds no Google credentials. It does not need any: storage stays on S3.
- G2 machines cannot live-migrate. Google restarts them for host maintenance, typically 30 days or more apart with 7 days' notice. Every unit below is enabled, so the box comes back on its own.

## 4. Prepare the VM (VM)

```bash
gcloud compute ssh ubuntu@$VM --zone=$ZONE
```

This creates the `ubuntu` account that the unit files and `deploy-from-github.sh` expect. Then:

```bash
sudo apt-get update && sudo apt-get -y upgrade
sudo apt-get install -y git rsync tmux build-essential python3-venv redis-server nginx \
  certbot python3-certbot-nginx docker.io libgl1 libglib2.0-0
sudo usermod -aG docker ubuntu
sudo systemctl enable --now redis-server && redis-cli ping
```

If a "restart services" dialog appears, accept the defaults. Install the GPU driver with Google's installer:

```bash
curl -L https://storage.googleapis.com/compute-gpu-installation-us/installer/latest/cuda_installer.pyz --output cuda_installer.pyz
sudo python3 cuda_installer.pyz install_driver
```

The installer may restart the VM. If it does, reconnect and run the same command again. When it finishes, reboot, reconnect and check the card:

```bash
sudo reboot
```

```bash
nvidia-smi
```

It should show an L4 and a driver of at least 580.95.05, the minimum Google lists for the L4. Do not enable Secure Boot on this VM; the installer does not work with it. Then give Docker the GPU, following NVIDIA's Container Toolkit guide:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

Check that a container sees the GPU (you reconnected after the reboot, so the `docker` group already applies):

```bash
docker run --rm --gpus all ubuntu nvidia-smi
```

## 5. Copy state from EC2 (VM pulls)

The VM pulls from EC2 with a throwaway key, so nothing is added to the VM's metadata. On the VM, make the key and print the public half:

```bash
ssh-keygen -t ed25519 -N '' -f ~/.ssh/ec2-pull -C ec2-pull
cat ~/.ssh/ec2-pull.pub
```

On EC2, append that line to the authorized keys. In the AWS console, add an inbound SSH (22) rule to the box's security group for `<the static IP from step 3>/32`.

```bash
echo 'ssh-ed25519 AAAA... ec2-pull' >> ~/.ssh/authorized_keys
```

Back on the VM, run the copy inside `tmux`, so a dropped session does not stop it. A rerun resumes where it stopped.

```bash
tmux new -s copy
SSHE="ssh -i /home/ubuntu/.ssh/ec2-pull -o StrictHostKeyChecking=accept-new"
SRC=ubuntu@3.7.243.74
mkdir -p ~/backend

sudo rsync -rlpt --partial --info=progress2 -e "$SSHE" --rsync-path="sudo rsync" \
  $SRC:/home/ubuntu/backend/models/ /home/ubuntu/backend/models/
sudo rsync -tp -e "$SSHE" --rsync-path="sudo rsync" $SRC:/home/ubuntu/backend/.env /home/ubuntu/backend/.env
sudo rsync -t  -e "$SSHE" --rsync-path="sudo rsync" $SRC:/home/ubuntu/sar_env.freeze.txt /home/ubuntu/
sudo mkdir -p /var/www/raikou
sudo rsync -rlpt -e "$SSHE" --rsync-path="sudo rsync" $SRC:/var/www/raikou/ /var/www/raikou/
sudo chown -R ubuntu:ubuntu /home/ubuntu/backend /home/ubuntu/sar_env.freeze.txt
chmod 600 ~/backend/.env
```

The models are about 13 GB and, as far as the repo shows, live only on the EC2 box. AWS bills the data leaving EC2. `/var/www/raikou` is the small static page nginx serves at `api.raikou.site`.

Optional warm-up: copy the Qdrant directory now, while Qdrant is still running on EC2. The copy is not consistent, but the final pass in step 8 then only sends the differences.

```bash
sudo rsync -a --numeric-ids -e "$SSHE" --rsync-path="sudo rsync" \
  $SRC:/home/ubuntu/qdrant_storage_v1/ /home/ubuntu/qdrant_storage_v1/
```

When the copying is finished for good (after step 8), delete the security-group rule and the `ec2-pull` line from EC2's `~/.ssh/authorized_keys`.

## 6. Build the backend (VM)

Code, from GitHub `main`, laid out the way `deploy-from-github.sh` does it:

```bash
git clone --depth 1 https://github.com/Trivo121/Raikou.git ~/raikou-src
git -C ~/raikou-src log -1 --oneline          # must match the commit from step 1
rsync -a --exclude '/.env' --exclude '/sessions' --exclude '/models' \
  --exclude '__pycache__' --exclude '*.pyc' ~/raikou-src/backend/ ~/backend/
```

Python environment. `requirements.txt` has only `>=` bounds, so install the freeze from step 1 to get the versions that work today:

```bash
PYVER=3.12            # the minor version step 1 printed for sar_env
python3 -m venv /tmp/uvenv && /tmp/uvenv/bin/pip -q install uv
/tmp/uvenv/bin/uv venv --python $PYVER ~/sar_env
/tmp/uvenv/bin/uv pip install --python ~/sar_env/bin/python -r ~/sar_env.freeze.txt
```

If the freeze will not install, use `-r ~/backend/requirements.txt` and rerun the checks in step 7.

Edit `~/backend/.env`:

- S3 access. EC2 reads S3 through its instance role, and GCP has no equivalent. In the AWS account that owns the bucket, create an IAM user such as `raikou-gcp` with the same inline policy as the role (`raikou-s3-access`), create an access key for it, and add `AWS_ACCESS_KEY_ID=` and `AWS_SECRET_ACCESS_KEY=` to the file. Paste them straight into the file, not into chat.
- Every key that step 1 listed as mentioning AWS or the box.
- Check that `SARCLIP_DEVICE` is not set in the file. The CPU override below sets it for the API only, so the GPU worker keeps the GPU.

Check the config the same way the deploy script does:

```bash
cd ~/backend && ENVIRONMENT=production ~/sar_env/bin/python -c "from app.core.config import settings; print('Config OK:', settings.PROJECT_NAME)"
```

Images, pinned to what runs on EC2:

```bash
VLLM_IMAGE=vllm/vllm-openai@sha256:...        # from step 1
QDRANT_IMAGE=qdrant/qdrant:...                # from step 1
docker pull $VLLM_IMAGE
docker pull $QDRANT_IMAGE
```

Services. This installs the A10G unit `sarchat-vllm.service`, not `sarchat-vllm.t4.service`. The L4 has bf16, so the T4's `--dtype half` and `--enforce-eager` are not needed. The CPU override for SARCLIP is installed anyway: it keeps the VRAM budget identical to the box that was tested with the optical worker. Remove it later if `nvidia-smi` shows at least 4 GB free under load.

```bash
cd ~/raikou-src/deployment
sudo cp sar-backend.service sarchat-vllm.service raikou-outbox-dispatcher.service \
  'raikou-worker-cpu@.service' 'raikou-worker-gpu@.service' 'raikou-worker-optical@.service' \
  /etc/systemd/system/
sudo sed -i "s#vllm/vllm-openai:latest#$VLLM_IMAGE#" /etc/systemd/system/sarchat-vllm.service
sudo mkdir -p /etc/systemd/system/sar-backend.service.d
sudo cp sar-backend-localhost.override.conf /etc/systemd/system/sar-backend.service.d/10-localhost.conf
sudo cp sar-backend-sarclip-cpu.override.conf /etc/systemd/system/sar-backend.service.d/20-sarclip-cpu.conf
sudo systemctl daemon-reload
```

Qdrant, with the same mount and ports as on EC2 (step 1 printed them):

```bash
mkdir -p ~/qdrant_storage_v1
docker run -d --name raikou-qdrant --restart unless-stopped \
  -p 127.0.0.1:6333:6333 -v /home/ubuntu/qdrant_storage_v1:/qdrant/storage $QDRANT_IMAGE
```

If step 1 reported an API key for Qdrant, pass it to the container as EC2 does.

Start everything except the dispatcher. The dispatcher is what pulls jobs from Supabase, so it stays off until the cutover.

```bash
sudo systemctl enable --now sarchat-vllm sar-backend raikou-worker-cpu@1 raikou-worker-gpu@0
```

The optical environment (about 6 GB, with its own smoke test):

```bash
bash ~/raikou-src/deployment/optical/setup_optical_env.sh
sudo systemctl enable --now raikou-worker-optical@0
```

nginx:

```bash
sudo cp ~/raikou-src/deployment/raikou-api.nginx.conf /etc/nginx/sites-available/raikou-api
sudo ln -sf /etc/nginx/sites-available/raikou-api /etc/nginx/sites-enabled/raikou-api
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl reload nginx
```

## 7. Dry run (VM)

```bash
curl -s localhost:8000/healthz; echo
curl -s localhost:8000/readyz; echo
curl -s localhost:8001/v1/models | head -c 300; echo
curl -s localhost:8001/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"/models/SARChat-InternVL2.5-2B","messages":[{"role":"user","content":"Say OK."}],"max_tokens":8}'
nvidia-smi --query-gpu=memory.used,memory.total --format=csv
```

- `/readyz` should be fully green: object store, Redis, Supabase, Qdrant and the pipeline checks. A red object store usually means the S3 key, bucket name or region.
- vLLM needs a few minutes on the first start. If it is not answering, run `sudo journalctl -u sarchat-vllm -n 50 --no-pager`.
- This unit asks vLLM for 60% of the card, about 13.5 GB. If it runs out of memory, lower `--gpu-memory-utilization` to 0.55 in `/etc/systemd/system/sarchat-vllm.service`.
- This setup has not been tried on an L4. The A10G unit was written for a card of the same size, and the T4 box showed the CPU-SARCLIP layout works with the optical worker.

## 8. Cutover

Do this when nothing is being processed. If EC2 is not serving traffic anyway, skip the waiting.

1. The day before, in GoDaddy, set the TTL of the `api` A record to the lowest value it offers.
2. On EC2, stop the dispatcher first and wait until no scene is processing (the UI shows it). Then stop the rest, including Qdrant:

   ```bash
   sudo systemctl stop raikou-outbox-dispatcher
   ```

   ```bash
   sudo systemctl stop raikou-worker-optical@0 raikou-worker-gpu@0 raikou-worker-cpu@1 sar-backend
   docker stop raikou-qdrant
   ```

3. On the VM, copy Qdrant a last time and start it on the result (reuse `SSHE` and `SRC` from step 5):

   ```bash
   docker stop raikou-qdrant
   sudo rsync -a --numeric-ids --delete -e "$SSHE" --rsync-path="sudo rsync" \
     $SRC:/home/ubuntu/qdrant_storage_v1/ /home/ubuntu/qdrant_storage_v1/
   docker start raikou-qdrant
   curl -s localhost:6333/collections
   ```

   The collections should include `sar_patches`, and `optical_patches` if the optical page has been used.

4. On the VM, start the dispatcher and recheck readiness:

   ```bash
   sudo systemctl enable --now raikou-outbox-dispatcher
   curl -s localhost:8000/readyz; echo
   ```

5. In GoDaddy, point the `api` A record at the static IP from step 3. Wait until `nslookup api.raikou.site 8.8.8.8` shows it.
6. On the VM, get the certificate once the name resolves to the new IP. Until then HTTPS fails:

   ```bash
   sudo certbot --nginx -d api.raikou.site -m you@example.com --agree-tos --redirect
   ```

7. Test through the real site at https://www.raikou.site: sign in, open a project, ask the chat about an existing scene, start a new area scene, and run one on the optical page.
8. Stop the EC2 instance in the console. Do not terminate it yet.

From here on, deploy with `deployment/deploy-from-github.sh`, run on the VM, as before.

## Rollback

- Point the `api` record back at 3.7.243.74 and start the EC2 instance. Step 1 recorded which services start at boot; start any that do not by hand (`docker start raikou-qdrant` for Qdrant).
- Scenes processed on GCP exist in Supabase and S3, but their vectors are only in the VM's Qdrant. After real use on GCP, copy `qdrant_storage_v1` back the same way, or reprocess those scenes from the UI.

## Retiring the AWS side

- Wait at least a week of stable use.
- If you want a last copy, create an AMI of the stopped instance, then terminate the instance and delete its volume.
- Before you release the Elastic IP 3.7.243.74, make sure no DNS record in GoDaddy still points at it. A released address goes to whoever asks next. 13.206.1.62 already ended up serving another company's site that way.

## If the quota doesn't come

- A request can sit pending for days. Answer any follow-up from the quota team.
- Quota is per region and per GPU model. Singapore (`asia-southeast1`) offers the L4 in zones a, b and c, at the cost of more latency for users in India.
- The credit's 90 days keep running while you wait. Decide by the end of the first week whether to keep waiting. Until quota arrives, the AWS box stays the backend.

## Cost controls

Stop the VM between sessions. A stopped VM still bills its disk, and a reserved IP that is not in use bills at a higher rate than one in use.

```bash
gcloud compute instances stop $VM --zone=$ZONE
gcloud compute instances start $VM --zone=$ZONE
```

To stop it every night automatically (Google's schedule runs up to 15 minutes late):

```bash
gcloud compute resource-policies create instance-schedule raikou-hours \
  --region=$REGION --timezone=Asia/Kolkata \
  --vm-start-schedule='0 9 * * MON-FRI' --vm-stop-schedule='0 21 * * *'
gcloud compute instances add-resource-policies $VM --resource-policies=raikou-hours --zone=$ZONE
PN=$(gcloud projects describe $PROJECT --format="value(projectNumber)")
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:service-$PN@compute-system.iam.gserviceaccount.com" \
  --role=roles/compute.instanceAdmin.v1
```

The last command gives the Compute Engine service agent the role Google's schedule page says it needs to start and stop VMs.

## Not checked when this was written

- The EC2 box was unreachable (SSH and `https://api.raikou.site/healthz` both timed out), so its Python version, disk use and Qdrant settings come from step 1, not from this draft.
- The image family `ubuntu-2404-lts-amd64` was not confirmed in Google's docs. If gcloud rejects it, list the names with `gcloud compute images list --project=ubuntu-os-cloud --filter="family:ubuntu-2404" --format="value(family)"`.
- That `gcloud compute ssh ubuntu@...` creates the `ubuntu` account, and that `enable-oslogin=FALSE` keeps metadata keys working, were not confirmed in the docs I could read. If you land in a different account, create it with `sudo adduser ubuntu`, give it passwordless sudo, and run the rest as `sudo -iu ubuntu`.
- The trial credit paying for GPU line items after the upgrade was not confirmed. Check Billing > Reports after the first hour.
- Hourly prices come from a third-party copy of Google's list. The T4 price in Mumbai was never found, which is one more reason this plan uses the L4.

## References

- [Free Trial limits and upgrading](https://docs.cloud.google.com/free/docs/free-cloud-features)
- [GPU quotas, including Spot](https://docs.cloud.google.com/compute/resource-usage)
- [Requesting a quota increase](https://docs.cloud.google.com/docs/quotas/view-manage)
- [GPU regions and zones](https://docs.cloud.google.com/compute/docs/gpus/gpu-regions-zones)
- [Create a G2 or G4 instance](https://docs.cloud.google.com/compute/docs/gpus/create-gpu-vm-g-series)
- [Install GPU drivers](https://docs.cloud.google.com/compute/docs/gpus/install-drivers-gpu)
- [Spot VMs](https://docs.cloud.google.com/compute/docs/instances/spot)
- [Instance schedules](https://docs.cloud.google.com/compute/docs/instances/schedule-instance-start-stop)
- [Budgets only send alerts](https://docs.cloud.google.com/billing/docs/how-to/budgets)
- [Reserving a static external IP](https://docs.cloud.google.com/vpc/docs/reserve-static-external-ip-address)
- [NVIDIA Container Toolkit install guide](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
