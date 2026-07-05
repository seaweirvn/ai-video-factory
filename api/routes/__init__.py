from fastapi import FastAPI

from api.routes import (
    analytics,
    content,
    edit,
    jobs,
    materials,
    publish,
    scoring,
    selection,
)


def register_routes(app: FastAPI) -> None:
    app.include_router(jobs.router)
    app.include_router(materials.router)
    app.include_router(selection.router)
    app.include_router(edit.router)
    app.include_router(content.router)
    app.include_router(publish.router)
    app.include_router(analytics.router)
    app.include_router(scoring.router)
