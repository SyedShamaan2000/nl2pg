"""Main FastAPI application entry point."""

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("src.api.router:app", host="0.0.0.0", port=8000, reload=True)
