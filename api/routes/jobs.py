from fastapi import APIRouter, HTTPException

from jobs import get_job_manager

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("")
async def list_jobs():
    return {"ok": True, "data": [j.model_dump(mode="json") for j in get_job_manager().list()]}


@router.get("/{job_id}")
async def get_job(job_id: str):
    job = get_job_manager().get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True, "data": job.model_dump(mode="json")}
