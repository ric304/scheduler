#!/usr/bin/env bash
set -euo pipefail

cd /mnt/c/vscode/Scheduler

venv/bin/python manage.py scheduler_seed_sample_job

venv/bin/python manage.py shell <<'PY'
from scheduler.models import JobDefinition

print('job_defs', JobDefinition.objects.count())
print('latest', JobDefinition.objects.order_by('-id').values('id','name','command_name','schedule').first())
PY
