"""Exercise real MongoDB lease and job fencing under concurrent claims."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nb.multitenant.models import ForwardTaskInput, TaskStatus
from nb.multitenant.repository import MongoRepository, utcnow


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mongo-uri", default=os.getenv("MONGO_TEST_URI", ""))
    parser.add_argument("--tenants", type=int, default=400)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--keep-database", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.mongo_uri:
        raise SystemExit("Set MONGO_TEST_URI or pass --mongo-uri")
    if args.tenants < 1 or args.workers < 2:
        raise SystemExit("Use at least one tenant and two workers")

    database = f"nb-load-{uuid.uuid4()}"
    repo = MongoRepository(args.mongo_uri, database)
    started = time.perf_counter()
    try:
        repo.ensure_indexes()
        repo.ping()
        attempts = [
            (tenant_id, f"worker-{worker_id}")
            for tenant_id in range(1, args.tenants + 1)
            for worker_id in range(args.workers)
        ]
        random.shuffle(attempts)

        def claim(value: tuple[int, str]):
            tenant_id, owner = value
            lease = repo.claim_lease(tenant_id, owner, 45)
            return tenant_id, owner, lease

        with ThreadPoolExecutor(max_workers=args.threads) as executor:
            results = list(executor.map(claim, attempts))

        owners: dict[int, list[tuple[str, dict]]] = {}
        for tenant_id, owner, lease in results:
            if lease:
                owners.setdefault(tenant_id, []).append((owner, lease))
        if len(owners) != args.tenants or any(len(value) != 1 for value in owners.values()):
            raise RuntimeError("A tenant was unclaimed or received more than one live lease")

        fenced_jobs = 0
        for tenant_id in range(1, min(args.tenants, 100) + 1):
            old_owner, old_lease = owners[tenant_id][0]
            task = repo.create_task(
                tenant_id,
                ForwardTaskInput(name=f"load-{tenant_id}"),
            )
            task = repo.set_task_status(
                tenant_id,
                task.id,
                TaskStatus.ACTIVE,
            )
            repo.fence_tenant_tasks(tenant_id, int(old_lease["epoch"]))
            job_id = repo.create_job(
                tenant_id,
                task.id,
                kind="load",
                task_revision=task.revision,
            )
            repo.claim_job(tenant_id, old_owner, int(old_lease["epoch"]))
            repo.leases.update_one(
                {"_id": tenant_id},
                {"$set": {"expires_at": utcnow() - timedelta(seconds=1)}},
            )
            new_owner = f"takeover-{tenant_id}"
            new_lease = repo.claim_lease(tenant_id, new_owner, 45)
            if new_lease is None:
                raise RuntimeError("Expired lease was not taken over")
            if repo.update_task_offsets(
                tenant_id,
                task.id,
                {"source": 1},
                expected_revision=task.revision,
                lease_epoch=int(old_lease["epoch"]),
            ):
                raise RuntimeError("Stale worker wrote task offsets")
            if not repo.update_task_offsets(
                tenant_id,
                task.id,
                {"source": 2},
                expected_revision=task.revision,
                lease_epoch=int(new_lease["epoch"]),
            ):
                raise RuntimeError("Current worker could not write task offsets")
            repo.claim_job(tenant_id, new_owner, int(new_lease["epoch"]))
            if repo.finish_job(
                job_id,
                worker_id=old_owner,
                lease_epoch=int(old_lease["epoch"]),
            ):
                raise RuntimeError("Stale worker completed a fenced job")
            if not repo.finish_job(
                job_id,
                worker_id=new_owner,
                lease_epoch=int(new_lease["epoch"]),
            ):
                raise RuntimeError("Current worker could not complete its job")
            fenced_jobs += 1

        elapsed = time.perf_counter() - started
        print(
            json.dumps(
                {
                    "database": database,
                    "tenants": args.tenants,
                    "workers": args.workers,
                    "claim_attempts": len(attempts),
                    "fenced_jobs": fenced_jobs,
                    "elapsed_seconds": round(elapsed, 3),
                    "status": "ok",
                },
                ensure_ascii=False,
            )
        )
    finally:
        if not args.keep_database:
            repo.client.drop_database(database)


if __name__ == "__main__":
    main()
