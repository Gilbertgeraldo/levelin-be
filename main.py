from fastapi import FastAPI

app = FastAPI(title="LevelIn-be")

@app.get("/")
def read_root():
    return {"message" : "levelin titit" }