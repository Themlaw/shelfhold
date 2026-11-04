"""`202 Accepted` + `Location: /api/v1/jobs/{id}` for long operations (EF-INT-001)."""

from fastapi.responses import JSONResponse
from pydantic import BaseModel

JOBS_PATH = "/api/v1/jobs"


class JobAccepted(BaseModel):
    """Body of a 202: the job to poll (the same URL is in the `Location` header)."""

    job_id: str
    status_url: str


def job_location(job_id: str) -> str:
    return f"{JOBS_PATH}/{job_id}"


def accepted_job(job_id: str) -> JSONResponse:
    """Answer of a route declared `status_code=202, response_model=JobAccepted`."""
    url = job_location(job_id)
    body = JobAccepted(job_id=job_id, status_url=url)
    return JSONResponse(body.model_dump(), status_code=202, headers={"Location": url})
